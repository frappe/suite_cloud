"""Public signup: a visitor asks for a Suite workspace, proves their email and gets a site.

Every method is a guest POST: the password, the code and the request key travel in the body,
never in a URL.
"""

import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint
from frappe.utils.password_strength import test_password_strength

from suite_cloud.hosting import labels
from suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request import OPEN_STATUSES
from suite_cloud.mail.tenancy.addresses import validate_email_address
from suite_cloud.utils import get_config


class SignupClosedError(frappe.PermissionError):
    pass


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=60 * 60)
def create_request(
    email: str, full_name: str, label: str, password: str, timezone: str | None = None
) -> dict:
    """Stores the request and mails a one-time code to ``email``."""

    assert_signup_open()
    email = validate_email_address(email)
    full_name = (full_name or "").strip()
    if not full_name:
        frappe.throw(_("Enter your name."))
    label = labels.validate_label(label)
    labels.assert_label_available(label)
    assert_password_strength(password, [email, full_name, label])
    request = frappe.get_doc(
        {
            "doctype": "Suite Tenant Request",
            "email": email,
            "full_name": full_name,
            "label": label,
            "password": password,
            "timezone": timezone,
            "ip_address": frappe.local.request_ip,
        }
    )
    request.insert(ignore_permissions=True)
    request.send_otp()
    return {"request": request.name}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=10 * 60)
def verify_otp(request: str, otp: str) -> dict:
    """Starts creating the workspace; the returned key reads its status."""

    assert_signup_open()
    doc = get_request(request)
    doc.verify_otp(otp)
    return {"request_key": doc.request_key}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60)
def get_status(request: str, request_key: str) -> dict:
    """Where the workspace stands. Once Completed the user logs in on ``site_url`` with their password."""

    doc = get_request(request)
    if not secrets.compare_digest(doc.request_key or "", request_key or ""):
        frappe.throw(_("Signup {0} was not found.").format(request), frappe.DoesNotExistError)
    site = frappe.get_doc("Suite Site", doc.suite_site) if doc.suite_site else None
    return {
        "status": doc.status,
        "stage": site.status if site else None,
        "site_url": site.url if site and doc.status == "Completed" else None,
    }


def assert_signup_open() -> None:
    if not cint(get_config("allow_signup")):
        frappe.throw(_("Signup is closed."), SignupClosedError)


def assert_password_strength(password: str, user_inputs: list[str]) -> None:
    result = test_password_strength(password or "", user_inputs=user_inputs)
    minimum = cint(frappe.db.get_single_value("System Settings", "minimum_password_score")) or 2
    if cint(result.get("score")) < minimum:
        feedback = (result.get("feedback") or {}).get("warning") or _(
            "Choose a longer, less common password."
        )
        frappe.throw(_("The password is too weak. {0}").format(feedback))


def get_request(name: str):
    if not name or not frappe.db.exists("Suite Tenant Request", name):
        frappe.throw(_("Signup {0} was not found.").format(name), frappe.DoesNotExistError)
    return frappe.get_doc("Suite Tenant Request", name)
