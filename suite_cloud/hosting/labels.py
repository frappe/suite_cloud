"""The label a tenant picks: ``acme`` names ``acme.<site zone>`` and ``acme.<tenant mail zone>``."""

import re

import frappe
from frappe import _

from suite_cloud.mail.tenancy.addresses import DOMAIN_LABEL
from suite_cloud.utils import get_config

RESERVED = {"admin", "atlas", "cargo", "proxy", "site", "www", "mail", "spf", "out"}
RESERVED_PATTERN = re.compile(r"^(spf\d+|proxy-.*|site-.*|.*-vm-.*)$")


class LabelNotAvailableError(frappe.ValidationError):
    http_status_code = 409


def validate_label(label: str | None) -> str:
    label = (label or "").strip().lower()
    if not DOMAIN_LABEL.match(label):
        frappe.throw(_("{0} is not a valid name: use lowercase letters, digits and dashes.").format(label))
    return label


def assert_label_available(label: str, request: str | None = None) -> None:
    """Free across sites, open signups, clusters and both zones' records."""

    if label in RESERVED or RESERVED_PATTERN.match(label) or is_taken(label, request):
        frappe.throw(_("{0} is not available.").format(label), LabelNotAvailableError)


def is_taken(label: str, request: str | None) -> bool:
    if frappe.db.exists("Stalwart Cluster", {"label": label}):
        return True
    if frappe.db.exists("Suite Site", {"label": label, "status": ["!=", "Dropped"]}):
        return True
    open_request = {"label": label, "status": ["in", ["Pending", "Verified", "Processing"]]}
    if request:
        open_request["name"] = ["!=", request]
    if frappe.db.exists("Suite Tenant Request", open_request):
        return True
    for zone in {get_config("site_dns_zone"), get_config("tenant_mail_zone")} - {None, ""}:
        domain = f"{label}.{zone}"
        if frappe.db.exists("Mail Domain", domain) or has_records(zone, label):
            return True
    return False


def has_records(zone: str, label: str) -> bool:
    """A record at the label or under it, e.g. ``_frappe-verification.acme``."""

    return bool(
        frappe.db.exists("DNS Record", {"dns_zone": zone, "host": label})
        or frappe.db.exists("DNS Record", {"dns_zone": zone, "host": ["like", f"%.{label}"]})
    )
