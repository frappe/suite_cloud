from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date

from suite_cloud.hosting import capacity, domains
from suite_cloud.hosting.tests.fakes import PROXY_ZONE, REGION
from suite_cloud.hosting.tests.fixtures import SITE_ZONE, HostingFixtures


class TestSiteDomains(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting(tenant_mail_zone=None)
        self.vm = self.make_vm()
        self.tenant = self.make_hosted_tenant()

    def claim(self):
        return self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

    def records(self, site) -> set:
        rows = frappe.get_all(
            "DNS Record",
            {"managed_by_doctype": "Suite Site", "managed_by": site.name},
            ["host", "type", "value"],
        )
        return {(r.host, r.type, r.value) for r in rows}

    def test_the_site_domain_is_published_attached_and_primary(self) -> None:
        site = self.claim()

        domain = f"acme.{SITE_ZONE}"
        self.assertEqual(site.primary_domain, domain)
        self.assertEqual(site.url, f"https://{domain}")
        row = site.get_domain(domain)
        self.assertEqual((row.status, row.is_primary, row.managed), ("Active", 1, 1))
        self.assertEqual(
            self.records(site),
            {("acme", "CNAME", f"proxy.{PROXY_ZONE}"), ("_frappe-verification.acme", "TXT", "token-1")},
        )
        self.assertEqual(self.pilot.primary[site.hostname], domain)

    def test_records_not_visible_yet_wait(self) -> None:
        with patch("suite_cloud.hosting.domains.verify_dns_record", return_value=False):
            site = self.claim()

        self.assertFalse(site.primary_domain)
        self.assertEqual(site.get_domain(f"acme.{SITE_ZONE}").status, "Pending")
        self.assertTrue(site.pending_follow_ups())
        self.assertNotIn("add_domain", [call[0] for call in self.pilot.calls])

    def test_stale_records_are_fetched_again_and_republished(self) -> None:
        with patch("suite_cloud.hosting.domains.verify_dns_record", return_value=False):
            site = self.claim()
        row = site.get_domain(f"acme.{SITE_ZONE}")
        frappe.db.set_value("Suite Site Domain", row.name, "records_fetched_at", add_to_date(None, hours=-24))
        self.pilot.token = "token-2"

        domains.ensure_site_domain(self.site(site.name))

        self.assertIn(("_frappe-verification.acme", "TXT", "token-2"), self.records(site))
        self.assertNotIn(("_frappe-verification.acme", "TXT", "token-1"), self.records(site))

    def test_already_attached_counts_as_active(self) -> None:
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        self.pilot.attached[site.hostname] = {f"acme.{SITE_ZONE}"}

        site = self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

        self.assertEqual(site.get_domain(f"acme.{SITE_ZONE}").status, "Active")
        self.assertEqual(site.primary_domain, f"acme.{SITE_ZONE}")

    def test_a_manual_domain_is_added_verified_made_primary_and_removed(self) -> None:
        site = self.claim()

        records = site.add_domain("shop.example.com")
        self.assertEqual({r["type"] for r in records}, {"CNAME", "TXT"})  # A records are not offered
        self.assertTrue(self.site(site.name).verify_domain("shop.example.com"))
        self.site(site.name).make_primary("shop.example.com")

        site = self.site(site.name)
        self.assertEqual(site.primary_domain, "shop.example.com")
        self.assertRaisesRegex(frappe.ValidationError, "primary", site.remove_domain, "shop.example.com")
        site.make_primary(f"acme.{SITE_ZONE}")
        self.site(site.name).remove_domain("shop.example.com")
        self.assertIsNone(self.site(site.name).get_domain("shop.example.com"))
        # Records of a domain the tenant publishes are never Suite Cloud's.
        self.assertFalse(any("shop" in host for host, _type, _value in self.records(site)))

    def test_a_renewed_token_asks_for_republishing(self) -> None:
        site = self.claim()
        site.add_domain("shop.example.com")
        row = self.site(site.name).get_domain("shop.example.com")
        frappe.db.set_value("Suite Site Domain", row.name, "records_fetched_at", add_to_date(None, hours=-24))
        self.pilot.token = "token-2"

        self.assertRaises(domains.DomainNotReadyError, self.site(site.name).verify_domain, "shop.example.com")

    def test_custom_domains_need_a_letsencrypt_email(self) -> None:
        site = self.claim()
        self.vm.db_set("letsencrypt_email_set", 0)

        self.assertRaisesRegex(
            frappe.ValidationError, "Let's Encrypt", self.site(site.name).add_domain, "shop.example.com"
        )
