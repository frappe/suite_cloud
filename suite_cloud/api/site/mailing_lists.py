import frappe

from suite_cloud.api.site import as_list, current_site, owned, owned_names, site_api


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_mailing_lists() -> list[dict]:
    return [frappe.get_doc("Mailing List", name).to_api() for name in owned_names("Mailing List")]


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_mailing_list(email: str) -> dict:
    return owned("Mailing List", email).to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def create_mailing_list(
    email: str,
    description: str | None = None,
    aliases: list[str] | str | None = None,
    recipients: list[str] | str | None = None,
) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "Mailing List",
            "email": email,
            "site": current_site().name,
            "description": description,
            "aliases": [{"alias_email": a} for a in as_list(aliases)],
            "recipients": [{"email": r} for r in as_list(recipients)],
        }
    )
    doc.insert(ignore_permissions=True)
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
def set_mailing_list_aliases(email: str, aliases: list[str] | str | None = None) -> dict:
    doc = owned("Mailing List", email)
    doc.set("aliases", [{"alias_email": a} for a in as_list(aliases)])
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_recipients(email: str, recipients: list[str] | str | None = None) -> dict:
    doc = owned("Mailing List", email)
    doc.set("recipients", [{"email": r} for r in as_list(recipients)])
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def delete_mailing_list(email: str) -> None:
    owned("Mailing List", email).delete(ignore_permissions=True)
