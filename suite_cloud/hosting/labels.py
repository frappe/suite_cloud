"""The label that names a tenant's site domain and mail domain, e.g. ``acme``: given, or made from
the organization's name when its request is processed.

The mail domain is ``acme.<tenant mail zone>``. The site domain is ``acme.<site zone>``, or
``acme.sites.<zone>`` when both are one zone: the site's CNAME cannot share a name with the mail
domain's MX and TXT records.
"""

import re
import unicodedata

import frappe
from frappe import _

from suite_cloud.hosting import checkpoint
from suite_cloud.mail.tenancy.addresses import DOMAIN_LABEL
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host
from suite_cloud.utils import get_config

SHARED_ZONE_LABEL = "sites"  # where site domains go when sites and mail share a zone
RESERVED = {"admin", "atlas", "cargo", "proxy", "site", "www", "mail", "spf", "out", SHARED_ZONE_LABEL}
RESERVED_PATTERN = re.compile(r"^(spf\d+|proxy-.*|site-.*|.*-vm-.*)$")
OPEN_STATUSES = ("Pending", "Verified", "Processing")  # requests in these hold their label
GENERATED_LENGTH = 30  # it is in every address of the tenant's mail domain
# Dropped from the end of an organization's name: Acme Inc. gives acme.
LEGAL_SUFFIXES = {
    "inc",
    "llc",
    "llp",
    "ltd",
    "limited",
    "pvt",
    "private",
    "corp",
    "co",
    "gmbh",
    "plc",
    "ag",
    "bv",
    "sa",
}


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


def login_local_part(email: str) -> str:
    """The local part of the owner's account on the site: their own, without a +tag, which Suite does
    not take for an account."""

    return email.split("@", 1)[0].split("+", 1)[0]


def assert_owner_name_free(email: str) -> None:
    if login_local_part(email) == get_config("notification_mailbox_local_part"):
        # The owner's account would be the site's own notification account.
        frappe.throw(
            _("{0} cannot own a hosted site: its name is kept for the site's notifications.").format(email)
        )


def label_from(organization: str) -> str:
    """The organization's name in lowercase ASCII words joined by dashes, legal suffixes left out:
    Acme Inc. gives acme. A reserved result is prefixed with ``team-``."""

    text = unicodedata.normalize("NFKD", organization or "").encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", text) or ["workspace"]
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if len(words) > 1:
        words = [word for word in words if word != "vm"] or ["vm"]  # *-vm-* is a server's name
    label = join_within(words, GENERATED_LENGTH)
    return join_within(["team", *words], GENERATED_LENGTH) if is_reserved(label) else label


def join_within(words: list[str], length: int) -> str:
    """As many whole words as fit, so no word is cut into a reserved part such as vm."""

    label = words[0][:length]
    for word in words[1:]:
        if len(label) + 1 + len(word) > length:
            break
        label = f"{label}-{word}"
    return label


def generate_label(organization: str, request: str | None = None) -> str:
    """The first free one of acme, acme-2, acme-3 and so on."""

    base = label_from(organization)
    for number in range(1, 100):
        suffix = f"-{number}"
        label = base if number == 1 else join_within(base.split("-"), GENERATED_LENGTH - len(suffix)) + suffix
        if not is_reserved(label) and not is_taken(label, request):
            return label
    frappe.throw(_("No label is free for {0}; give one.").format(organization), LabelNotAvailableError)


def reserve_label(request) -> str:
    """The request's label, held from now on: its own if still free, else one made from its
    organization. One request at a time, committed before the next, so no two pick one label."""

    with frappe.cache.lock(f"{frappe.local.site}:suite_cloud:labels", timeout=60, blocking_timeout=60):
        if request.label:
            # Held now only by requests already processing: of two that gave it, the first wins.
            assert_label_available(request.label, request.name, request.tenant, holders=("Processing",))
        else:
            request.db_set("label", generate_label(request.organization, request.name))
        request.db_set("status", "Processing")  # holds the label for the requests after it
        checkpoint()
    return request.label


def assert_label_available(
    label: str, request: str | None = None, tenant: str | None = None, holders: tuple = OPEN_STATUSES
) -> None:
    """Free across sites, requests in ``holders``, clusters and both zones' records. A ``tenant``'s
    own hosted mail domain, kept from a site it dropped, leaves the label free for that tenant."""

    if is_reserved(label) or is_taken(label, request, tenant, holders):
        frappe.throw(_("{0} is not available.").format(label), LabelNotAvailableError)


def is_reserved(label: str) -> bool:
    return label in RESERVED or bool(RESERVED_PATTERN.match(label))


def is_taken(
    label: str, request: str | None, tenant: str | None = None, holders: tuple = OPEN_STATUSES
) -> bool:
    if frappe.db.exists("Stalwart Cluster", {"label": label}):
        return True
    if frappe.db.exists("Suite Site", {"label": label, "status": ["!=", "Dropped"]}):
        return True
    open_request = {"label": label, "status": ["in", list(holders)]}
    if request:
        open_request["name"] = ["!=", request]
    if frappe.db.exists("Suite Tenant Request", open_request):
        return True
    for domain, zone in (
        (site_domain(label), get_config("site_dns_zone")),
        (mail_domain(label), get_config("tenant_mail_zone")),
    ):
        if domain and is_domain_held(domain, zone, tenant):
            return True
    return False


def is_domain_held(domain: str, zone: str, tenant: str | None) -> bool:
    if tenant and frappe.db.exists("Mail Domain", {"name": domain, "tenant": tenant, "hosted": 1}):
        return False  # the tenant's own, with its records
    return bool(frappe.db.exists("Mail Domain", domain) or has_records(zone, relative_host(domain, zone)))


def has_records(zone: str, host: str) -> bool:
    """A record at the host or under it, e.g. ``_frappe-verification.acme``."""

    return bool(
        frappe.db.exists("DNS Record", {"dns_zone": zone, "host": host})
        or frappe.db.exists("DNS Record", {"dns_zone": zone, "host": ["like", f"%.{host}"]})
    )
