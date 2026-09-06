import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint

from suite_cloud.tests.fixtures import (
    ROOT_DOMAIN,
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_node,
    make_zone,
    remove_cluster,
)
from suite_cloud.utils import get_config

# Unique to the tests so a site's own records never collide with them.
TEST_HOST = "suite-cloud-test"
OTHER_ZONE = "other.test"


class TestDNSZone(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        frappe.db.delete("DNS Record", {"host": TEST_HOST})
        configure_settings()

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_domain_name_is_normalised(self) -> None:
        zone = make_zone("Zone.TEST.")
        self.assertEqual(zone.name, "zone.test")

    def test_provider_requires_credentials(self) -> None:
        zone = make_zone()
        zone.dns_provider = "Cloudflare"
        zone.dns_provider_token = ""

        self.assertRaisesRegex(frappe.ValidationError, "Token", zone.save)

    def test_only_one_zone_is_default(self) -> None:
        make_zone()
        make_zone(OTHER_ZONE)
        self.assertEqual(frappe.db.get_value("DNS Zone", ROOT_DOMAIN, "is_default"), 0)
        self.assertEqual(frappe.db.get_value("DNS Zone", OTHER_ZONE, "is_default"), 1)

        make_zone()  # back to the fixture default so later tests see it
        self.assertEqual(frappe.db.get_value("DNS Zone", OTHER_ZONE, "is_default"), 0)

    def test_dns_record_defaults_to_the_default_zone(self) -> None:
        record = make_dns_record()
        self.assertEqual(record.dns_zone, ROOT_DOMAIN)
        self.assertEqual(record.fqdn, f"{TEST_HOST}.{ROOT_DOMAIN}")

    def test_zone_ttl_wins_over_settings(self) -> None:
        make_zone(default_ttl=60)
        self.assertEqual(make_dns_record().ttl, 60)

        make_zone(default_ttl=0)
        frappe.db.delete("DNS Record", {"host": TEST_HOST})
        self.assertEqual(make_dns_record().ttl, cint(get_config("default_dns_ttl")))

    def test_same_record_may_exist_in_two_zones(self) -> None:
        make_zone(OTHER_ZONE, is_default=0)
        make_dns_record()
        other = make_dns_record(zone=OTHER_ZONE)
        self.assertEqual(other.fqdn, f"{TEST_HOST}.{OTHER_ZONE}")
        self.assertEqual(frappe.db.count("DNS Record", {"host": TEST_HOST}), 2)

    def test_cluster_records_live_in_the_cluster_zone(self) -> None:
        make_zone(OTHER_ZONE, is_default=0)
        remove_cluster("eu-1")
        cluster = make_cluster("eu-1", hostname=f"mail.eu.{OTHER_ZONE}", dns_zone=OTHER_ZONE)
        node = make_node(cluster, "n1", "203.0.113.50")
        clear_request_cache()

        zones = frappe.get_all(
            "DNS Record", {"managed_by": ["in", [cluster.name, node.name]]}, pluck="dns_zone"
        )
        self.assertTrue(zones)
        self.assertEqual(set(zones), {OTHER_ZONE})
        self.assertTrue(frappe.db.exists("DNS Record", {"dns_zone": OTHER_ZONE, "host": "spf.eu"}))
        remove_cluster("eu-1")

    def test_cluster_hostname_must_be_under_its_zone(self) -> None:
        make_zone(OTHER_ZONE, is_default=0)
        remove_cluster("eu-1")
        self.assertRaisesRegex(
            frappe.ValidationError,
            "DNS zone",
            make_cluster,
            "eu-1",
            hostname=f"mail.eu.{ROOT_DOMAIN}",
            dns_zone=OTHER_ZONE,
        )

    def test_every_zone_is_reserved_for_mail_domains(self) -> None:
        from suite_cloud.tenancy.addresses import assert_domain_available

        make_zone(OTHER_ZONE, is_default=0)
        self.assertRaisesRegex(
            frappe.ValidationError, "reserved", assert_domain_available, f"customer.{OTHER_ZONE}", "any-site"
        )


def make_dns_record(zone: str | None = None):
    """Inserts a DNS Record without a provider, which leaves it unverified and enqueues nothing."""

    record = frappe.new_doc("DNS Record")
    record.dns_zone = zone
    record.host = TEST_HOST
    record.type = "A"
    record.value = "203.0.113.10"
    record.category = "Other"
    return record.insert()
