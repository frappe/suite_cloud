"""Public signup: a visitor asks for a Suite workspace, proves their email and gets a site.

Every method is a guest POST: the code and the request key travel in the body, never in a URL.
The owner is invited from their new site and chooses their own password there.
"""

import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint

from suite_cloud.utils import get_config


class SignupClosedError(frappe.PermissionError):
    pass


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=60 * 60)
def create_request(
    email: str, full_name: str, organization: str, label: str | None = None, timezone: str | None = None
) -> dict:
    """Stores the request and mails a one-time code to ``email``."""

    assert_signup_open()
    request = frappe.get_doc(
        {
            "doctype": "Suite Tenant Request",
            "email": email,
            "full_name": full_name,
            "organization": organization,
            "label": label,
            "timezone": timezone,
            "ip_address": frappe.local.request_ip,
        }
    )
    request.flags.verify_by_otp = True  # checked in validate, like a request made in the desk
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
    """Where the workspace stands. Once Completed the owner has been invited from ``site_url``."""

    doc = get_request(request)
    if not secrets.compare_digest(doc.request_key or "", request_key or ""):
        frappe.throw(_("Signup {0} was not found.").format(request), frappe.DoesNotExistError)
    site = frappe.get_doc("Suite Site", doc.suite_site) if doc.suite_site else None
    return {
        "status": doc.status,
        "stage": site.status if site else None,
        "label": doc.label,  # set once processing starts, unless given
        "site_url": site.url if site and doc.status == "Completed" else None,
    }


def assert_signup_open() -> None:
    if not cint(get_config("allow_signup")):
        frappe.throw(_("Signup is closed."), SignupClosedError)


def get_request(name: str):
    if not name or not frappe.db.exists("Suite Tenant Request", name):
        frappe.throw(_("Signup {0} was not found.").format(name), frappe.DoesNotExistError)
    return frappe.get_doc("Suite Tenant Request", name)
