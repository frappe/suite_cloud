from unittest.mock import patch

import frappe

from suite_cloud.api.mail import accounts as accounts_api
from suite_cloud.api.mail import domains as domains_api
from suite_cloud.api.mail import groups as groups_api
from suite_cloud.hosting import capacity, progress, sites
from suite_cloud.hosting.tests.fakes import REGION
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, HostingFixtures
from suite_cloud.mail.tenancy import hosted_domain
from suite_cloud.mail.tenancy.hosted_domain import HostedObjectError
from suite_cloud.mail.tests.test_tenancy import TenancyTestCase

DOMAIN = f"acme.{MAIL_ZONE}"
NOTIFICATION = f"notification@{DOMAIN}"


class TestHostedMail(HostingFixtures, TenancyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.setup_hosting(real_mail=True, site_dns_zone=None)  # the site domain has its own tests
        self.vm = self.make_vm()

    def claim(self):
        return self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

    def test_the_tenant_gets_a_live_mail_domain_and_a_notification_account(self) -> None:
        site = self.claim()

        self.assertEqual((site.mail_domain, site.notification_account), (DOMAIN, NOTIFICATION))
        self.assertTrue(site.email_account_created)
        domain = frappe.get_doc("Mail Domain", DOMAIN)
        self.assertTrue(domain.hosted and domain.is_live())
        account = self.site_api.docs[("Email Account", "Notifications")]
        self.assertEqual(account["email_id"], NOTIFICATION)
        # From the domain's _submissions._tcp record: implicit TLS on 465, as the cluster serves it.
        self.assertEqual((account["smtp_server"], account["smtp_port"]), (self.cluster.hostname, 465))
        self.assertEqual((account["use_ssl_for_outgoing"], account["use_tls"]), (1, 0))
        self.assertEqual((account["default_outgoing"], account["enable_outgoing"]), (1, 1))
        self.assertTrue(account["password"])

    def test_a_request_shows_every_step_to_the_owner_invite(self) -> None:
        values = {"email": "progress@frappe.test", "full_name": "John Doe", "label": "acme"}
        request = frappe.get_doc({"doctype": "Suite Tenant Request", **values})
        request.flags.verify_by_otp = True  # not processed: the test claims the site for the tenant
        request.insert()
        request.db_set({"status": "Processing", "tenant": self.tenant.name})
        progress.sync(request)

        with patch("frappe.publish_realtime") as publish:
            site = self.claim()

        request.reload()
        self.assertEqual(request.status, "Completed")
        statuses = {row.step: row.status for row in request.steps}
        self.assertEqual(statuses.pop("site_domain"), "Skipped")  # no site DNS zone here
        self.assertEqual(set(statuses.values()), {"Completed"})
        details = {row.step: row.detail for row in request.steps}
        self.assertEqual((details["mail_domain"], details["notification_account"]), (DOMAIN, NOTIFICATION))
        self.assertEqual(details["invite"], site.login_email)
        reports = [call.args[1] for call in publish.call_args_list if call.args[0] == progress.EVENT]
        self.assertEqual((reports[-1]["progress"], reports[-1]["total"]), (14, 14))

    def test_the_notification_account_is_a_send_only_system_account(self) -> None:
        self.tenant.db_set("max_accounts", 1)
        self.make_domain()
        self.make_account("someone@acme.com")  # the tenant's one account
        site = self.claim()

        account = frappe.get_doc("Mail Account", NOTIFICATION)  # created despite the full limit
        self.assertEqual((account.is_system_account, account.disable_receiving), (1, 1))
        tenant = frappe.get_doc("Suite Tenant", self.tenant.name)
        self.assertEqual(tenant.account_count(), 1)
        self.fake.objects["Account"][account.stalwart_id]["usedDiskQuota"] = 10**9
        self.assertEqual(tenant.fetch_mail_used_disk_bytes(), 0)  # Suite Cloud's own disk
        # Once it sends, the site invites the owner from it.
        (invite,) = self.site_api.invites()
        self.assertEqual(invite["account"], f"owner@{DOMAIN}")
        self.assertEqual(site.owner_invite, invite["name"])

    def test_the_domain_is_checked_only_once_the_provider_has_its_records(self) -> None:
        with (
            patch(
                "suite_cloud.suite_cloud.doctype.dns_record.dns_record.get_dns_provider", return_value=None
            ),
            patch("suite_cloud.mail.doctype.mail_domain.mail_domain.MailDomain.verify_dns_records") as verify,
        ):
            site = self.claim()

        self.assertEqual(site.mail_domain, DOMAIN)  # created and its records queued
        verify.assert_not_called()  # resolvers asked now would cache the absence
        self.assertFalse(site.notification_account)

    def test_the_domain_records_are_published_in_the_mail_zone(self) -> None:
        site = self.claim()

        rows = frappe.get_all(
            "DNS Record",
            {"managed_by_doctype": "Mail Domain", "managed_by": site.mail_domain},
            ["host", "type", "category"],
        )
        self.assertIn(("acme", "MX", "Other"), {(r.host, r.type, r.category) for r in rows})
        self.assertIn(("acme", "TXT", "SPF"), {(r.host, r.type, r.category) for r in rows})
        self.assertIn("_dmarc.acme", {r.host for r in rows})
        self.assertTrue(any(r.host.endswith("._domainkey.acme") for r in rows))
        self.assertFalse(any(r.host.startswith("_imaps") for r in rows))  # SRV has no DNS Record type

    def test_changed_records_are_published_again(self) -> None:
        self.claim()
        domain = frappe.get_doc("Mail Domain", DOMAIN)
        row = next(row for row in domain.dns_rows() if "_domainkey" in row.host)
        row.value = "v=DKIM1; k=ed25519; p=replaced"

        domain.save_records()  # as replace_dkim_keys and refresh_dns_records do

        self.assertTrue(
            frappe.db.exists("DNS Record", {"managed_by": DOMAIN, "value": ["like", "%p=replaced"]})
        )

    def test_a_domain_keeps_its_zone_after_the_mail_zone_moves(self) -> None:
        from suite_cloud.tests.fixtures import make_zone

        self.claim()
        make_zone("moved.test", is_default=0, dns_provider="Cloudflare", dns_provider_token="token")
        self.addCleanup(self.set_setting, "tenant_mail_zone", MAIL_ZONE)
        self.set_setting("tenant_mail_zone", "moved.test")

        frappe.get_doc("Mail Domain", DOMAIN).save_records()

        zones = frappe.get_all("DNS Record", {"managed_by": DOMAIN}, pluck="dns_zone", distinct=True)
        self.assertEqual(zones, [MAIL_ZONE])

    def test_a_failed_smtp_check_is_retried_with_a_new_password(self) -> None:
        self.site_api.smtp_failures = 1
        site = self.claim()
        self.assertFalse(site.email_account_created)
        self.assertIn("Mail", site.error)
        self.assertEqual(site.status, "Active")

        sites.provision(site.name)

        self.assertTrue(self.site(site.name).email_account_created)
        self.assertFalse(self.site(site.name).error)  # the step's earlier failure is cleared
        self.assertEqual(frappe.db.count("Mail Account", {"name": NOTIFICATION}), 1)

    def test_drop_site_removes_the_mail_domain_and_its_records(self) -> None:
        site = self.claim()

        self.site(site.name).drop_site()

        self.assertFalse(frappe.db.exists("Mail Domain", DOMAIN))
        self.assertFalse(frappe.db.exists("Mail Account", NOTIFICATION))
        self.assertFalse(frappe.db.exists("DNS Record", {"managed_by": DOMAIN}))
        self.assertEqual(self.site(site.name).status, "Dropped")

    def test_a_hosted_domain_must_be_one_label_of_the_tenants_site(self) -> None:
        self.claim()

        for name in (f"a.b.{MAIL_ZONE}", f"other.{MAIL_ZONE}", "acme.example.com"):
            with self.subTest(name):
                doc = frappe.get_doc(
                    {"doctype": "Mail Domain", "domain_name": name, "tenant": self.tenant.name, "hosted": 1}
                )
                self.assertRaises(frappe.ValidationError, doc.insert)

    def test_a_plain_domain_under_the_mail_zone_stays_reserved(self) -> None:
        doc = frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": f"other.{MAIL_ZONE}", "tenant": self.tenant.name}
        )
        self.assertRaisesRegex(frappe.ValidationError, "belongs to the mail service", doc.insert)

    def test_the_tenant_api_cannot_change_the_hosted_domain(self) -> None:
        self.claim()
        self.act_as_tenant()

        self.assertRaises(HostedObjectError, domains_api.update_domain, DOMAIN, description="x")
        self.assertRaises(HostedObjectError, domains_api.delete_domain, DOMAIN)

    def test_the_tenant_api_does_not_see_the_notification_account(self) -> None:
        self.claim()
        self.act_as_tenant()

        self.assertEqual(accounts_api.list_accounts()["items"], [])
        self.assertEqual(accounts_api.list_accounts(domain=DOMAIN)["total"], 0)
        self.assertEqual(accounts_api.get_quotas([NOTIFICATION]), {})
        for call in (
            lambda: accounts_api.get_account(NOTIFICATION),
            lambda: accounts_api.set_password(NOTIFICATION, "another-secret-pw"),
            lambda: accounts_api.delete_account(NOTIFICATION),
            lambda: groups_api.create_group(email=f"team@{DOMAIN}", members=[NOTIFICATION]),
        ):
            with self.subTest(call):
                self.assertRaises(frappe.DoesNotExistError, call)

    def test_record_rows_map_hosts_to_the_zone(self) -> None:
        site = self.claim()
        rows = hosted_domain.record_rows(frappe.get_doc("Mail Domain", site.mail_domain), MAIL_ZONE)
        self.assertTrue(all(row["dns_zone"] == MAIL_ZONE for row in rows))
        self.assertNotIn("SRV", {row["type"] for row in rows})

    def act_as_tenant(self) -> None:
        frappe.local.suite_tenant = None
        frappe.set_user(self.tenant.user)
        frappe.utils.set_request(headers={"Authorization": f"Tenant {self.tenant.name}:any"})
        frappe.local.form_dict = frappe._dict()
        self.addCleanup(self.reset_session)

    def reset_session(self) -> None:
        frappe.local.suite_tenant = None
        frappe.local.request = None
        frappe.set_user("Administrator")
