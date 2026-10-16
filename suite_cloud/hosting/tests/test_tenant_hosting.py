import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.hosting import capacity
from suite_cloud.hosting.doctype.region.region import sync_regions
from suite_cloud.hosting.tests.fakes import REGION
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, SITE_ZONE, HostingFixtures, make_region
from suite_cloud.mail.tenancy.placement import pick_cluster
from suite_cloud.mail.tests.fixtures import ROOT_DOMAIN, activate_cluster, make_cluster


class TestHostingSettings(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()
        self.settings = frappe.get_single("Suite Cloud Settings")

    def test_the_two_zones_must_differ(self) -> None:
        self.settings.tenant_mail_zone = SITE_ZONE
        self.assertRaisesRegex(frappe.ValidationError, "different zones", self.settings.save)

    def test_zones_need_a_provider(self) -> None:
        frappe.db.set_value("DNS Zone", MAIL_ZONE, "dns_provider", "")
        self.assertRaisesRegex(frappe.ValidationError, "needs a DNS provider", self.settings.save)

    def test_the_mail_zone_cannot_be_a_cluster_zone(self) -> None:
        make_cluster()  # lives under the default zone
        self.settings.tenant_mail_zone = ROOT_DOMAIN
        frappe.db.set_value("DNS Zone", ROOT_DOMAIN, "dns_provider", "Cloudflare")
        self.assertRaisesRegex(frappe.ValidationError, "zone of cluster", self.settings.save)

    def test_central_is_configured_with_all_four_fields(self) -> None:
        self.assertTrue(self.settings.is_central_configured)
        self.settings.central_team = None
        self.assertFalse(self.settings.is_central_configured)

    def test_test_central_connection_reads_capabilities(self) -> None:
        self.assertIn("server:create", self.settings.test_central_connection())


class TestRegionSync(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()

    def test_sync_keeps_what_the_operator_set(self) -> None:
        make_region(enabled=1, proxy_zone="zz1.frappe.test", display_name="old")

        self.assertEqual(sync_regions(), 1)

        region = frappe.get_doc("Region", REGION)
        self.assertEqual((region.display_name, region.reachable), ("Test Region", 1))
        self.assertEqual(
            (region.enabled, region.proxy_zone, region.stalwart_region), (1, "zz1.frappe.test", "blr")
        )


class TestPlacement(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()
        # Clusters of the site the tests run on would compete; the transaction is rolled back.
        frappe.db.set_value("Stalwart Cluster", {"name": ["not like", f"%.{ROOT_DOMAIN}"]}, "enabled", 0)

    def test_a_cluster_serving_the_region_is_preferred_over_the_default(self) -> None:
        serving = activate_cluster(make_cluster("blr", hostname=f"mail.blr.{ROOT_DOMAIN}"))
        other = activate_cluster(
            make_cluster("eu", hostname=f"mail.eu.{ROOT_DOMAIN}", regions=[{"region": "eu"}])
        )
        other.db_set("is_default", 1)

        self.assertEqual(pick_cluster(REGION), serving.name)
        self.assertEqual(pick_cluster("nowhere"), other.name)

    def test_a_new_hosted_tenant_is_placed_only_when_asked(self) -> None:
        cluster = activate_cluster(make_cluster())
        self.assertFalse(self.make_hosted_tenant().stalwart_cluster)  # the fixture default is off

        self.set_setting("auto_place_tenants", 1)
        self.assertEqual(self.make_hosted_tenant().stalwart_cluster, cluster.name)
        self.assertFalse(self.make_hosted_tenant(tenant_type="Team").stalwart_cluster)


class TestTenantGuards(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting(tenant_mail_zone=None)
        self.make_vm()
        self.tenant = self.make_hosted_tenant()

    def test_create_hosted_site_claims_a_site(self) -> None:
        name = self.tenant.create_hosted_site("acme")

        self.assertEqual(self.site(name).status, "Active")
        self.assertRaisesRegex(
            frappe.ValidationError, "Drop the tenant's site", self.tenant.create_hosted_site, "other"
        )

    def test_a_tenant_with_a_live_site_cannot_be_archived_or_deleted(self) -> None:
        self.tenant.create_hosted_site("acme")

        self.assertRaisesRegex(frappe.ValidationError, "Drop the tenant's site", self.tenant.archive)
        self.assertRaisesRegex(frappe.ValidationError, "Drop the tenant's site", self.tenant.delete)

    def test_a_rotated_secret_reaches_the_active_site(self) -> None:
        self.tenant.create_hosted_site("acme")

        secret = self.tenant.rotate_secret()

        self.assertEqual(self.site_api.singles["Suite Settings"]["suite_cloud_secret"], secret)

    def test_a_secret_that_cannot_be_pushed_reconfigures_the_site(self) -> None:
        name = self.tenant.create_hosted_site("acme")
        self.site_api.set_single = lambda *args: (_ for _ in ()).throw(RuntimeError("down"))

        self.tenant.rotate_secret()

        self.assertEqual(self.site(name).status, "Failed")  # Configure ran again and the site is down
        self.assertFalse(self.site(name).suite_configured)

    def test_only_suite_site_tenants_get_a_site(self) -> None:
        team = self.make_hosted_tenant("team.frappe.test", tenant_type="Team")
        self.assertRaisesRegex(
            frappe.ValidationError, "Only a Suite Site tenant", team.create_hosted_site, "team"
        )

    def test_a_disabled_region_takes_no_sites(self) -> None:
        frappe.db.set_value("Region", REGION, "enabled", 0)
        self.assertRaisesRegex(
            frappe.ValidationError, "does not take sites", self.tenant.create_hosted_site, "acme"
        )

    def test_the_owner_sees_only_their_site(self) -> None:
        name = self.tenant.create_hosted_site("acme")
        pooled = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()

        frappe.set_user(self.tenant.user)
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertEqual(frappe.get_list("Suite Site", pluck="name"), [name])
        self.assertFalse(frappe.has_permission("Suite Site", "read", pooled.name))
        self.assertFalse(frappe.has_permission("Suite Site", "write", name))
