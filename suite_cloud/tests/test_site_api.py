from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api import fc
from suite_cloud.api.site import (
    SiteAuthError,
    SiteSuspendedError,
    accounts,
    current_site,
    domains,
    groups,
    mailing_lists,
)
from suite_cloud.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.stalwart import forget_sessions
from suite_cloud.tenancy.ownership import VALUE_PREFIX, DomainNotVerifiedError, OwnershipLookupError
from suite_cloud.tests.fake_stalwart import FakeStalwart
from suite_cloud.tests.fixtures import (
    activate_cluster,
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_site,
    verified_ownership,
)


class SiteApiTestCase(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        self.cluster = activate_cluster(make_cluster())
        self.fake = FakeStalwart(
            base_url=self.cluster.base_url, admin_password=self.cluster.get_password("admin_password")
        )
        self.fake.add_token("test-token")
        self.fake._add("Role", {"description": DISABLED_ROLE_DESCRIPTION})
        self.fake.singletons["SystemSettings"] = {
            "mailExchangers": {"0": {"hostname": self.cluster.hostname, "priority": 10}}
        }
        self._install = self.fake.install()
        self._install.__enter__()
        self.addCleanup(self._install.__exit__, None, None, None)
        forget_sessions(self.cluster)
        clear_request_cache()
        self._ownership = verified_ownership()
        self._ownership.start()
        self.addCleanup(self._ownership.stop)
        self.site = make_site(self.cluster)
        self.other = make_site(self.cluster, "other.frappe.test")
        self.act_as(self.site)

    def tearDown(self) -> None:
        frappe.local.suite_site = None
        frappe.local.request = None
        frappe.set_user("Administrator")
        for doctype in ("Mail Account", "Mail Group", "Mailing List", "Mail Domain"):
            for name in frappe.get_all(doctype, pluck="name"):
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_on_trash=True)
        frappe.flags.do_not_enqueue = False

    def act_as(self, site, secret: str = "any") -> None:
        """Simulates a request that Frappe already authenticated against the Suite Site key."""

        frappe.local.suite_site = None
        frappe.set_user(frappe.db.get_single_value("Suite Cloud Settings", "site_service_user"))
        frappe.local.request = frappe._dict(headers={"Authorization": f"token {site.api_key}:{secret}"})
        frappe.local.form_dict = frappe._dict()


class TestSiteResolution(SiteApiTestCase):
    def test_current_site_comes_from_the_authorization_header(self) -> None:
        self.assertEqual(current_site().name, self.site.name)

    def test_unknown_key_or_wrong_user_is_rejected(self) -> None:
        frappe.local.request = frappe._dict(headers={"Authorization": "token nope:secret"})
        frappe.local.suite_site = None
        self.assertRaises(SiteAuthError, current_site)

        frappe.set_user("Guest")
        frappe.local.request = frappe._dict(headers={"Authorization": f"token {self.site.api_key}:x"})
        self.assertRaises(SiteAuthError, current_site)

    def test_suspended_site_is_refused(self) -> None:
        self.site.db_set({"enabled": 0, "status": "Suspended"})
        frappe.clear_document_cache("Suite Site", self.site.name)
        frappe.local.suite_site = None
        self.assertRaises(SiteSuspendedError, current_site)

    def test_managers_can_act_for_a_site(self) -> None:
        frappe.set_user("Administrator")
        frappe.local.suite_site = None
        frappe.local.request = frappe._dict(headers={})
        frappe.local.form_dict = frappe._dict(site=self.other.name)
        self.assertEqual(current_site().name, self.other.name)


