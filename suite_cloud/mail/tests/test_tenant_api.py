from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api.mail import accounts, domains, groups, mailing_lists, meta
from suite_cloud.api.tenant import (
    ClusterUnavailableError,
    TenantAddressError,
    TenantAuthError,
    TenantSuspendedError,
    authenticate,
    current_tenant,
    get_disk_usage,
    ping,
    update_tenant_profile,
)
from suite_cloud.mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.mail.doctype.mail_domain.mail_domain import verify_unverified_domains
from suite_cloud.mail.stalwart import forget_sessions
from suite_cloud.mail.stalwart.errors import StalwartUnavailableError
from suite_cloud.mail.stalwart.service import ManagementService
from suite_cloud.mail.tenancy.ownership import (
    VALUE_PREFIX,
    DomainNotVerifiedError,
    OwnershipLookupError,
)
from suite_cloud.mail.tests.fake_stalwart import FakeStalwart
from suite_cloud.mail.tests.fixtures import (
    activate_cluster,
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_tenant,
    verified_ownership,
)
from suite_cloud.suite_cloud.doctype.suite_tenant.suite_tenant import MailNotConfiguredError

MAIL_DOMAIN = "suite_cloud.mail.doctype.mail_domain.mail_domain"
SUITE_TENANT = "suite_cloud.suite_cloud.doctype.suite_tenant.suite_tenant"


class TenantApiTestCase(IntegrationTestCase):
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
        self.tenant = make_tenant(self.cluster)
        self.other = make_tenant(self.cluster, "other.frappe.test")
        self.act_as(self.tenant)

    def tearDown(self) -> None:
        frappe.local.suite_tenant = None
        frappe.local.request = None
        frappe.set_user("Administrator")
        # Only the fixture sites: the dev site holds real accounts, and deleting those even
        # inside the rolled-back transaction serialises each one, which asks the live cluster.
        tenants = frappe.get_all("Suite Tenant", {"title": ["like", "%.frappe.test"]}, pluck="name")
        for doctype in ("Mail Account", "Mail Group", "Mailing List", "Mail Domain"):
            for name in frappe.get_all(doctype, {"tenant": ["in", tenants]}, pluck="name"):
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_on_trash=True)
        frappe.flags.do_not_enqueue = False

    def act_as(self, tenant, secret: str = "any") -> None:
        """Simulates a request the auth hook already signed in for the tenant."""

        frappe.local.suite_tenant = None
        frappe.set_user(tenant.user)
        frappe.utils.set_request(headers={"Authorization": f"Tenant {tenant.name}:{secret}"})
        frappe.local.form_dict = frappe._dict()

    @staticmethod
    def verify(domain: str) -> None:
        """Marks the domain verified the way the DNS check would; only live domains take objects."""

        frappe.db.set_value("Mail Domain", domain, "is_verified", 1)
        frappe.clear_document_cache("Mail Domain", domain)


