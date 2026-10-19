from unittest.mock import patch

import frappe

from suite_cloud.api.mail import domains as domains_api
from suite_cloud.hosting import capacity, labels, progress
from suite_cloud.hosting.labels import LabelNotAvailableError
from suite_cloud.hosting.tests.fakes import REGION
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, HostingFixtures
from suite_cloud.mail.tenancy import hosted_domain
from suite_cloud.mail.tenancy.hosted_domain import HostedObjectError
from suite_cloud.mail.tests.test_tenancy import TenancyTestCase

DOMAIN = f"acme.{MAIL_ZONE}"


class TestHostedMail(HostingFixtures, TenancyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.setup_hosting(real_mail=True, site_dns_zone=None)  # the site domain has its own tests
        self.vm = self.make_vm()

    def claim(self):
        return self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

    def test_the_tenant_gets_a_live_mail_domain_and_no_account_of_the_sites_own(self) -> None:
        site = self.claim()

        self.assertEqual((site.mail_domain, site.mail_domain_verified), (DOMAIN, 1))
        domain = frappe.get_doc("Mail Domain", DOMAIN)
        self.assertTrue(domain.hosted and domain.is_live())
        # The site sends through Suite Cloud: it has no mailbox or Email Account of its own.
        self.assertFalse(frappe.db.exists("Mail Account", {"domain": DOMAIN}))
        self.assertFalse([key for key in self.site_api.docs if key[0] == "Email Account"])

    def test_a_request_shows_every_step_to_the_owner_invite(self) -> None:
        values = {
            "email": "progress@frappe.test",
            "full_name": "John Doe",
            "organization": "Acme",
            "label": "acme",
        }
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
        self.assertEqual(details["mail_domain"], DOMAIN)
        self.assertEqual(details["invite"], site.login_email)
        self.assertGreaterEqual(request.processing_time, 0)  # set once the last step completed
        reports = [call.args[1] for call in publish.call_args_list if call.args[0] == progress.EVENT]
        self.assertEqual((reports[-1]["progress"], reports[-1]["total"]), (13, 13))

    def test_the_owner_is_invited_once_the_domain_is_live(self) -> None:
        self.tenant.db_set("max_accounts", 1)
        self.make_domain()
        self.make_account("someone@acme.com")  # the tenant's one account: the site takes none
        site = self.claim()

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
        self.assertFalse(site.mail_domain_verified)
        self.assertFalse(self.site_api.invites())

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

    def test_drop_site_keeps_the_tenants_mail_domain_and_mailboxes(self) -> None:
        site = self.claim()
        self.make_account(f"owner@{DOMAIN}")  # the owner accepted the invite

        self.site(site.name).drop_site()

        site = self.site(site.name)
        self.assertEqual((site.status, site.error), ("Dropped", None))
        self.assertFalse(site.mail_domain or site.mail_domain_verified)
        self.assertTrue(frappe.db.exists("Mail Account", f"owner@{DOMAIN}"))
        self.assertTrue(frappe.db.exists("Mail Domain", DOMAIN))
        self.assertTrue(frappe.db.exists("DNS Record", {"managed_by": DOMAIN}))

    def test_the_tenants_next_site_takes_its_kept_domain_back(self) -> None:
        self.site(self.claim().name).drop_site()
        self.assertRaises(LabelNotAvailableError, labels.assert_label_available, "acme")  # anyone else

        name = frappe.get_doc("Suite Tenant", self.tenant.name).create_hosted_site("acme")

        site = self.site(name)
        self.assertEqual((site.mail_domain, site.mail_domain_verified), (DOMAIN, 1))

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
