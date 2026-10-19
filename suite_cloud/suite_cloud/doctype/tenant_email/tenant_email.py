# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import email
import smtplib
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formataddr
from uuid import NAMESPACE_URL, uuid5

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now
from frappe.utils import now_datetime

EMAIL_TYPE_HEADER = "X-Suite-Email-Type"
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
# The site's say on who sends: the Email Account sends, so these are set again or left out.
SITE_HEADERS = ("From", "Sender", "Reply-To", "Return-Path", "X-Original-From", EMAIL_TYPE_HEADER)


class EmailNotConfiguredError(frappe.ValidationError):
    """No outgoing Email Account is set for the tenant's type: an operator problem."""


class TenantEmail(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        email_account: DF.Link | None
        email_type: DF.Data | None
        error: DF.SmallText | None
        message_id: DF.Data | None
        recipient: DF.Data | None
        request_key: DF.Data | None
        sender: DF.Data | None
        sent_at: DF.Datetime | None
        status: DF.Literal["Sending", "Sent", "Failed"]
        subject: DF.SmallText | None
        tenant: DF.Link
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = get_name(self.tenant, self.request_key)

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message_id": self.message_id,
            "sent_at": self.sent_at,
            "error": self.error,
        }

    @staticmethod
    def clear_old_logs(days: int = 90) -> None:
        table = frappe.qb.DocType("Tenant Email")
        frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))


def send(tenant, recipient: str, message: str, request_key: str) -> TenantEmail:
    """Sends one email of the tenant's site now, through the Email Account of the tenant's type.

    A key that was sent answers that result again; a failed one is sent again.
    """

    log = claim(tenant.name, request_key)
    if log.status == "Sent":
        return log
    account = get_email_account(tenant.tenant_type)
    parsed = parse(message)
    email_type = str(parsed[EMAIL_TYPE_HEADER] or "other").strip().lower()[:140]
    parsed = as_sent_by(parsed, account, get_sender_name(tenant, account))
    log.update(
        {
            "email_type": email_type,
            "recipient": recipient,
            "email_account": account.name,
            "sender": str(parsed["From"]),
            "subject": str(parsed["Subject"] or ""),
            "message_id": str(parsed["Message-Id"] or "").strip("<>") or None,  # no tag to sanitize
        }
    )
    try:
        deliver(account, recipient, parsed.as_bytes())
        log.update({"status": "Sent", "sent_at": now_datetime(), "error": None})
    except Exception as e:
        log.update({"status": "Failed", "error": describe(e)})
    log.save(ignore_permissions=True)
    return log


def get_name(tenant: str, request_key: str) -> str:
    """One name per tenant and key, so a second row for the key cannot be inserted."""

    return str(uuid5(NAMESPACE_URL, f"suite-cloud:tenant-email:{tenant}:{request_key}"))


def claim(tenant: str, request_key: str) -> TenantEmail:
    """The key's row, locked until the request ends, before anything is sent: a concurrent request
    with the same key waits on the lock, then answers this request's result."""

    name = get_name(tenant, request_key)
    if not frappe.db.exists("Tenant Email", name):
        try:
            frappe.get_doc({"doctype": "Tenant Email", "tenant": tenant, "request_key": request_key}).insert(
                ignore_permissions=True
            )
        except frappe.DuplicateEntryError:
            pass  # inserted by a concurrent request, which held it until it was done
    return frappe.get_doc("Tenant Email", name, for_update=True)


def get_email_account(tenant_type: str) -> Document:
    name = frappe.get_cached_doc("Suite Cloud Settings").get_tenant_email_account(tenant_type)
    if not name or not frappe.db.get_value("Email Account", name, "enable_outgoing"):
        frappe.throw(
            _(
                "Suite Cloud has no outgoing Email Account for {0} tenants. Ask its operators to set one."
            ).format(tenant_type),
            EmailNotConfiguredError,
        )
    return frappe.get_doc("Email Account", name)


def get_sender_name(tenant, account) -> str:
    if frappe.get_cached_doc("Suite Cloud Settings").use_tenant_title_as_sender_name and tenant.title:
        return tenant.title
    return account.name


def parse(message: str) -> EmailMessage:
    if len(message.encode()) > MAX_MESSAGE_BYTES:
        frappe.throw(_("The email is larger than {0} MB.").format(MAX_MESSAGE_BYTES // (1024 * 1024)))
    return email.message_from_string(message, policy=policy.SMTP)


def as_sent_by(message: EmailMessage, account, sender_name: str) -> EmailMessage:
    """From the account under ``sender_name``; Reply-To only as the account sets it."""

    for header in SITE_HEADERS:
        del message[header]
    message["From"] = Address(display_name=sender_name, addr_spec=account.email_id)
    if reply_to := get_reply_to(account):
        message["Reply-To"] = reply_to
    return message


def get_reply_to(account) -> str | None:
    if not account.add_reply_to_header:
        return None
    addresses = [formataddr((row._name, row.email)) for row in account.reply_to_addresses if row.email]
    return ", ".join(addresses) or None


def deliver(account, recipient: str, message: bytes) -> None:
    if account.service == "Frappe Mail":
        account.get_frappe_mail_client().send_raw(
            sender=account.email_id, recipients=recipient, message=message
        )
        return
    server = account.get_smtp_server()
    try:
        server.session.sendmail(from_addr=account.email_id, to_addrs=recipient, msg=message)
    finally:
        server.quit()


def describe(error: Exception) -> str:
    """The mail server's own answer when it gave one; anything else is logged for the operators."""

    if isinstance(error, smtplib.SMTPRecipientsRefused):
        code, reply = next(iter(error.recipients.values()))
        return f"{code} {frappe.safe_decode(reply)}"
    if isinstance(error, smtplib.SMTPResponseException):
        return f"{error.smtp_code} {frappe.safe_decode(error.smtp_error)}"
    frappe.log_error(title="[Suite Cloud] Tenant email not sent", defer_insert=True)
    return str(error) or type(error).__name__