class TestTenantResolution(TenantApiTestCase):
    def test_disk_usage_is_summed_in_one_call_and_cached(self) -> None:
        key = f"suite_cloud:disk-usage:{self.tenant.name}"
        frappe.cache.delete_value(key)
        self.addCleanup(frappe.cache.delete_value, key)
        with patch(f"{SUITE_TENANT}.get_client") as get_client:
            self.assertEqual(get_disk_usage()["used_disk_bytes"], 0)
        get_client.assert_not_called()  # nothing to ask, so not even the session is fetched

        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("alice@acme.com", "secret-pw")
        groups.create_group("sales@acme.com")
        self.fake.find("Account", name="alice")["usedDiskQuota"] = 4096
        self.fake.find("Account", name="sales")["usedDiskQuota"] = 512
        calls = len(self.fake.calls)
        usage = get_disk_usage(fresh=True)
        self.assertEqual(usage["used_disk_bytes"], 4608)
        self.assertIsNotNone(frappe.utils.get_datetime(usage["fetched_at"]))
        self.assertEqual([c[0] for c in self.fake.calls[calls:]], ["x:Account/get"])
        # The cached figure is served until it expires or the caller asks for a fresh one.
        self.fake.find("Account", name="alice")["usedDiskQuota"] = 8192
        self.assertEqual(get_disk_usage(), usage)
        self.assertEqual(len(self.fake.calls), calls + 1)
        self.assertEqual(get_disk_usage(fresh="1")["used_disk_bytes"], 8704)
        self.assertEqual(get_disk_usage()["used_disk_bytes"], 8704)
        # The desk form shows the same figure; nothing else that reads the document asks for it.
        doc = frappe.get_doc("Suite Tenant", self.tenant.name)
        self.assertIsNone(doc.mail_used_disk_bytes)
        doc.run_method("onload")
        self.assertEqual(doc.mail_used_disk_bytes, 8704)

    def test_disk_usage_is_not_cached_when_the_cluster_cannot_answer(self) -> None:
        key = f"suite_cloud:disk-usage:{self.tenant.name}"
        frappe.cache.delete_value(key)
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("alice@acme.com", "secret-pw")
        with patch.object(ManagementService, "get_many", side_effect=StalwartUnavailableError()):
            self.assertRaises(ClusterUnavailableError, get_disk_usage)
            # The form still opens; the figure is simply absent.
            doc = frappe.get_doc("Suite Tenant", self.tenant.name)
            doc.run_method("onload")
            self.assertIsNone(doc.mail_used_disk_bytes)
        self.assertIsNone(frappe.cache.get_value(key, expires=True))

    def test_site_profile_follows_the_workspace(self) -> None:
        profile = update_tenant_profile(title="  Acme Corp ", contact_email="Ops@Acme.test")
        self.assertEqual((profile["title"], profile["contact_email"]), ("Acme Corp", "ops@acme.test"))
        self.assertEqual(frappe.db.get_value("Suite Tenant", self.tenant.name, "title"), "Acme Corp")
        # Only the fields passed change; a blank puts the contact back to the owner's email.
        profile = update_tenant_profile(contact_email="")
        self.assertEqual((profile["title"], profile["contact_email"]), ("Acme Corp", self.tenant.user))
        # A notice address follows the general email rules, not the mail server's local-part rules.
        self.assertEqual(
            update_tenant_profile(contact_email="ops/team@acme.test")["contact_email"], "ops/team@acme.test"
        )
        self.assertRaisesRegex(
            frappe.ValidationError,
            "not a valid email address",
            update_tenant_profile,
            contact_email="not-an-address",
        )
        self.act_as(self.tenant)  # a refused save leaves the request's cached tenant dirty
        self.assertRaises(frappe.ValidationError, update_tenant_profile, title="")

    def test_page_sizes_stay_between_one_and_the_cap(self) -> None:
        from suite_cloud.api.tenant import page_size

        self.assertEqual(page_size(0, 200), 1)  # 0 would mean "no limit" to Frappe
        self.assertEqual(page_size(-5, 200), 1)
        self.assertEqual(page_size(5000, 200), 200)
        self.assertEqual(page_size("x", 200), 200)

    def test_list_params_are_json_arrays_or_their_text(self) -> None:
        from suite_cloud.api.tenant import InvalidInputError, as_alias_rows, as_list

        self.assertEqual(as_list('["a@x.com", "b@x.com"]'), ["a@x.com", "b@x.com"])
        self.assertEqual(as_list(["a@x.com", " "]), ["a@x.com"])
        self.assertEqual(as_list(None), [])
        self.assertEqual(as_alias_rows('[{"email": "a@x.com", "enabled": false}]')[0]["enabled"], 0)
        for text in ("a@x.com, b@x.com", "a@x.com", "", '{"email": "a@x.com"}'):
            self.assertRaises(InvalidInputError, as_list, text)

    def test_current_tenant_comes_from_the_authorization_header(self) -> None:
        self.assertEqual(current_tenant().name, self.tenant.name)

    def test_unknown_tenant_or_wrong_user_is_rejected(self) -> None:
        frappe.utils.set_request(headers={"Authorization": "Tenant nope:secret"})
        frappe.local.suite_tenant = None
        self.assertRaises(TenantAuthError, current_tenant)

        frappe.set_user("Guest")
        frappe.utils.set_request(headers={"Authorization": f"Tenant {self.tenant.name}:x"})
        self.assertRaises(TenantAuthError, current_tenant)

    def test_the_auth_hook_checks_the_tenant_secret(self) -> None:
        frappe.set_user("Administrator")
        secret = self.tenant.rotate_secret()
        frappe.set_user("Guest")

        frappe.utils.set_request(headers={"Authorization": "token a:b"})
        authenticate()  # another scheme is left to Frappe
        self.assertEqual(frappe.session.user, "Guest")

        for credential in (f"{self.tenant.name}:wrong", f"nope:{secret}", self.tenant.name):
            frappe.utils.set_request(headers={"Authorization": f"Tenant {credential}"})
            self.assertRaisesRegex(frappe.AuthenticationError, "authentication failed", authenticate)
            self.assertEqual(frappe.session.user, "Guest")

        frappe.utils.set_request(headers={"Authorization": f"Tenant {self.tenant.name}:{secret}"})
        authenticate()
        self.assertEqual(frappe.session.user, self.tenant.user)

        self.tenant.db_set("enabled", 0)
        frappe.set_user("Guest")
        self.assertRaises(frappe.AuthenticationError, authenticate)

    def test_mail_endpoints_wait_for_a_cluster(self) -> None:
        tenant = frappe.get_doc({"doctype": "Suite Tenant", "title": "new.frappe.test"}).insert()
        self.act_as(tenant)
        self.assertIsNone(ping()["cluster"])
        with self.assertRaises(MailNotConfiguredError) as ctx:
            domains.list_domains()
        self.assertEqual(ctx.exception.http_status_code, 422)

    def test_requests_must_come_from_an_allowed_address_when_set(self) -> None:
        self.tenant.allowed_ips = "203.0.113.10\n2001:db8::/32\n 10.0.0.0/8 "
        self.tenant.save(ignore_permissions=True)  # the fixture request runs as the tenant's owner
        self.assertEqual(self.tenant.allowed_ips, "203.0.113.10/32\n2001:db8::/32\n10.0.0.0/8")
        self.assertIn("allowed_ips", self.tenant.to_api())

        for ip in ("203.0.113.10", "2001:db8:1::5", "10.20.30.40"):
            self.act_as(self.tenant)
            frappe.local.request_ip = ip
            self.assertEqual(ping()["tenant"], self.tenant.name)
        for key in frappe.cache.get_keys("suite_cloud:refused:*"):
            frappe.cache.delete_value(key)
        mock_log = patch("suite_cloud.api.tenant.frappe.log_error").start()
        self.addCleanup(patch.stopall)
        for ip in ("203.0.113.11", "192.168.1.1", None, "garbage"):
            self.act_as(self.tenant)
            frappe.local.request_ip = ip
            self.assertRaisesRegex(TenantAddressError, "does not accept requests", ping)
        # Each refusal leaves an Error Log naming the site and the address, for operators to act
        # on; it is written on the side (deferred) so the refusal's own rollback keeps it, and a
        # flood from one address is logged once per window.
        frappe.local.request_ip = "192.168.1.1"
        self.act_as(self.tenant)
        self.assertRaisesRegex(TenantAddressError, "does not accept requests", ping)
        logged = [m for m in mock_log.call_args_list if "outside its allowed list" in m.kwargs["title"]]
        self.assertEqual(len(logged), 4)
        self.assertIn("garbage", logged[3].kwargs["message"])
        self.assertTrue(all(m.kwargs["defer_insert"] for m in logged))

        # Operators acting for the site from the desk are not the site's server.
        frappe.local.request_ip = "192.168.1.1"
        frappe.set_user("Administrator")
        frappe.local.suite_tenant = None
        frappe.local.form_dict = frappe._dict(tenant=self.tenant.name)
        self.assertEqual(ping()["tenant"], self.tenant.name)
        # No list means any address.
        frappe.db.set_value("Suite Tenant", self.tenant.name, "allowed_ips", "")
        frappe.clear_document_cache("Suite Tenant", self.tenant.name)
        self.act_as(self.tenant)
        self.assertEqual(ping()["tenant"], self.tenant.name)
        frappe.local.request_ip = None

    def test_suspended_site_is_refused(self) -> None:
        self.tenant.db_set({"enabled": 0, "status": "Suspended"})
        frappe.clear_document_cache("Suite Tenant", self.tenant.name)
        frappe.local.suite_tenant = None
        self.assertRaises(TenantSuspendedError, current_tenant)

    def test_managers_can_act_for_a_site(self) -> None:
        frappe.set_user("Administrator")
        frappe.local.suite_tenant = None
        frappe.utils.set_request(headers={})
        frappe.local.form_dict = frappe._dict(tenant=self.other.name)
        self.assertEqual(current_tenant().name, self.other.name)


