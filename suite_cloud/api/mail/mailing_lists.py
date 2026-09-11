import frappe
from frappe.utils import sbool

from suite_cloud.api.mail import aliases as alias_rows
from suite_cloud.api.site import as_alias_rows, as_list, current_site, owned, owned_page, page_size, site_api

PAGE_CAP = 500  # the dashboard's largest page
from suite_cloud.cloud_mail.doctype.mailing_list.mailing_list import list_payloads
from suite_cloud.cloud_mail.tenancy import sync


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_mailing_lists(search: str | None = None, start: int = 0, limit: int = 100) -> dict:
    names, total = owned_page("Mailing List", search, start, limit, PAGE_CAP)
    return {"items": list_payloads(names), "total": total}


@frappe.whitelist(methods=["GET", "POST"])
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
            doc.add_recipients(as_list(recipients))
        except Exception:
            sync.push_destroy(doc, "mailing_lists")  # the row rolls back; the cluster list must too
            raise
    frappe.local.response["http_status_code"] = 201
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def update_mailing_list(email: str, description: str | None = None) -> dict:
    doc = owned("Mailing List", email)
    if description is not None:
        doc.description = description
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_mailing_list_aliases(email: str, aliases: list | str | None = None) -> dict:
    doc = owned("Mailing List", email)
    doc.set("aliases", as_alias_rows(aliases))
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def add_mailing_list_alias(email: str, alias: str, description: str | None = None) -> dict:
    return alias_rows.add("Mailing List", email, alias, description).to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def remove_mailing_list_alias(email: str, alias: str) -> dict:
    return alias_rows.remove("Mailing List", email, alias).to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_mailing_list_alias_enabled(email: str, alias: str, enabled: bool) -> dict:
    return alias_rows.set_enabled("Mailing List", email, alias, sbool(enabled)).to_api()


# --- recipients: standalone documents, so large lists page instead of loading whole ---------------


@frappe.whitelist(methods=["GET", "POST"])
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
        limit_start=int(start),
        limit_page_length=page_size(limit, 1000),
    )
    return {
        "items": [{"email": r.email, "enabled": bool(r.enabled)} for r in rows],
        "total": frappe.db.count("Mailing List Recipient", {"mailing_list": doc.name}),
    }


@frappe.whitelist(methods=["POST"])
@site_api
def add_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    """Adds up to 5000 addresses per call; ones already on the list are skipped."""

    doc = owned("Mailing List", email)
    return {"added": doc.add_recipients(as_list(recipients)[:5000]), "recipient_count": doc.recipient_count()}


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def remove_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    doc = owned("Mailing List", email)
    return {
        "removed": doc.remove_recipients(as_list(recipients)[:5000]),
        "recipient_count": doc.recipient_count(),
    }


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    """Full replace, for small lists; large lists should add and remove incrementally."""

    doc = owned("Mailing List", email)
    doc.set_recipients(as_list(recipients))
    return doc.to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def delete_mailing_list(email: str) -> None:
    owned("Mailing List", email).delete(ignore_permissions=True)
