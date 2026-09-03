"""Address rules shared by accounts, groups and lists: syntax, ownership and uniqueness."""

import re

import frappe
from frappe import _
from frappe.utils import validate_email_address as frappe_validate_email

DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
ADDRESS_DOCTYPES = ("Mail Account", "Mail Group", "Mailing List")


def validate_domain_name(value: str | None) -> str:
    domain = (value or "").strip().lower().rstrip(".")
    try:
        domain = domain.encode("idna").decode()
    except UnicodeError:
        frappe.throw(_("{0} is not a valid domain name.").format(value))

    labels = domain.split(".")
    if len(labels) < 2 or not all(DOMAIN_LABEL.match(label) for label in labels) or len(domain) > 253:
        frappe.throw(_("{0} is not a valid domain name.").format(value))
    return domain


def validate_email_address(value: str | None) -> str:
    email = (value or "").strip().lower()
    if not frappe_validate_email(email) or email.count("@") != 1:
        frappe.throw(_("{0} is not a valid email address.").format(value))
    local, domain = email.split("@", 1)
    if "%" in local or "/" in local:  # '%' is Stalwart's master-user separator
        frappe.throw(_("{0} is not a valid email address.").format(value))
    return f"{local}@{validate_domain_name(domain)}"


def get_site_domain(site: str, domain_name: str):
    """The site's Mail Domain named ``domain_name``; other sites' domains are invisible (404)."""

    domain = (
        frappe.get_cached_doc("Mail Domain", domain_name)
        if frappe.db.exists("Mail Domain", domain_name)
        else None
    )
    if domain is None or domain.site != site:
        frappe.throw(
            _("Domain {0} does not belong to this site.").format(domain_name), frappe.DoesNotExistError
        )
    return domain


def assert_address_available(email: str, exclude: tuple[str, str] | None = None) -> None:
    """An address may be a primary address or an alias exactly once across the whole directory."""

    for doctype in ADDRESS_DOCTYPES:
        if doctype == (exclude or (None,))[0] and exclude[1] == email:
            continue
        if frappe.db.exists(doctype, email):
            frappe.throw(
                _("{0} is already used by a {1}.").format(email, _(doctype)), frappe.DuplicateEntryError
            )

    alias = frappe.db.get_value(
        "Mail Address Alias", {"alias_email": email}, ["parenttype", "parent"], as_dict=True
    )
    if alias and (alias.parenttype, alias.parent) != exclude:
        frappe.throw(
            _("{0} is already an alias of {1}.").format(email, alias.parent), frappe.DuplicateEntryError
        )