class TestDomainOwnership(TenantApiTestCase):
    def test_check_domain_hands_out_the_site_record(self) -> None:
        result = domains.check_domain("Acme.com")
        record = result["ownership_record"]
        self.assertEqual(result["domain"], "acme.com")
        self.assertEqual((record["type"], record["host"], record["fqdn"]), ("TXT", "@", "acme.com"))
        self.assertEqual(record["value"], f"{VALUE_PREFIX}{self.tenant.name}")
        # The same record for every domain of the site, a different one per site.
        self.assertEqual(domains.check_domain("other.com")["ownership_record"]["value"], record["value"])
        self.act_as(self.other)
        self.assertNotEqual(domains.check_domain("acme.com")["ownership_record"]["value"], record["value"])

    def test_check_domain_does_not_tell_who_holds_a_domain(self) -> None:
        # Another site holds taken.com; asking about it only yields the record to publish.
        self.act_as(self.other)
        domains.create_domain("taken.com")
        self.act_as(self.tenant)
        answer = domains.check_domain("taken.com")
        self.assertEqual(answer["ownership_record"]["fqdn"], "taken.com")
        # Only a proven owner learns that it is not available; without the record the answer
        # is the same as for any unproven domain.
        with patch("suite_cloud.mail.tenancy.ownership.verify_ownership", return_value=False):
            self.assertRaisesRegex(
                frappe.ValidationError, "Publish a TXT record", domains.create_domain, "taken.com"
            )
        self.assertRaisesRegex(
            frappe.DuplicateEntryError, "not available", domains.create_domain, "taken.com"
        )

    def test_a_cloud_that_skips_domain_verification_takes_the_tenants_word(self) -> None:
        # A development cloud's test domains exist in no public DNS; the operator vouches for them.
        frappe.set_user("Administrator")
        configure_settings(skip_domain_verification=1)
        self.act_as(self.tenant)

        with patch("suite_cloud.mail.tenancy.ownership.verify_dns_record", return_value=False):
            domains.create_domain("acme.com")
        domain = frappe.get_doc("Mail Domain", "acme.com")
        self.assertTrue(domain.is_live())
        self.assertTrue(domain.skip_scheduled_verification)
        self.assertTrue(self.fake.all("Domain")[0]["isEnabled"])

        # Asking to verify does not take the word back, whatever the resolvers say.
        target = f"{MAIL_DOMAIN}.verify_dns_record"
        with patch(target, return_value=False):
            self.assertTrue(domains.verify_dns_records("acme.com")["is_verified"])

        # Switched back on, the domain taken on the cloud's word is checked like any other - and
        # goes offline, having no records - and the next domain has to prove control again.
        frappe.set_user("Administrator")
        with patch(target, return_value=False):
            configure_settings(skip_domain_verification=0)
        domain.reload()
        self.assertFalse(domain.is_live())
        self.assertFalse(domain.verification_skipped or domain.skip_scheduled_verification)
        self.assertFalse(self.fake.all("Domain")[0]["isEnabled"])
        self.act_as(self.tenant)
        with patch("suite_cloud.mail.tenancy.ownership.verify_dns_record", return_value=False):
            self.assertRaises(DomainNotVerifiedError, domains.create_domain, "other.com")

    def test_the_hourly_check_catches_a_vouched_domain_the_recheck_missed(self) -> None:
        # Vouched for while a recheck job was already running, say: the job's list never had it.
        frappe.set_user("Administrator")
        configure_settings(skip_domain_verification=1)
        self.act_as(self.tenant)
        domains.create_domain("acme.com")
        frappe.set_user("Administrator")
        with patch(f"{MAIL_DOMAIN}.recheck_vouched_domains"):
            configure_settings(skip_domain_verification=0)
        domain = frappe.get_doc("Mail Domain", "acme.com")
        self.assertTrue(domain.is_live() and domain.verification_skipped)

        with patch(f"{MAIL_DOMAIN}.verify_dns_record", return_value=False):
            verify_unverified_domains()
        domain.reload()
        self.assertFalse(domain.is_live())
        self.assertFalse(domain.verification_skipped or domain.skip_scheduled_verification)

    def test_domain_is_added_only_once_its_record_resolves(self) -> None:
        target = "suite_cloud.mail.tenancy.ownership.verify_dns_record"
        with patch(target, return_value=False):
            self.assertRaisesRegex(
                DomainNotVerifiedError, self.tenant.name, domains.create_domain, "acme.com"
            )
        with patch(target, return_value=None):
            self.assertRaises(OwnershipLookupError, domains.create_domain, "acme.com")
        self.assertFalse(frappe.db.exists("Mail Domain", "acme.com"))
        self.assertEqual(self.fake.all("Domain"), [])

        with patch(target, return_value=True) as verify:
            domains.create_domain("acme.com")
        verify.assert_called_once_with("acme.com", "TXT", f"{VALUE_PREFIX}{self.tenant.name}")
        self.assertRaisesRegex(frappe.DuplicateEntryError, "already added", domains.check_domain, "acme.com")
        self.act_as(self.other)
        self.assertEqual(
            domains.check_domain("acme.com")["domain"], "acme.com"
        )  # tells the other site nothing
        with patch(target, return_value=True):
            self.assertRaisesRegex(
                frappe.DuplicateEntryError, "not available", domains.create_domain, "acme.com"
            )

    def test_operators_add_domains_without_the_record(self) -> None:
        target = "suite_cloud.mail.tenancy.ownership.verify_dns_record"
        frappe.set_user("Administrator")
        with patch(target, return_value=False) as verify:
            frappe.get_doc(
                {"doctype": "Mail Domain", "domain_name": "acme.com", "tenant": self.tenant.name}
            ).insert()
        verify.assert_not_called()

        self.act_as(self.tenant)
        with patch(target, return_value=False):
            self.assertRaises(DomainNotVerifiedError, domains.create_domain, "other.com")


