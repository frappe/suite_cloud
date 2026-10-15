import frappe
from frappe.utils import cint, sbool

from suite_cloud.api.mail import aliases as alias_rows
from suite_cloud.api.site import (
    RECIPIENT_BATCH,
    as_alias_rows,
    as_list,
    current_site,
    owned,
    owned_page,
    page_size,
    site_api,
)

PAGE_CAP = 500  # the dashboard's largest page
from suite_cloud.mail.doctype.mailing_list.mailing_list import list_payloads
from suite_cloud.mail.tenancy import sync


@frappe.whitelist(methods=["GET"])
@site_api
def list_mailing_lists(search: str | None = None, start: int = 0, limit: int = 100) -> dict:
    names, total = owned_page("Mailing List", search, start, limit, PAGE_CAP)
    return {"items": list_payloads(names), "total": total}


@frappe.whitelist(methods=["GET"])
@site_api
def get_mailing_list(email: str) -> dict:
    return owned("Mailing List", email).to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def create_mailing_list(
    email: str,
    description: str | None = None,
    aliases: list | str | None = None,
    recipients: list[str] | str | None = None,
) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "Mailing List",
            "email": email,
            "site": current_site().name,
            "description": description,
            "aliases": as_alias_rows(aliases),
        }
    )
    doc.insert(ignore_permissions=True)
    if recipients:
        try:
            doc.add_recipients(as_list(recipients, RECIPIENT_BATCH))
        except Exception:
            sync.push_destroy(doc, "mailing_lists")  # the row rolls back; the cluster list must too
            raise
    frappe.local.response["http_status_code"] = 201
    return doc.to_api()


@frappe.whitelist(methods=["PATCH"])
@site_api
def update_mailing_list(email: str, description: str | None = None) -> dict:
    doc = owned("Mailing List", email)
    if description is not None:
        doc.description = description
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["PUT"])
@site_api
def set_aliases(email: str, aliases: list | str | None = None) -> dict:
    doc = owned("Mailing List", email)
    doc.set("aliases", as_alias_rows(aliases))
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def add_alias(email: str, alias: str, description: str | None = None) -> dict:
    return alias_rows.add("Mailing List", email, alias, description).to_api()


@frappe.whitelist(methods=["DELETE"])
@site_api
def remove_alias(email: str, alias: str) -> dict:
    return alias_rows.remove("Mailing List", email, alias).to_api()


@frappe.whitelist(methods=["PUT"])
@site_api
def set_alias_enabled(email: str, alias: str, enabled: bool) -> dict:
    return alias_rows.set_enabled("Mailing List", email, alias, sbool(enabled)).to_api()


# The names the pre-v2 Suite client calls; they go once every site runs the v2 client.
set_mailing_list_aliases = set_aliases
add_mailing_list_alias = add_alias
remove_mailing_list_alias = remove_alias
set_mailing_list_alias_enabled = set_alias_enabled


# --- recipients: standalone documents, so large lists page instead of loading whole ---------------


@frappe.whitelist(methods=["GET"])
@site_api
def list_recipients(email: str, search: str | None = None, start: int = 0, limit: int = 200) -> dict:
    doc = owned("Mailing List", email)
    filters = {"mailing_list": doc.name}
    if search:
        filters["email"] = ["like", f"%{search.strip()}%"]
    rows = frappe.get_all(
        "Mailing List Recipient",
        filters=filters,
        fields=["email", "enabled"],
        order_by="email asc",
        limit_start=max(cint(start), 0),
        limit_page_length=page_size(limit, 1000),
    )
    return {
        "items": [{"email": r.email, "enabled": bool(r.enabled)} for r in rows],
        "total": frappe.db.count("Mailing List Recipient", filters),
    }


@frappe.whitelist(methods=["GET"])
@site_api
def list_all_recipients(start: int = 0, limit: int = 1000) -> dict:
    """Every recipient of every list the site owns, ordered by list then address.

    A site mirrors its lists in a handful of calls this way, instead of one call per list, which
    would exhaust its rate limit on its own once it holds a few hundred lists.
    """

    filters = {"site": current_site().name}
    rows = frappe.get_all(
        "Mailing List Recipient",
        filters=filters,
        fields=["mailing_list", "email", "enabled"],
        order_by="mailing_list asc, email asc",
        limit_start=max(cint(start), 0),
        limit_page_length=page_size(limit, 1000),
    )
    return {
        "items": [
            {"mailing_list": r.mailing_list, "email": r.email, "enabled": bool(r.enabled)} for r in rows
        ],
        "total": frappe.db.count("Mailing List Recipient", filters),
    }


@frappe.whitelist(methods=["POST"])
@site_api
def add_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    """Adds up to 5000 addresses per call; ones already on the list are skipped."""

    doc = owned("Mailing List", email)
    return {
        "added": doc.add_recipients(as_list(recipients, RECIPIENT_BATCH)),
        "recipient_count": doc.recipient_count(),
    }


@frappe.whitelist(methods=["DELETE"])
@site_api
def remove_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    doc = owned("Mailing List", email)
    return {
        "removed": doc.remove_recipients(as_list(recipients, RECIPIENT_BATCH)),
        "recipient_count": doc.recipient_count(),
    }


@frappe.whitelist(methods=["PUT"])
@site_api
def set_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    """Full replace, for small lists; large lists should add and remove incrementally."""

    doc = owned("Mailing List", email)
    doc.set_recipients(as_list(recipients, RECIPIENT_BATCH))
    return doc.to_api()


@frappe.whitelist(methods=["DELETE"])
@site_api
def delete_mailing_list(email: str) -> None:
    owned("Mailing List", email).delete(ignore_permissions=True)
    frappe.local.response["http_status_code"] = 204
