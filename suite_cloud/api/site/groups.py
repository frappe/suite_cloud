import frappe

from suite_cloud.api.site import as_list, current_site, owned, owned_names, site_api


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_groups() -> list[dict]:
    return [frappe.get_doc("Mail Group", name).to_api() for name in owned_names("Mail Group")]


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_group(email: str) -> dict:
    return owned("Mail Group", email).to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def create_group(
    email: str,
    description: str | None = None,
    aliases: list[str] | str | None = None,
    members: list[str] | str | None = None,
) -> dict:
    member_names = [owned("Mail Account", m).name for m in as_list(members)]
    doc = frappe.get_doc(
        {
            "doctype": "Mail Group",
            "email": email,
            "site": current_site().name,
            "description": description,
            "aliases": [{"alias_email": a} for a in as_list(aliases)],
        }
    )
    doc.insert(ignore_permissions=True)
    if member_names:
        _set_members(doc, member_names)
    frappe.local.response["http_status_code"] = 201
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def update_group(email: str, description: str | None = None) -> dict:
    doc = owned("Mail Group", email)
    if description is not None:
        doc.description = description
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_group_aliases(email: str, aliases: list[str] | str | None = None) -> dict:
    doc = owned("Mail Group", email)
    doc.set("aliases", [{"alias_email": a} for a in as_list(aliases)])
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_group_members(email: str, members: list[str] | str | None = None) -> dict:
    doc = owned("Mail Group", email)
    _set_members(doc, as_list(members))
    return doc.to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def delete_group(email: str) -> None:
    owned("Mail Group", email).delete(ignore_permissions=True)


def _set_members(group, members: list[str]) -> None:
    """Membership lives on the accounts; add the group to new members and drop it from the rest."""

    wanted = {owned("Mail Account", m).name for m in members}
    for account_name in set(group.member_emails()) | wanted:
        account = frappe.get_doc("Mail Account", account_name)
        current = {row.group for row in account.groups}
        if account_name in wanted and group.name not in current:
            account.append("groups", {"group": group.name})
        elif account_name not in wanted and group.name in current:
            account.set("groups", [row for row in account.groups if row.group != group.name])
        else:
            continue
        account.save(ignore_permissions=True)