class TestDirectoryApi(TenantApiTestCase):
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
        # Quotas come in bulk, 0 for an unlimited mailbox; unknown or foreign addresses are absent.
        self.assertEqual(
            accounts.get_quotas(["alice@acme.com", "nobody@acme.com"]),
            {"alice@acme.com": {"disk_quota_gb": 0, "used_disk_bytes": 4096}},
        )
        rotated = accounts.rotate_app_password("alice@acme.com")["app_password"]
        self.assertNotEqual(rotated, account["app_password"])
        page = mailing_lists.list_recipients("all@acme.com")
        self.assertEqual([r["email"] for r in page["items"]], ["alice@acme.com", "ext@example.org"])
        self.assertEqual(page["total"], 2)
        # The site's whole directory of recipients comes in one listing, paged across lists.
        mailing_lists.create_mailing_list("dev@acme.com", recipients=["bob@example.org"])
        self.assertEqual(
            mailing_lists.list_all_recipients(),
            {
                "items": [
                    {"mailing_list": "all@acme.com", "email": "alice@acme.com", "enabled": True},
                    {"mailing_list": "all@acme.com", "email": "ext@example.org", "enabled": True},
                    {"mailing_list": "dev@acme.com", "email": "bob@example.org", "enabled": True},
                ],
                "total": 3,
            },
        )
        page = mailing_lists.list_all_recipients(start=2, limit=1)
        self.assertEqual(([r["email"] for r in page["items"]], page["total"]), (["bob@example.org"], 3))
        self.act_as(self.other)
        self.assertEqual(mailing_lists.list_all_recipients(), {"items": [], "total": 0})
        self.act_as(self.tenant)
        mailing_lists.delete_mailing_list("dev@acme.com")
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
            groups.set_members("sales@acme.com", ["alice@acme.com"])["members"], ["alice@acme.com"]
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

    def test_app_passwords_are_managed_per_account(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        account = accounts.create_account("alice@acme.com", "secret-pw")
        self.assertTrue(account["app_password"].startswith("apppassword-"))
        self.assertEqual(account["quotas"]["maxAppPasswords"], 10)
        listed = accounts.list_app_passwords("alice@acme.com")["items"]
        self.assertEqual([p["description"] for p in listed], ["Suite Cloud"])
        self.assertNotIn("secret", listed[0])

        created = accounts.create_app_password(
            "alice@acme.com", "Phone", expires_at="2027-01-01T00:00:00Z", allowed_ips=["203.0.113.0/24"]
        )
        self.assertEqual(frappe.local.response["http_status_code"], 201)
        self.assertTrue(created["secret"].startswith("apppassword-"))
        self.assertEqual(
            (created["description"], created["expires_at"], created["allowed_ips"]),
            ("Phone", "2027-01-01T00:00:00Z", ["203.0.113.0/24"]),
        )
        stalwart_id = frappe.db.get_value("Mail Account", "alice@acme.com", "stalwart_id")
        live = self.fake.objects[f"AppPassword:{stalwart_id}"]
        self.assertEqual(live[created["id"]]["expiresAt"], "2027-01-01T00:00:00Z")

        updated = accounts.update_app_password(
            "alice@acme.com", created["id"], description="Old phone", allowed_ips=[]
        )
        self.assertEqual(
            (updated["description"], updated["allowed_ips"], updated["expires_at"]),
            ("Old phone", [], "2027-01-01T00:00:00Z"),
        )
        self.assertEqual(
            (live[created["id"]]["description"], live[created["id"]]["allowedIps"]), ("Old phone", [])
        )

        # The site's own login is rotated, not deleted; the others go for good.
        self.assertRaises(
            frappe.ValidationError, accounts.delete_app_password, "alice@acme.com", listed[0]["id"]
        )
        self.assertRaises(frappe.DoesNotExistError, accounts.delete_app_password, "alice@acme.com", "nope")
        accounts.delete_app_password("alice@acme.com", created["id"])
        self.assertEqual(frappe.local.response["http_status_code"], 204)
        frappe.db.after_commit.run()
        self.assertNotIn(created["id"], live)
        rotated = accounts.rotate_app_password("alice@acme.com")["app_password"]
        self.assertNotEqual(rotated, account["app_password"])
        frappe.db.after_commit.run()
        self.assertEqual([c["description"] for c in live.values()], ["Suite Cloud"])

        # A credential made outside Suite Cloud is listed, and its id works for a change or a revoke.
        live["ap-watch"] = {"id": "ap-watch", "description": "Watch", "createdAt": "2026-02-01T00:00:00Z"}
        self.assertIn("ap-watch", [p["id"] for p in accounts.list_app_passwords("alice@acme.com")["items"]])
        self.assertEqual(
            accounts.update_app_password("alice@acme.com", "ap-watch", description="Old watch")[
                "description"
            ],
            "Old watch",
        )
        self.assertEqual(live["ap-watch"]["description"], "Old watch")
        accounts.delete_app_password("alice@acme.com", "ap-watch")
        frappe.db.after_commit.run()
        self.assertNotIn("ap-watch", live)

        # The default limit counts the site's login; another site sees nothing.
        for number in range(9):
            accounts.create_app_password("alice@acme.com", f"Device {number}")
        self.assertRaisesRegex(
            frappe.ValidationError,
            "at most 10",
            accounts.create_app_password,
            "alice@acme.com",
            "One too many",
        )
        self.act_as(self.other)
        self.assertRaises(frappe.DoesNotExistError, accounts.list_app_passwords, "alice@acme.com")

    def test_api_keys_are_managed_per_account_except_the_reserved_one(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("alice@acme.com", "secret-pw")
        self.assertEqual(accounts.list_api_keys("alice@acme.com")["items"], [])
        self.assertRaisesRegex(
            frappe.ValidationError, "reserved", accounts.create_api_key, "alice@acme.com", "Suite Cloud"
        )

        created = accounts.create_api_key("alice@acme.com", "Reporting", allowed_ips='["198.51.100.7"]')
        self.assertEqual(frappe.local.response["http_status_code"], 201)
        self.assertTrue(created["secret"].startswith("apikey-"))
        self.assertEqual(created["allowed_ips"], ["198.51.100.7"])
        self.assertIn(created["secret"], self.fake.tokens)
        updated = accounts.update_api_key(
            "alice@acme.com", created["id"], description="Reports", allowed_ips=[]
        )
        self.assertEqual((updated["description"], updated["allowed_ips"]), ("Reports", []))
        self.assertRaisesRegex(
            frappe.ValidationError,
            "reserved",
            accounts.update_api_key,
            "alice@acme.com",
            created["id"],
            description="Suite Cloud",
        )
        listed = accounts.list_api_keys("alice@acme.com")["items"]
        self.assertEqual([k["description"] for k in listed], ["Reports"])
        accounts.delete_api_key("alice@acme.com", created["id"])
        self.assertEqual(frappe.local.response["http_status_code"], 204)
        frappe.db.after_commit.run()
        self.assertEqual(accounts.list_api_keys("alice@acme.com")["items"], [])

        for number in range(10):
            accounts.create_api_key("alice@acme.com", f"Key {number}")
        self.assertRaisesRegex(
            frappe.ValidationError, "at most 10", accounts.create_api_key, "alice@acme.com", "One too many"
        )

    def test_quotas_travel_as_a_map_with_disk_optional(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        account = accounts.create_account("alice@acme.com", "secret-pw", quotas='{"maxEmails": 1000}')
        self.assertEqual(account["quotas"], {"maxEmails": 1000, "maxAppPasswords": 10, "maxApiKeys": 10})
        self.assertEqual(account["disk_quota_gb"], 0)  # none asked for, so none: unlimited
        # A credential limit lifted on creation stays lifted; the default fills only what is unnamed.
        lifted = accounts.create_account("bob@acme.com", "secret-pw", quotas={"maxAppPasswords": 0})
        self.assertEqual(lifted["quotas"], {"maxApiKeys": 10})
        self.assertEqual(self.fake.find("Account", name="alice")["quotas"]["maxEmails"], 1000)

        # Replacing the other rows keeps the disk row; disk_quota_gb changes it in GB.
        updated = accounts.update_account("alice@acme.com", quotas={"maxSieveScripts": 2}, disk_quota_gb=2)
        self.assertEqual(updated["quotas"], {"maxSieveScripts": 2, "maxDiskQuota": 2 * 1024**3})
        self.assertEqual(
            accounts.update_account("alice@acme.com", quotas={})["quotas"], {"maxDiskQuota": 2 * 1024**3}
        )
        # A maxDiskQuota inside the map works too, in bytes; 0 in either place lifts the limit.
        updated = accounts.update_account("alice@acme.com", quotas={"maxDiskQuota": 3 * 1024**3})
        self.assertEqual(updated["disk_quota_gb"], 3)
        self.assertRaises(
            frappe.ValidationError, accounts.update_account, "alice@acme.com", quotas={"maxNope": 1}
        )
        self.assertEqual(accounts.get_quotas(["alice@acme.com"])["alice@acme.com"]["disk_quota_gb"], 3)
        self.assertEqual(accounts.update_account("alice@acme.com", quotas={"maxDiskQuota": 0})["quotas"], {})
        self.assertEqual(accounts.update_account("alice@acme.com", disk_quota_gb=4)["disk_quota_gb"], 4)
        self.assertEqual(accounts.update_account("alice@acme.com", disk_quota_gb=0)["disk_quota_gb"], 0)
        self.assertNotIn("maxDiskQuota", self.fake.find("Account", name="alice")["quotas"])
        self.assertEqual(accounts.get_quotas(["alice@acme.com"])["alice@acme.com"]["disk_quota_gb"], 0)

        group = groups.create_group("sales@acme.com", quotas={"maxEmails": 50}, disk_quota_gb=1)
        self.assertEqual(group["quotas"], {"maxEmails": 50, "maxDiskQuota": 1024**3})
        self.assertEqual(
            groups.update_group("sales@acme.com", quotas={})["quotas"], {"maxDiskQuota": 1024**3}
        )
        # The options a site may offer come from the cluster's schema; disk is set through disk_quota_gb.
        options = meta.get_account_options()["quotas"]
        self.assertEqual([o["value"] for o in options], ["maxEmails", "maxSieveScripts"])

    def test_an_account_can_be_created_unable_to_receive(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")

        account = accounts.create_account("noreply@acme.com", "secret-pw", disable_receiving=True)
        self.assertTrue(account["disable_receiving"])
        # "Email: Receive emails" taken away on top of the roles, in the shape Stalwart 0.16 takes.
        self.assertEqual(
            self.fake.find("Account", name="noreply")["permissions"],
            {"@type": "Merge", "enabledPermissions": {}, "disabledPermissions": {"emailReceive": True}},
        )
        self.assertTrue(accounts.list_accounts()["items"][0]["disable_receiving"])

        # Unasked, an account keeps everything its roles grant.
        self.assertFalse(accounts.create_account("alice@acme.com", "secret-pw")["disable_receiving"])
        self.assertEqual(self.fake.find("Account", name="alice")["permissions"], {"@type": "Inherit"})

        # An operator lets it receive again from the form, and the cluster follows.
        doc = frappe.get_doc("Mail Account", "noreply@acme.com")
        doc.disable_receiving = 0
        doc.save(ignore_permissions=True)
        self.assertEqual(self.fake.find("Account", name="noreply")["permissions"], {"@type": "Inherit"})
        self.assertFalse(accounts.get_account("noreply@acme.com")["disable_receiving"])

    def test_a_site_stops_and_restores_an_accounts_receiving(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("alice@acme.com", "secret-pw")
        live = lambda: self.fake.find("Account", name="alice")["permissions"]  # noqa: E731

        self.assertTrue(
            accounts.update_account("alice@acme.com", disable_receiving=True)["disable_receiving"]
        )
        self.assertEqual(
            live(),
            {"@type": "Merge", "enabledPermissions": {}, "disabledPermissions": {"emailReceive": True}},
        )
        # An update that says nothing about receiving leaves it as it is.
        self.assertTrue(accounts.update_account("alice@acme.com", display_name="Alice")["disable_receiving"])
        self.assertEqual(live()["disabledPermissions"], {"emailReceive": True})

        self.assertFalse(
            accounts.update_account("alice@acme.com", disable_receiving=False)["disable_receiving"]
        )
        self.assertEqual(live(), {"@type": "Inherit"})

    def test_a_group_can_be_kept_from_receiving_and_let_receive_again(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        live = lambda name: self.fake.find("Account", name=name)["permissions"]  # noqa: E731
        denied = {"@type": "Merge", "enabledPermissions": {}, "disabledPermissions": {"emailReceive": True}}

        # Created unable to receive, and listed as such; another group is left to inherit.
        self.assertTrue(groups.create_group("noreply@acme.com", disable_receiving=True)["disable_receiving"])
        self.assertEqual(live("noreply"), denied)
        self.assertTrue(groups.list_groups()["items"][0]["disable_receiving"])
        self.assertFalse(groups.create_group("sales@acme.com")["disable_receiving"])
        self.assertEqual(live("sales"), {"@type": "Inherit"})

        # Changed on an existing group, in the same update as the rest of the save.
        calls = len(self.fake.calls)
        updated = groups.update_group("sales@acme.com", description="Sales", disable_receiving=True)
        self.assertTrue(updated["disable_receiving"])
        sales_id = frappe.db.get_value("Mail Group", "sales@acme.com", "stalwart_id")
        updates = [args["update"] for name, args in self.fake.calls[calls:] if name == "x:Account/set"]
        self.assertEqual(updates, [{sales_id: {"description": "Sales", "permissions": denied}}])
        # An update that says nothing about receiving leaves it as it is.
        self.assertTrue(groups.update_group("sales@acme.com", description="Sales team")["disable_receiving"])
        self.assertEqual(live("sales"), denied)

        self.assertFalse(groups.update_group("sales@acme.com", disable_receiving=False)["disable_receiving"])
        self.assertEqual(live("sales"), {"@type": "Inherit"})

    def test_changing_receiving_leaves_an_accounts_other_permissions_alone(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("alice@acme.com", "secret-pw")
        # As an adopted account may carry them: a list of its own, set by hand on the cluster.
        self.fake.find("Account", name="alice")["permissions"] = {
            "@type": "Replace",
            "enabledPermissions": {"authenticate": True, "emailReceive": True},
            "disabledPermissions": {"sysAccountGet": True},
        }

        doc = frappe.get_doc("Mail Account", "alice@acme.com")
        doc.disable_receiving = 1
        doc.save(ignore_permissions=True)
        self.assertEqual(
            self.fake.find("Account", name="alice")["permissions"],
            {
                "@type": "Replace",
                "enabledPermissions": {"authenticate": True, "emailReceive": True},
                "disabledPermissions": {"sysAccountGet": True, "emailReceive": True},
            },
        )

        # Receiving again, it is back to exactly what was set by hand: nothing lost, nothing regained.
        doc.disable_receiving = 0
        doc.save(ignore_permissions=True)
        self.assertEqual(
            self.fake.find("Account", name="alice")["permissions"],
            {
                "@type": "Replace",
                "enabledPermissions": {"authenticate": True, "emailReceive": True},
                "disabledPermissions": {"sysAccountGet": True},
            },
        )

    def test_a_save_that_changes_receiving_reaches_the_cluster_as_one_update(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        accounts.create_account("noreply@acme.com", "secret-pw", display_name="No Reply")

        doc = frappe.get_doc("Mail Account", "noreply@acme.com")
        doc.display_name = "Do Not Reply"
        doc.disable_receiving = 1
        calls = len(self.fake.calls)
        doc.save(ignore_permissions=True)

        # One update is taken or refused whole. Two could leave the cluster with the new name while
        # the refused second one rolls the document back to the old.
        updates = [args["update"] for name, args in self.fake.calls[calls:] if name == "x:Account/set"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0][doc.stalwart_id],
            {
                "description": "Do Not Reply",
                "permissions": {
                    "@type": "Merge",
                    "enabledPermissions": {},
                    "disabledPermissions": {"emailReceive": True},
                },
            },
        )

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
            [a["email"] for a in groups.add_alias("sales@acme.com", "team@acme.com")["aliases"]],
            ["team@acme.com"],
        )
        self.assertFalse(
            groups.set_alias_enabled("sales@acme.com", "team@acme.com", "0")["aliases"][0]["enabled"]
        )
        self.assertEqual(groups.remove_alias("sales@acme.com", "team@acme.com")["aliases"], [])
        self.assertEqual(
            [a["email"] for a in mailing_lists.add_alias("all@acme.com", "everyone@acme.com")["aliases"]],
            ["everyone@acme.com"],
        )
        self.assertFalse(
            mailing_lists.set_alias_enabled("all@acme.com", "everyone@acme.com", False)["aliases"][0][
                "enabled"
            ]
        )
        self.assertEqual(mailing_lists.remove_alias("all@acme.com", "everyone@acme.com")["aliases"], [])
        # An alias that is taken elsewhere is refused, whichever object asks for it.
        self.assertRaises(frappe.DuplicateEntryError, groups.add_alias, "sales@acme.com", "al@acme.com")

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

    def test_refusals_become_422_and_deletes_204(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        with self.assertRaises(frappe.ValidationError) as ctx:
            accounts.create_account("carol@acme.com", "short")
        self.assertEqual(ctx.exception.http_status_code, 422)
        with self.assertRaises(frappe.DoesNotExistError) as ctx:
            accounts.get_account("nobody@acme.com")
        self.assertEqual(ctx.exception.http_status_code, 404)  # not a 417, so left alone

        domains.delete_domain("acme.com")
        self.assertEqual(frappe.local.response.get("http_status_code"), 204)

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
        self.assertIn("did not accept the change: Address carol already exists", str(ctx.exception))
        self.assertFalse(frappe.db.exists("Mail Account", "carol@acme.com"))