class TestDomainOwnership(SiteApiTestCase):
    def test_check_domain_hands_out_the_site_record(self) -> None:
        result = domains.check_domain("Acme.com")
        record = result["ownership_record"]
        self.assertEqual(result["domain"], "acme.com")
        self.assertEqual((record["type"], record["host"], record["fqdn"]), ("TXT", "@", "acme.com"))
        self.assertEqual(record["value"], f"{VALUE_PREFIX}{self.site.domain_verification_token}")
        # The same record for every domain of the site, a different one per site.
        self.assertEqual(domains.check_domain("other.com")["ownership_record"]["value"], record["value"])
        self.act_as(self.other)
        self.assertNotEqual(domains.check_domain("acme.com")["ownership_record"]["value"], record["value"])

    def test_domain_is_added_only_once_its_record_resolves(self) -> None:
        target = "suite_cloud.tenancy.ownership.verify_dns_record"
        with patch(target, return_value=False):
            self.assertRaisesRegex(
                DomainNotVerifiedError, self.site.domain_verification_token, domains.create_domain, "acme.com"
            )
        with patch(target, return_value=None):
            self.assertRaises(OwnershipLookupError, domains.create_domain, "acme.com")
        self.assertFalse(frappe.db.exists("Mail Domain", "acme.com"))
        self.assertEqual(self.fake.all("Domain"), [])

        with patch(target, return_value=True) as verify:
            domains.create_domain("acme.com")
        verify.assert_called_once_with(
            "acme.com", "TXT", f"{VALUE_PREFIX}{self.site.domain_verification_token}"
        )
        self.assertRaisesRegex(frappe.DuplicateEntryError, "already added", domains.check_domain, "acme.com")
        self.act_as(self.other)
        self.assertRaisesRegex(frappe.DuplicateEntryError, "not available", domains.check_domain, "acme.com")
        self.assertRaisesRegex(frappe.DuplicateEntryError, "not available", domains.create_domain, "acme.com")

    def test_operators_add_domains_without_the_record(self) -> None:
        target = "suite_cloud.tenancy.ownership.verify_dns_record"
        frappe.set_user("Administrator")
        with patch(target, return_value=False) as verify:
            frappe.get_doc(
                {"doctype": "Mail Domain", "domain_name": "acme.com", "site": self.site.name}
            ).insert()
        verify.assert_not_called()

        self.act_as(self.site)
        with patch(target, return_value=False):
            self.assertRaises(DomainNotVerifiedError, domains.create_domain, "other.com")


class TestDirectoryApi(SiteApiTestCase):
    def test_domain_account_group_list_flow(self) -> None:
        domain = domains.create_domain("Acme.com", description="Main")
        self.assertEqual(domain["domain"], "acme.com")
        self.assertTrue(any(r["category"] == "DKIM" for r in domain["dns_records"]))
        self.assertEqual([d["domain"] for d in domains.list_domains()], ["acme.com"])

        group = groups.create_group("sales@acme.com", description="Sales")
        mailing_list = mailing_lists.create_mailing_list("all@acme.com", recipients=["ext@example.org"])
        self.assertEqual((group["members"], group["description"]), ([], "Sales"))
        self.assertEqual(mailing_list["recipients"], ["ext@example.org"])
        account = accounts.create_account(
            "alice@acme.com",
            "secret-pw",
            display_name="Alice",
            aliases=["ally@acme.com"],
            groups=["sales@acme.com"],
            mailing_lists=["all@acme.com"],
        )
        self.assertEqual(account["groups"], ["sales@acme.com"])
        self.assertEqual(account["aliases"][0]["email"], "ally@acme.com")
        self.assertEqual(
            mailing_lists.get_mailing_list("all@acme.com")["recipients"],
            ["ext@example.org", "alice@acme.com"],
        )
        self.assertEqual(groups.get_group("sales@acme.com")["members"], ["alice@acme.com"])

        page = accounts.list_accounts(search="ali")
        self.assertEqual((page["total"], [a["email"] for a in page["items"]]), (1, ["alice@acme.com"]))

        accounts.set_password("alice@acme.com", "another-pw")
        self.assertTrue(
            accounts.create_app_password("alice@acme.com", "Phone")["secret"].startswith("apppassword-")
        )
        self.assertFalse(accounts.set_account_enabled("alice@acme.com", False)["enabled"])
        self.assertEqual(accounts.set_groups("alice@acme.com", [])["groups"], [])
        self.assertEqual(
            groups.set_group_members("sales@acme.com", ["alice@acme.com"])["members"], ["alice@acme.com"]
        )
        self.assertEqual(mailing_lists.set_recipients("all@acme.com", ["a@b.co"])["recipients"], ["a@b.co"])

        accounts.delete_account("alice@acme.com")
        groups.delete_group("sales@acme.com")
        mailing_lists.delete_mailing_list("all@acme.com")
        domains.delete_domain("acme.com")
        self.assertEqual(domains.list_domains(), [])
        self.assertEqual(self.fake.all("Domain"), [])

    def test_other_sites_objects_are_invisible(self) -> None:
        domains.create_domain("acme.com")
        accounts.create_account("bob@acme.com", "secret-pw")

        self.act_as(self.other)
        self.assertEqual(domains.list_domains(), [])
        self.assertRaises(frappe.DoesNotExistError, domains.get_domain, "acme.com")
        self.assertRaises(frappe.DoesNotExistError, accounts.get_account, "bob@acme.com")
        self.assertRaises(frappe.DoesNotExistError, accounts.set_password, "bob@acme.com", "hijacked")
        self.assertRaises(frappe.DoesNotExistError, accounts.create_account, "eve@acme.com", "secret-pw")
        self.assertRaises(frappe.DoesNotExistError, groups.create_group, "team@acme.com")

    def test_stalwart_refusals_become_422(self) -> None:
        domains.create_domain("acme.com")
        self.fake.objects["Account"]["taken"] = {
            "@type": "User",
            "id": "taken",
            "name": "carol",
            "domainId": self.fake.find("Domain", name="acme.com")["id"],
        }
        frappe.db.savepoint("refusal")
        with self.assertRaises(frappe.ValidationError) as ctx:
            accounts.create_account("carol@acme.com", "secret-pw")
        frappe.db.rollback(save_point="refusal")  # what the request handler does on an exception
        self.assertEqual(ctx.exception.http_status_code, 422)
        self.assertIn("alreadyExists", str(ctx.exception))
        self.assertFalse(frappe.db.exists("Mail Account", "carol@acme.com"))


