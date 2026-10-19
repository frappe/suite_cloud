# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import smtplib
from email.utils import formataddr, make_msgid
from uuid import NAMESPACE_URL, uuid5

import frappe
from frappe import _
from frappe.email.email_body import get_email
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now
from frappe.utils import now_datetime

from suite_cloud.suite_cloud.doctype.tenant_email import email_types


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


def send(tenant, name: str, recipient: str, params: dict, request_key: str) -> TenantEmail:
    """Sends the tenant's site one of Suite Cloud's emails now, from its template and through the
    Email Account of the tenant's type.

    A key that was sent answers that result again; a failed one is sent again.
    """

    email_type = email_types.get_email_type(name)
    email_types.validate_params(email_type, params, tenant.get_site_hosts())
    log = claim(tenant.name, request_key)
    if log.status == "Sent":
        return log
    account = get_email_account(tenant.tenant_type)
    subject, body = email_types.render(name, email_type, params, tenant.title)
    sender = formataddr((get_sender_name(tenant, account), account.email_id))
    mail = get_email(
        [recipient],
        sender=sender,
        msg=body,
        subject=subject,
        email_account=account,
        inline_images=email_types.get_inline_images(body),
    )
    message_id = make_msgid(domain=account.email_id.rpartition("@")[2]).strip("<>")
    mail.set_message_id(message_id)
    message = mail.as_string()  # also sets the sender and Reply-To the account asks for
    log.update(
        {
            "email_type": name,
            "recipient": recipient,
            "email_account": account.name,
            "sender": mail.sender,
            "subject": subject,
            "message_id": message_id,  # without <>, which would be sanitized away as a tag
        }
    )
    try:
        deliver(account, recipient, frappe.safe_encode(message))
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
