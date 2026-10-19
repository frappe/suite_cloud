import email
import smtplib
from email import policy
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api import email as email_api
from suite_cloud.api.tenant import InvalidInputError
from suite_cloud.mail.tests.fixtures import make_owner
from suite_cloud.suite_cloud.doctype.tenant_email import tenant_email
from suite_cloud.suite_cloud.doctype.tenant_email.tenant_email import EmailNotConfiguredError
from suite_cloud.utils import clear_config_cache

ACCOUNT = "Suite Cloud Test Mail"
RECIPIENT = "someone@example.test"
MESSAGE = (
    "From: Site Admin <admin@acme.example.test>\r\n"
    "Reply-To: admin@acme.example.test\r\n"
    "To: someone@example.test\r\n"
    "Subject: You have been invited\r\n"
    "Message-Id: <invite-1@acme.example.test>\r\n"
    "X-Suite-Email-Type: Account-Invite\r\n"
    "Content-Type: text/plain\r\n"
    "\r\n"
    "Join us.\r\n"
)


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

    def send(self, request_key: str = "queue-1:someone@example.test"):
        return tenant_email.send(self.tenant, RECIPIENT, MESSAGE, request_key)

    def sent_message(self) -> email.message.EmailMessage:
        _account, recipient, raw = self.deliver.call_args.args
        self.assertEqual(recipient, RECIPIENT)
        return email.message_from_bytes(raw, policy=policy.SMTP)

    def test_the_account_of_the_tenants_type_sends_under_its_own_name(self) -> None:
        log = self.send()

        message = self.sent_message()
        self.assertEqual(message["From"], f"{ACCOUNT} <noreply@cloud.example.test>")
        self.assertIsNone(message["Reply-To"])  # the site's is dropped; the account sets none
        self.assertIsNone(message["X-Suite-Email-Type"])
        self.assertEqual(message["Subject"], "You have been invited")
        self.assertEqual(
            (log.status, log.email_type, log.email_account, log.message_id),
            ("Sent", "account-invite", ACCOUNT, "invite-1@acme.example.test"),
        )
        self.assertEqual(frappe.get_doc("Suite Tenant", self.tenant.name).sent_email_count(), 1)

    def test_the_tenant_title_and_the_accounts_reply_to_when_set(self) -> None:
        self.set_settings(use_tenant_title_as_sender_name=1)
        self.account.add_reply_to_header = 1
        self.account.append("reply_to_addresses", {"_name": "Support", "email": "support@cloud.example.test"})
        self.account.save()

        self.send()

        message = self.sent_message()
        self.assertEqual(message["From"], '"Mail Senders Inc." <noreply@cloud.example.test>')
        self.assertEqual(message["Reply-To"], "Support <support@cloud.example.test>")

    def test_a_key_that_was_sent_is_not_sent_again(self) -> None:
        first, second = self.send(), self.send()

        self.assertEqual(first.name, second.name)
        self.deliver.assert_called_once()

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
                self.assertRaises(InvalidInputError, email_api.send_email, recipient, MESSAGE, "key")
        result = email_api.send_email(RECIPIENT, MESSAGE, "queue-2:someone@example.test")
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