class TestFrappeCloudApi(SiteApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        frappe.set_user("Administrator")
        frappe.local.suite_site = None
        # The site may hold a real default cluster; selection must land on the fixture cluster.
        frappe.db.set_value("Stalwart Cluster", {"name": ["!=", self.cluster.name]}, "is_default", 0)
        self.cluster.db_set("is_default", 1)

    def test_create_site_returns_credentials_once(self) -> None:
        with patch("suite_cloud.api.fc.get_public_url", return_value="https://cloud.suite.test"):
            result = fc.create_site("New.Frappe.Test", region="blr", fc_reference="site-42")

        self.assertEqual(result["site"], "new.frappe.test")
        self.assertEqual(result["cluster"], self.cluster.name)
        self.assertEqual(result["jmap_url"], self.cluster.base_url)
        self.assertEqual(result["suite_cloud_url"], "https://cloud.suite.test")
        self.assertEqual(result["authorization_source"], "Suite Site")
        self.assertEqual(len(result["api_secret"]), 40)
        self.assertEqual(
            frappe.get_doc("Suite Site", "new.frappe.test").get_password("api_secret"), result["api_secret"]
        )
        self.assertNotIn("api_secret", fc.get_site("new.frappe.test"))
        self.assertRaises(frappe.DuplicateEntryError, fc.create_site, "new.frappe.test")

    def test_rotate_suspend_resume_archive(self) -> None:
        rotated = fc.rotate_site_secret(self.site.name)
        self.assertEqual(
            frappe.get_doc("Suite Site", self.site.name).get_password("api_secret"), rotated["api_secret"]
        )
        self.assertEqual(fc.suspend_site(self.site.name)["status"], "Suspended")
        self.assertEqual(fc.resume_site(self.site.name)["status"], "Active")
        self.assertEqual(fc.archive_site(self.site.name)["status"], "Archived")

    def test_cluster_selection(self) -> None:
        self.assertEqual(fc.pick_cluster(None, None), self.cluster.name)
        self.assertEqual(fc.pick_cluster(None, "BLR"), self.cluster.name)
        self.assertEqual(
            fc.pick_cluster(None, "sfo"), self.cluster.name
        )  # falls back to the default/only cluster
        self.assertRaisesRegex(frappe.ValidationError, "not active", fc.pick_cluster, "missing", None)
