from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api import fc
from suite_cloud.api.mail import accounts, domains, groups, mailing_lists, meta
from suite_cloud.api.site import (
    SiteAddressError,
    SiteAuthError,
    SiteSuspendedError,
    current_site,
    ping,
    update_site_profile,
)
from suite_cloud.cloud_mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.cloud_mail.stalwart import forget_sessions
from suite_cloud.cloud_mail.tenancy.ownership import (
    VALUE_PREFIX,
    DomainNotVerifiedError,
    OwnershipLookupError,
)
from suite_cloud.cloud_mail.tests.fake_stalwart import FakeStalwart
from suite_cloud.cloud_mail.tests.fixtures import (
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
        # Only the fixture sites: the dev site holds real accounts, and deleting those even
        # inside the rolled-back transaction serialises each one, which asks the live cluster.
        for doctype in ("Mail Account", "Mail Group", "Mailing List", "Mail Domain"):
            for name in frappe.get_all(doctype, {"site": ["like", "%.frappe.test"]}, pluck="name"):
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_on_trash=True)
        frappe.flags.do_not_enqueue = False

    def act_as(self, site, secret: str = "any") -> None:
        """Simulates a request that Frappe already authenticated against the Suite Site key."""

        frappe.local.suite_site = None
        frappe.set_user(frappe.db.get_single_value("Suite Cloud Settings", "site_service_user"))
        frappe.local.request = frappe._dict(headers={"Authorization": f"token {site.api_key}:{secret}"})
        frappe.local.form_dict = frappe._dict()


