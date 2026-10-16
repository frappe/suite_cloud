"""The label a tenant picks: ``acme`` names its site domain and its mail domain.

The mail domain is ``acme.<tenant mail zone>``. The site domain is ``acme.<site zone>``, or
``acme.sites.<zone>`` when both are one zone: the site's CNAME cannot share a name with the mail
domain's MX and TXT records.
"""

import re

import frappe
from frappe import _

from suite_cloud.mail.tenancy.addresses import DOMAIN_LABEL
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host
from suite_cloud.utils import get_config

SHARED_ZONE_LABEL = "sites"  # where site domains go when sites and mail share a zone
RESERVED = {"admin", "atlas", "cargo", "proxy", "site", "www", "mail", "spf", "out", SHARED_ZONE_LABEL}
RESERVED_PATTERN = re.compile(r"^(spf\d+|proxy-.*|site-.*|.*-vm-.*)$")


class LabelNotAvailableError(frappe.ValidationError):
    http_status_code = 409


def validate_label(label: str | None) -> str:
    label = (label or "").strip().lower()
    if not DOMAIN_LABEL.match(label):
        frappe.throw(_("{0} is not a valid name: use lowercase letters, digits and dashes.").format(label))
    return label


def site_domain(label: str) -> str | None:
    site_zone, mail_zone = get_config(("site_dns_zone", "tenant_mail_zone"))
    if not site_zone:
        return None
    return f"{label}.{SHARED_ZONE_LABEL}.{site_zone}" if site_zone == mail_zone else f"{label}.{site_zone}"


def mail_domain(label: str) -> str | None:
    zone = get_config("tenant_mail_zone")
    return f"{label}.{zone}" if zone else None


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
    for domain, zone in (
        (site_domain(label), get_config("site_dns_zone")),
        (mail_domain(label), get_config("tenant_mail_zone")),
    ):
        if domain and (
            frappe.db.exists("Mail Domain", domain) or has_records(zone, relative_host(domain, zone))
        ):
            return True
    return False


def has_records(zone: str, host: str) -> bool:
    """A record at the host or under it, e.g. ``_frappe-verification.acme``."""

    return bool(
        frappe.db.exists("DNS Record", {"dns_zone": zone, "host": host})
        or frappe.db.exists("DNS Record", {"dns_zone": zone, "host": ["like", f"%.{host}"]})
    )
