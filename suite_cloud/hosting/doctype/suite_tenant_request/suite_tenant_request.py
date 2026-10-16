# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import hashlib
import secrets
from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now, now_datetime

from suite_cloud.hosting import begin_step, commit, labels, run_in_background, undo_step
from suite_cloud.mail.tenancy.addresses import validate_email_address
from suite_cloud.utils import get_config, log_exception

OTP_TTL = 600
OTP_ATTEMPTS = 5
REQUEST_TTL_HOURS = 24


class SuiteTenantRequest(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        email: DF.Data
        error: DF.SmallText | None
        expires_at: DF.Datetime | None
        full_name: DF.Data
        ip_address: DF.Data | None
        label: DF.Data
        request_key: DF.Data | None
        status: DF.Literal["Pending", "Verified", "Processing", "Completed", "Failed", "Expired"]
        suite_site: DF.Link | None
        tenant: DF.Link | None
        timezone: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    def before_insert(self) -> None:
        self.status = "Pending"
        self.request_key = secrets.token_urlsafe(32)
        self.expires_at = add_to_date(now_datetime(), hours=REQUEST_TTL_HOURS)

    def validate(self) -> None:
        if self.is_new():
            self.validate_request()

    def validate_request(self) -> None:
        """The same checks for a signup and for an operator creating the request in the desk."""

        self.email = validate_email_address(self.email)
        self.full_name = (self.full_name or "").strip()
        if not self.full_name:
            frappe.throw(_("Enter the name of the person signing up."))
        self.label = labels.validate_label(self.label)
        labels.assert_label_available(self.label)
        self.timezone = (self.timezone or "").strip() or None
        if self.email.split("@", 1)[0] == get_config("notification_mailbox_local_part"):
            # The owner's account would be the site's own notification account.
            frappe.throw(
                _("{0} cannot sign up: its name is kept for the site's notifications.").format(self.email)
            )

    def after_insert(self) -> None:
        """A signup proves its email with a code; an operator's request is taken on their word."""

        if not self.flags.verify_by_otp:
            self.db_set("status", "Verified")
            enqueue_processing(self.name)

    # --- one-time code --------------------------------------------------------

    def send_otp(self) -> None:
        otp = f"{secrets.randbelow(10**6):06d}"
        frappe.cache.set_value(
            otp_key(self.name), {"hash": otp_hash(otp), "attempts": 0}, expires_in_sec=OTP_TTL
        )
        frappe.sendmail(
            recipients=[self.email],
            subject=_("Your Frappe Suite verification code"),
            message=_("Your code is {0}. It expires in 10 minutes.").format(otp),
            delayed=False,
        )

    def verify_otp(self, otp: str) -> None:
        if self.status != "Pending" or now_datetime() > self.expires_at:
            frappe.throw(_("This signup is no longer waiting for a code."))
        stored = frappe.cache.get_value(otp_key(self.name), expires=True)
        if not stored or stored["attempts"] >= OTP_ATTEMPTS:
            frappe.throw(_("The code expired; ask for a new one."))
        if not secrets.compare_digest(stored["hash"], otp_hash((otp or "").strip())):
            stored["attempts"] += 1
            frappe.cache.set_value(otp_key(self.name), stored, expires_in_sec=OTP_TTL)
            frappe.throw(_("The code is not correct."))
        frappe.cache.delete_value(otp_key(self.name))
        self.db_set("status", "Verified")
        enqueue_processing(self.name)

    # --- completion -------------------------------------------------------------

    def complete(self) -> None:
        """The site is ready and its owner invited from it: they choose their password there."""

        self.db_set({"status": "Completed", "error": None})

    # --- actions ----------------------------------------------------------------

    @frappe.whitelist()
    def retry(self) -> None:
        frappe.only_for("System Manager")
        if self.status != "Failed":
            frappe.throw(_("Only a failed request can be retried."))
        self.db_set({"status": "Verified", "error": None})
        enqueue_processing(self.name)

    @frappe.whitelist()
    def resend_invite(self) -> None:
        """The owner's invite again, or a fresh one once it expired."""

        frappe.only_for("System Manager")
        if self.status != "Completed" or not self.suite_site:
            frappe.throw(_("The site of this request has not invited its owner yet."))
        frappe.get_doc("Suite Site", self.suite_site).resend_owner_invite()


def enqueue_processing(request: str) -> None:
    run_in_background(
        "suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request.process_request",
        f"signup:{request}",
        request=request,
    )


def process_request(request: str) -> None:
    """User, then tenant, then a claimed site; the site's own job completes the request."""

    doc = frappe.get_doc("Suite Tenant Request", request, for_update=True)
    if doc.status != "Verified":
        return
    begin_step()
    try:
        from suite_cloud.hosting import capacity

        region = capacity.assert_region_enabled(get_config("default_region"))
        tenant = doc.tenant or ensure_tenant(doc, ensure_user(doc), region)
        # Linked before the site is claimed: the site's job reads the password through the tenant.
        doc.db_set({"tenant": tenant, "status": "Processing"})
        site = capacity.claim_site(tenant, doc.label, region)
        frappe.db.set_value("Suite Tenant Request", doc.name, "suite_site", site)
    except Exception as e:
        undo_step()
        log_exception(f"Signup {request} failed", doc)
        frappe.get_doc("Suite Tenant Request", request).db_set({"status": "Failed", "error": str(e)})
    commit()


def ensure_user(request) -> str:
    """The signing-up user, as a Website User holding the Suite Tenant role."""

    from suite_cloud.mail.tenancy.permissions import TENANT_ROLE

    if frappe.db.exists("User", request.email):
        user = frappe.get_doc("User", request.email)
        if TENANT_ROLE not in {row.role for row in user.roles}:
            user.append_roles(TENANT_ROLE)
            user.save(ignore_permissions=True)
        return user.name
    first_name, _space, last_name = request.full_name.strip().partition(" ")
    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": request.email,
            "first_name": first_name,
            "last_name": last_name,
            "user_type": "Website User",
            "send_welcome_email": 0,
            "roles": [{"role": TENANT_ROLE}],
        }
    )
    user.insert(ignore_permissions=True)
    return user.name


def ensure_tenant(request, user: str, region: str) -> str:
    tenant = frappe.get_doc(
        {
            "doctype": "Suite Tenant",
            "title": _("{0}'s workspace").format(request.full_name.strip()),
            "tenant_type": "Suite Site",
            "user": user,
        }
    )
    tenant.flags.region = region
    tenant.insert(ignore_permissions=True)
    return tenant.name


def expire_requests() -> None:
    """Daily: signups that never confirmed their email expire."""

    stale = frappe.get_all(
        "Suite Tenant Request", {"status": "Pending", "expires_at": ["<", now()]}, pluck="name"
    )
    for name in stale:
        frappe.db.set_value("Suite Tenant Request", name, "status", "Expired")
    commit()


def otp_key(request: str) -> str:
    return f"suite_cloud:signup-otp:{request}"


def otp_hash(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()