class TestSiteResolution(SiteApiTestCase):
    def test_site_profile_follows_the_workspace(self) -> None:
        profile = update_site_profile(title="  Acme Corp ", contact_email="Ops@Acme.test")
        self.assertEqual((profile["title"], profile["contact_email"]), ("Acme Corp", "ops@acme.test"))
        self.assertEqual(frappe.db.get_value("Suite Site", self.site.name, "title"), "Acme Corp")
        # Only the fields passed change; blanks clear the contact and reset the title to the site name.
        self.assertEqual(update_site_profile(contact_email="")["title"], "Acme Corp")
        profile = update_site_profile(title="", contact_email="")
        self.assertEqual((profile["title"], profile["contact_email"]), (self.site.name, None))
        self.assertRaises(frappe.ValidationError, update_site_profile, contact_email="not-an-address")

    def test_page_sizes_stay_between_one_and_the_cap(self) -> None:
        from suite_cloud.api.site import page_size

        self.assertEqual(page_size(0, 200), 1)  # 0 would mean "no limit" to Frappe
        self.assertEqual(page_size(-5, 200), 1)
        self.assertEqual(page_size(5000, 200), 200)
        self.assertEqual(page_size("x", 200), 200)

    def test_list_params_accept_json_text(self) -> None:
        from suite_cloud.api.site import as_list

        self.assertEqual(as_list('["a@x.com", "b@x.com"]'), ["a@x.com", "b@x.com"])
        self.assertEqual(as_list("a@x.com, b@x.com\nc@x.com"), ["a@x.com", "b@x.com", "c@x.com"])
        self.assertEqual(as_list(["a@x.com", " "]), ["a@x.com"])
        self.assertRaises(frappe.ValidationError, as_list, "[not json")

    def test_current_site_comes_from_the_authorization_header(self) -> None:
        self.assertEqual(current_site().name, self.site.name)

    def test_unknown_key_or_wrong_user_is_rejected(self) -> None:
        frappe.local.request = frappe._dict(headers={"Authorization": "token nope:secret"})
        frappe.local.suite_site = None
        self.assertRaises(SiteAuthError, current_site)

        frappe.set_user("Guest")
        frappe.local.request = frappe._dict(headers={"Authorization": f"token {self.site.api_key}:x"})
        self.assertRaises(SiteAuthError, current_site)

    def test_requests_must_come_from_an_allowed_address_when_set(self) -> None:
        self.site.allowed_ips = "203.0.113.10\n2001:db8::/32\n 10.0.0.0/8 "
        self.site.save(ignore_permissions=True)  # the fixture request runs as the service user
        self.assertEqual(self.site.allowed_ips, "203.0.113.10/32\n2001:db8::/32\n10.0.0.0/8")
        self.assertIn("allowed_ips", self.site.to_api())

        for ip in ("203.0.113.10", "2001:db8:1::5", "10.20.30.40"):
            self.act_as(self.site)
            frappe.local.request_ip = ip
            self.assertEqual(ping()["site"], self.site.name)
        for ip in ("203.0.113.11", "192.168.1.1", None, "garbage"):
            self.act_as(self.site)
            frappe.local.request_ip = ip
            self.assertRaisesRegex(SiteAddressError, "does not accept requests", ping)

        # Operators acting for the site from the desk are not the site's server.
        frappe.local.request_ip = "192.168.1.1"
        frappe.set_user("Administrator")
        frappe.local.suite_site = None
        frappe.local.form_dict = frappe._dict(site=self.site.name)
        self.assertEqual(ping()["site"], self.site.name)
        # No list means any address.
        frappe.db.set_value("Suite Site", self.site.name, "allowed_ips", "")
        frappe.clear_document_cache("Suite Site", self.site.name)
        self.act_as(self.site)
        self.assertEqual(ping()["site"], self.site.name)
        frappe.local.request_ip = None

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
        target = "suite_cloud.cloud_mail.tenancy.ownership.verify_dns_record"
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
        target = "suite_cloud.cloud_mail.tenancy.ownership.verify_dns_record"
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
    @staticmethod
    def verify(domain: str) -> None:
        """Marks the domain verified the way the DNS check would; only live domains take objects."""

        frappe.db.set_value("Mail Domain", domain, "is_verified", 1)
        frappe.clear_document_cache("Mail Domain", domain)

    def test_domain_account_group_list_flow(self) -> None:
        domain = domains.create_domain("Acme.com", description="Main")
        self.assertEqual(domain["domain"], "acme.com")
        # Timestamps leave the API as UTC so a site in another zone does not read them as local.
        self.assertRegex(domain["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(any(r["category"] == "DKIM" for r in domain["dns_records"]))
        self.assertEqual([d["domain"] for d in domains.list_domains()], ["acme.com"])
        # A Unicode domain is stored IDNA-encoded and reachable under either spelling.
        self.assertEqual(domains.create_domain("bücher.de")["domain"], "xn--bcher-kva.de")
        self.assertEqual(domains.get_domain("Bücher.de")["domain"], "xn--bcher-kva.de")
        domains.delete_domain("bücher.de")
        self.assertRaisesRegex(frappe.ValidationError, "not active", groups.create_group, "sales@acme.com")
        self.verify("acme.com")

        group = groups.create_group("sales@acme.com", description="Sales", disk_quota_gb=2)
        self.assertEqual(group["disk_quota_gb"], 2)
        self.assertEqual(groups.update_group("sales@acme.com", disk_quota_gb=3)["disk_quota_gb"], 3)
        mailing_list = mailing_lists.create_mailing_list("all@acme.com", recipients=["ext@example.org"])
        self.assertEqual((group["members"], group["description"]), ([], "Sales"))
        self.assertEqual(mailing_list["recipient_count"], 1)
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
        self.assertTrue(account["app_password"].startswith("apppassword-"))
        self.assertNotIn("app_password", accounts.get_account("alice@acme.com"))
        # Usage is read from the cluster for a single account only; a list would cost one call each.
        self.fake.find("Account", name="alice")["usedDiskQuota"] = 4096
        self.assertEqual(accounts.get_account("alice@acme.com")["used_disk_bytes"], 4096)
        # A list page fetches usage for all its rows in one cluster call.
        calls = len(self.fake.calls)
        self.assertEqual(accounts.list_accounts()["items"][0]["used_disk_bytes"], 4096)
        self.assertEqual([c[0] for c in self.fake.calls[calls:]], ["x:Account/get"])
        self.fake.find("Account", name="sales")["usedDiskQuota"] = 512
        self.assertEqual(groups.list_groups()["items"][0]["used_disk_bytes"], 512)
        self.assertEqual(groups.get_group("sales@acme.com")["used_disk_bytes"], 512)
        # The property asks the cluster only after the desk form opts in on load.
        doc = frappe.get_doc("Mail Account", "alice@acme.com")
        self.assertIsNone(doc.used_disk_bytes)
        doc.run_method("onload")
        self.assertEqual(doc.used_disk_bytes, 4096)
        # Allotments come in bulk; unknown or foreign addresses are simply absent.
        self.assertEqual(
            accounts.get_quotas(["alice@acme.com", "nobody@acme.com"]),
            {"alice@acme.com": {"disk_quota_gb": self.site.default_disk_quota_gb, "used_disk_bytes": 4096}},
        )
        rotated = accounts.rotate_app_password("alice@acme.com")["app_password"]
        self.assertNotEqual(rotated, account["app_password"])
        page = mailing_lists.list_recipients("all@acme.com")
        self.assertEqual([r["email"] for r in page["items"]], ["alice@acme.com", "ext@example.org"])
        self.assertEqual(page["total"], 2)
        self.assertEqual(
            mailing_lists.add_recipients("all@acme.com", ["x@y.org", "ext@example.org"])["added"], ["x@y.org"]
        )
        self.assertEqual(mailing_lists.remove_recipients("all@acme.com", ["x@y.org"])["recipient_count"], 2)
        self.assertEqual(groups.get_group("sales@acme.com")["members"], ["alice@acme.com"])

        page = accounts.list_accounts(search="ali")
        self.assertEqual((page["total"], [a["email"] for a in page["items"]]), (1, ["alice@acme.com"]))

        accounts.set_password("alice@acme.com", "another-pw")
        self.assertTrue(
            accounts.create_app_password("alice@acme.com", "Phone")["secret"].startswith("apppassword-")
        )
        self.assertFalse(accounts.set_account_enabled("alice@acme.com", False)["enabled"])
        self.assertEqual(accounts.set_groups("alice@acme.com", [])["groups"], [])
        self.assertEqual(accounts.get_account("alice@acme.com")["mailing_lists"], ["all@acme.com"])
        self.assertEqual(accounts.list_accounts()["items"][0]["mailing_lists"], ["all@acme.com"])
        rows = accounts.set_aliases(
            "alice@acme.com",
            [{"email": "al@acme.com", "enabled": False, "description": "old"}, "ally@acme.com"],
        )["aliases"]
        self.assertEqual(
            [(a["email"], a["enabled"], a["description"]) for a in rows],
            [("al@acme.com", False, "old"), ("ally@acme.com", True, None)],
        )
        self.assertEqual(
            groups.set_group_members("sales@acme.com", ["alice@acme.com"])["members"], ["alice@acme.com"]
        )
        self.assertEqual(mailing_lists.set_recipients("all@acme.com", ["a@b.co"])["recipient_count"], 1)
        self.assertEqual(
            [r["email"] for r in mailing_lists.list_recipients("all@acme.com")["items"]], ["a@b.co"]
        )

        accounts.delete_account("alice@acme.com")
        groups.delete_group("sales@acme.com")
        mailing_lists.delete_mailing_list("all@acme.com")
        domains.delete_domain("acme.com")
        self.assertEqual(domains.list_domains(), [])
        self.assertEqual(self.fake.all("Domain"), [])

    def test_quotas_travel_as_a_map_with_disk_always_present(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        default_bytes = int(self.site.default_disk_quota_gb * 1024**3)
        account = accounts.create_account("alice@acme.com", "secret-pw", quotas='{"maxEmails": 1000}')
        self.assertEqual(account["quotas"], {"maxEmails": 1000, "maxDiskQuota": default_bytes})
        self.assertEqual(account["disk_quota_gb"], self.site.default_disk_quota_gb)
        self.assertEqual(self.fake.find("Account", name="alice")["quotas"]["maxEmails"], 1000)

        # Replacing the optional rows keeps the disk row; disk_quota_gb changes it in GB.
        updated = accounts.update_account("alice@acme.com", quotas={"maxSieveScripts": 2}, disk_quota_gb=2)
        self.assertEqual(updated["quotas"], {"maxSieveScripts": 2, "maxDiskQuota": 2 * 1024**3})
        self.assertEqual(
            accounts.update_account("alice@acme.com", quotas={})["quotas"], {"maxDiskQuota": 2 * 1024**3}
        )
        # A maxDiskQuota inside the map works too, in bytes, and the site total still applies.
        updated = accounts.update_account("alice@acme.com", quotas={"maxDiskQuota": 3 * 1024**3})
        self.assertEqual(updated["disk_quota_gb"], 3)
        self.assertRaises(
            frappe.ValidationError, accounts.update_account, "alice@acme.com", quotas={"maxNope": 1}
        )
        self.assertRaisesRegex(
            frappe.ValidationError,
            "above 0",
            accounts.update_account,
            "alice@acme.com",
            quotas={"maxDiskQuota": 0},
        )
        self.assertEqual(accounts.get_quotas(["alice@acme.com"])["alice@acme.com"]["disk_quota_gb"], 3)

        group = groups.create_group("sales@acme.com", quotas={"maxEmails": 50})
        self.assertEqual(group["quotas"], {"maxEmails": 50, "maxDiskQuota": default_bytes})
        self.assertEqual(
            groups.update_group("sales@acme.com", quotas={})["quotas"], {"maxDiskQuota": default_bytes}
        )
        # The options a site may offer come from the cluster's schema; disk is set through disk_quota_gb.
        options = meta.get_account_options()["quotas"]
        self.assertEqual([o["value"] for o in options], ["maxEmails", "maxSieveScripts"])

    def test_domain_delivery_settings_reach_the_cluster(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        self.assertFalse(domains.get_domain("acme.com")["allow_relaying"])  # off unless asked for
        self.assertFalse(self.fake.find("Domain", name="acme.com")["allowRelaying"])

        updated = domains.update_domain("acme.com", allow_relaying=True, sub_addressing=False)
        self.assertEqual((updated["allow_relaying"], updated["sub_addressing"]), (True, False))
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual((live["allowRelaying"], live["subAddressing"]["@type"]), (True, "Disabled"))

    def test_lists_page_and_search_by_name(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        for name in ("ops", "sales", "support"):
            groups.create_group(f"{name}@acme.com", description=f"{name.title()} team")
            mailing_lists.create_mailing_list(f"{name}-news@acme.com")

        page = groups.list_groups(start=1, limit=1)
        self.assertEqual(([g["email"] for g in page["items"]], page["total"]), (["sales@acme.com"], 3))
        page = groups.list_groups(search="Support team")  # description matches too
        self.assertEqual(([g["email"] for g in page["items"]], page["total"]), (["support@acme.com"], 1))
        page = mailing_lists.list_mailing_lists(search="ops", limit=0)  # 0 is not "everything"
        self.assertEqual(([m["email"] for m in page["items"]], page["total"]), (["ops-news@acme.com"], 1))
        self.assertEqual(mailing_lists.list_mailing_lists(search="nobody")["total"], 0)

    def test_aliases_change_one_at_a_time(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("alice@acme.com", "secret-pw", aliases=["ally@acme.com"])
        groups.create_group("sales@acme.com")
        mailing_lists.create_mailing_list("all@acme.com")

        rows = accounts.add_alias("alice@acme.com", "Al@Acme.com", description="short")["aliases"]
        self.assertEqual(
            [(a["email"], a["description"]) for a in rows],
            [("ally@acme.com", None), ("al@acme.com", "short")],
        )
        # Adding it again changes nothing, and the cluster sees the same set once.
        self.assertEqual(len(accounts.add_alias("alice@acme.com", "al@acme.com")["aliases"]), 2)
        stored = self.fake.find("Account", name="alice")
        self.assertEqual(sorted(a["name"] for a in stored["aliases"].values()), ["al", "ally"])

        rows = accounts.set_alias_enabled("alice@acme.com", "al@acme.com", False)["aliases"]
        self.assertEqual(
            [(a["email"], a["enabled"]) for a in rows], [("ally@acme.com", True), ("al@acme.com", False)]
        )
        self.assertRaises(
            frappe.DoesNotExistError, accounts.set_alias_enabled, "alice@acme.com", "x@acme.com", True
        )
        self.assertRaisesRegex(
            frappe.ValidationError,
            "primary address",
            accounts.remove_alias,
            "alice@acme.com",
            "alice@acme.com",
        )
        self.assertRaisesRegex(
            frappe.ValidationError, "primary address", accounts.add_alias, "alice@acme.com", "alice@acme.com"
        )
        self.assertEqual(
            [a["email"] for a in accounts.remove_alias("alice@acme.com", "ally@acme.com")["aliases"]],
            ["al@acme.com"],
        )
        self.assertEqual(
            len(accounts.remove_alias("alice@acme.com", "ally@acme.com")["aliases"]), 1
        )  # already gone

        self.assertEqual(
            [a["email"] for a in groups.add_group_alias("sales@acme.com", "team@acme.com")["aliases"]],
            ["team@acme.com"],
        )
        self.assertFalse(
            groups.set_group_alias_enabled("sales@acme.com", "team@acme.com", "0")["aliases"][0]["enabled"]
        )
        self.assertEqual(groups.remove_group_alias("sales@acme.com", "team@acme.com")["aliases"], [])
        self.assertEqual(
            [
                a["email"]
                for a in mailing_lists.add_mailing_list_alias("all@acme.com", "everyone@acme.com")["aliases"]
            ],
            ["everyone@acme.com"],
        )
        self.assertFalse(
            mailing_lists.set_mailing_list_alias_enabled("all@acme.com", "everyone@acme.com", False)[
                "aliases"
            ][0]["enabled"]
        )
        self.assertEqual(
            mailing_lists.remove_mailing_list_alias("all@acme.com", "everyone@acme.com")["aliases"], []
        )
        # An alias that is taken elsewhere is refused, whichever object asks for it.
        self.assertRaises(frappe.DuplicateEntryError, groups.add_group_alias, "sales@acme.com", "al@acme.com")

    def test_other_sites_objects_are_invisible(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
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
        self.verify("acme.com")
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
            result = fc.create_site(
                "New.Frappe.Test", region="blr", fc_reference="site-42", contact_email="Ops@New.Test"
            )

        self.assertEqual(result["site"], "new.frappe.test")
        self.assertEqual((result["title"], result["contact_email"]), ("new.frappe.test", "ops@new.test"))
        updated = fc.update_site(
            "new.frappe.test", title="New Co", contact_email="admin@new.test", max_groups=7
        )
        self.assertEqual(
            (updated["title"], updated["contact_email"], updated["limits"]["max_groups"]),
            ("New Co", "admin@new.test", 7),
        )
        self.assertRaises(
            frappe.ValidationError, fc.update_site, "new.frappe.test", contact_email="not-an-address"
        )
        # Frappe Cloud passes the hosting server's outbound addresses; bad ones are refused.
        self.assertEqual(
            fc.update_site("new.frappe.test", allowed_ips=["203.0.113.10", "10.0.0.0/8"])["allowed_ips"],
            ["203.0.113.10/32", "10.0.0.0/8"],
        )
        self.assertEqual(fc.update_site("new.frappe.test", allowed_ips=[])["allowed_ips"], [])
        self.assertRaisesRegex(
            frappe.ValidationError, "not an IP", fc.update_site, "new.frappe.test", allowed_ips=["nope"]
        )
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
        self.assertRaisesRegex(
            frappe.ValidationError, "negative", fc.update_site, self.site.name, max_domains=-1
        )
        # Archived is final: neither suspend nor resume may bring the site back.
        self.assertRaisesRegex(frappe.ValidationError, "archived", fc.suspend_site, self.site.name)

    def test_cluster_selection(self) -> None:
        self.assertEqual(fc.pick_cluster(None, None), self.cluster.name)
        self.assertEqual(fc.pick_cluster(None, "BLR"), self.cluster.name)
        self.assertEqual(fc.pick_cluster(None, "sfo"), self.cluster.name)  # falls back to the default
        self.assertRaisesRegex(frappe.ValidationError, "not active", fc.pick_cluster, "missing", None)

        # Without a default, a region nobody serves is refused unless some cluster serves all regions.
        # The site may hold real clusters serving "blr", so the fixture gets a region of its own.
        self.cluster.db_set("is_default", 0)
        self.cluster.append("regions", {"region": "fixture-only"})
        self.cluster.save()
        self.assertRaisesRegex(
            frappe.ValidationError, "serves region nowhere", fc.pick_cluster, None, "nowhere"
        )
        self.assertEqual(fc.pick_cluster(None, "FIXTURE-ONLY"), self.cluster.name)
        self.cluster.db_set("is_default", 1)
