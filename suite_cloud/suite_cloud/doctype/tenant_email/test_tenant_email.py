import email
import smtplib
from email import policy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api import email as email_api
from suite_cloud.api.tenant import InvalidInputError
from suite_cloud.mail.tests.fixtures import make_owner
from suite_cloud.suite_cloud.doctype.tenant_email import email_types, tenant_email
from suite_cloud.suite_cloud.doctype.tenant_email.email_types import EMAIL_TYPES, InvalidEmailError
from suite_cloud.suite_cloud.doctype.tenant_email.tenant_email import EmailNotConfiguredError
from suite_cloud.utils import clear_config_cache

ACCOUNT = "Suite Cloud Test Mail"
RECIPIENT = "someone@example.test"
INVITE = {"inviter": "Ana <b>Lima</b>", "link": "https://acme.example.test/mail/signup/abc"}


class TestTenantEmail(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.db.delete("Suite Tenant", {"title": "Mail Senders Inc."})
        self.tenant = frappe.get_doc(
            {
                "doctype": "Suite Tenant",
                "title": "Mail Senders Inc.",
                "tenant_type": "Team",
                "user": make_owner(),
            }
        ).insert()
        self.account = make_email_account()
        self.set_settings(tenant_email_accounts=[{"tenant_type": "Team", "email_account": ACCOUNT}])
        self.deliver = patch.object(tenant_email, "deliver").start()
        self.addCleanup(patch.stopall)

    def set_settings(self, **values) -> None:
        settings = frappe.get_single("Suite Cloud Settings")
        settings.update({"use_tenant_title_as_sender_name": 0, **values})
        settings.save()
        frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
        clear_config_cache()

    def send(self, request_key: str = "invite-1", email_type: str = "account-invite", params=None):
        return tenant_email.send(self.tenant, email_type, RECIPIENT, params or INVITE, request_key)

    def sent_message(self) -> email.message.EmailMessage:
        _account, recipient, raw = self.deliver.call_args.args
        self.assertEqual(recipient, RECIPIENT)
        return email.message_from_bytes(raw, policy=policy.SMTP)

    def test_suite_cloud_renders_the_email_and_its_account_sends_it(self) -> None:
        log = self.send()

        message = self.sent_message()
        self.assertEqual(message["From"], f"{ACCOUNT} <noreply@cloud.example.test>")
        self.assertEqual(message["Reply-To"], "noreply@cloud.example.test")  # the account's, never the site's
        self.assertEqual(message["Subject"], "Ana <b>Lima</b> invited you to Frappe Mail")
        html = message.get_body(("html",)).get_content()
        self.assertIn("Ana &lt;b&gt;Lima&lt;/b&gt; invited you to join Mail Senders Inc.", html)
        self.assertIn('href="https://acme.example.test/mail/signup/abc"', html)
        self.assertEqual(
            (log.status, log.email_type, log.email_account, log.subject),
            ("Sent", "account-invite", ACCOUNT, "Ana <b>Lima</b> invited you to Frappe Mail"),
        )
        self.assertEqual(message["Message-Id"], f"<{log.message_id}>")
        self.assertEqual(frappe.get_doc("Suite Tenant", self.tenant.name).sent_email_count(), 1)

    def test_every_email_type_renders(self) -> None:
        for name, email_type in EMAIL_TYPES.items():
            params = {
                p: "https://acme.example.test/x" if p in email_type.links else "1" for p in email_type.params
            }
            with self.subTest(name):
                subject, body = email_types.render(name, email_type, params, "Acme")
                self.assertTrue(subject and "\n" not in subject)
                self.assertIn(f"Frappe {email_type.product}", body)

    def test_the_tenant_title_and_the_accounts_reply_to_when_set(self) -> None:
        self.set_settings(use_tenant_title_as_sender_name=1)
        self.account.add_reply_to_header = 1
        self.account.append("reply_to_addresses", {"_name": "Support", "email": "support@cloud.example.test"})
        self.account.save()

        self.send()

        message = self.sent_message()
        self.assertEqual(message["From"], '"Mail Senders Inc." <noreply@cloud.example.test>')
        self.assertEqual(message["Reply-To"], "Support <support@cloud.example.test>")

    def test_only_known_emails_with_their_parameters_are_sent(self) -> None:
        for email_type, params, error in (
            ("newsletter", INVITE, "does not send newsletter"),
            ("account-invite", {"link": INVITE["link"]}, "Missing parameters: inviter"),
            ("account-invite", {**INVITE, "body": "<p>Win!</p>"}, "Unknown parameters: body"),
            ("account-invite", {**INVITE, "inviter": ["Ana"]}, "inviter must be text"),
            ("account-invite", {**INVITE, "link": "javascript:alert(1)"}, "link must be a web address"),
        ):
            with self.subTest(error):
                self.assertRaisesRegex(
                    InvalidEmailError, error, self.send, params=params, email_type=email_type
                )
        self.deliver.assert_not_called()

    def test_links_lead_only_to_the_tenants_own_site_when_it_is_hosted(self) -> None:
        email_type = EMAIL_TYPES["password-reset"]
        hosts = {"acme.example.test"}

        email_types.validate_params(email_type, {"link": "https://acme.example.test/reset/1"}, hosts)
        self.assertRaisesRegex(
            InvalidEmailError,
            "the tenant's own site",
            email_types.validate_params,
            email_type,
            {"link": "https://phish.example.net/reset/1"},
            hosts,
        )
        email_types.validate_params(email_type, {"link": "https://anywhere.example.net/1"}, set())

    def test_a_key_that_was_sent_is_not_sent_again(self) -> None:
        first, second = self.send(), self.send()

        self.assertEqual(first.name, second.name)
        self.deliver.assert_called_once()

    def test_a_concurrent_request_with_the_key_answers_the_first_result(self) -> None:
        first = self.send()

        # The first request's row is not committed yet: the second inserts the same name and waits.
        with patch.object(frappe.db, "exists", return_value=False):
            second = self.send()

        self.assertEqual((second.name, second.status), (first.name, "Sent"))
        self.deliver.assert_called_once()

    def test_an_email_counts_in_the_month_it_was_sent(self) -> None:
        self.deliver.side_effect = smtplib.SMTPRecipientsRefused({RECIPIENT: (450, b"Try later")})
        failed = self.send()
        failed.db_set("creation", frappe.utils.add_months(frappe.utils.now_datetime(), -1))

        self.deliver.side_effect = None
        self.send()

        self.assertEqual(frappe.get_doc("Suite Tenant", self.tenant.name).sent_email_count(), 1)

    def test_a_refused_email_fails_and_its_key_sends_it_again(self) -> None:
        self.deliver.side_effect = smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"No such user")})
        failed = self.send()
        self.assertEqual((failed.status, failed.error), ("Failed", "550 No such user"))
        self.assertEqual(failed.to_api()["status"], "Failed")

        self.deliver.side_effect = None
        sent = self.send()

        self.assertEqual((sent.name, sent.status, sent.error), (failed.name, "Sent", None))

    def test_a_tenant_type_without_an_account_cannot_send(self) -> None:
        self.set_settings(tenant_email_accounts=[])

        self.assertRaisesRegex(EmailNotConfiguredError, "Team tenants", self.send)
        self.deliver.assert_not_called()

    def test_settings_take_one_sending_account_per_tenant_type(self) -> None:
        rows = [{"tenant_type": "Team", "email_account": ACCOUNT}] * 2
        self.assertRaisesRegex(
            frappe.ValidationError, "already have", self.set_settings, tenant_email_accounts=rows
        )

    def test_the_api_takes_one_recipient(self) -> None:
        frappe.local.suite_tenant = self.tenant
        self.addCleanup(setattr, frappe.local, "suite_tenant", None)

        for recipient in ("a@example.test, b@example.test", "not-an-address"):
            with self.subTest(recipient):
                self.assertRaises(
                    InvalidInputError, email_api.send_email, "account-invite", recipient, INVITE, "key"
                )
        result = email_api.send_email("account-invite", RECIPIENT, INVITE, "invite-2")
        self.assertEqual(result["status"], "Sent")


def make_email_account():
    if frappe.db.exists("Email Account", ACCOUNT):
        frappe.delete_doc("Email Account", ACCOUNT, force=True)
    return frappe.get_doc(
        {
            "doctype": "Email Account",
            "email_account_name": ACCOUNT,
            "email_id": "noreply@cloud.example.test",
            "smtp_server": "smtp.cloud.example.test",
            "no_smtp_authentication": 1,
            "enable_outgoing": 1,
        }
    ).insert()
