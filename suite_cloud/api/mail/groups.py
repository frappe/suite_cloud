import frappe
from frappe import _
from frappe.utils import sbool

from suite_cloud.api.mail import aliases as alias_rows
from suite_cloud.api.site import (
    MEMBERSHIP_CAP,
    as_alias_rows,
    as_list,
    current_site,
    normalize_name,
    owned,
    owned_page,
    site_api,
)
from suite_cloud.cloud_mail.doctype.mail_group.mail_group import group_payloads
from suite_cloud.cloud_mail.tenancy import quotas as quota_rows

PAGE_CAP = 500  # the dashboard's largest page


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_groups(search: str | None = None, start: int = 0, limit: int = 100) -> dict:
    names, total = owned_page("Mail Group", search, start, limit, PAGE_CAP)
    return {"items": group_payloads(names), "total": total}


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_group(email: str) -> dict:
    return owned("Mail Group", email).to_api(with_usage=True)


@frappe.whitelist(methods=["POST"])
@site_api
def create_group(
    email: str,
    description: str | None = None,
    aliases: list | str | None = None,
    members: list[str] | str | None = None,
    disk_quota_gb: float | None = None,
    quotas: dict | str | None = None,
) -> dict:
    wanted = _resolve_members(members)  # before the insert: a refusal must not leave a cluster group behind
    doc = frappe.get_doc(
        {
            "doctype": "Mail Group",
            "email": email,
            "site": current_site().name,
            "description": description,
            "aliases": as_alias_rows(aliases),
        }
    )
    quota_rows.apply(doc, disk_quota_gb, quotas)
    doc.insert(ignore_permissions=True)
    if wanted:
        _set_members(doc, wanted)
    frappe.local.response["http_status_code"] = 201
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def update_group(
    email: str,
    description: str | None = None,
    disk_quota_gb: float | None = None,
    quotas: dict | str | None = None,
) -> dict:
    """``quotas`` replaces the optional limits (``{}`` lifts them all); the disk quota stays unless
    ``disk_quota_gb`` or a ``maxDiskQuota`` entry changes it."""

    doc = owned("Mail Group", email)
    if description is not None:
        doc.description = description
    quota_rows.apply(doc, disk_quota_gb, quotas)
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_group_aliases(email: str, aliases: list | str | None = None) -> dict:
    doc = owned("Mail Group", email)
    doc.set("aliases", as_alias_rows(aliases))
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def add_group_alias(email: str, alias: str, description: str | None = None) -> dict:
    return alias_rows.add("Mail Group", email, alias, description).to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def remove_group_alias(email: str, alias: str) -> dict:
    return alias_rows.remove("Mail Group", email, alias).to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_group_alias_enabled(email: str, alias: str, enabled: bool) -> dict:
    return alias_rows.set_enabled("Mail Group", email, alias, sbool(enabled)).to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_group_members(email: str, members: list[str] | str | None = None) -> dict:
    doc = owned("Mail Group", email)
    _set_members(doc, _resolve_members(members))
    return doc.to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def delete_group(email: str) -> None:
    owned("Mail Group", email).delete(ignore_permissions=True)


def _resolve_members(members: list[str] | str | None) -> set[str]:
    """The site's account names behind ``members``; any other address is not found."""

    wanted = {normalize_name(m) for m in as_list(members, MEMBERSHIP_CAP)}
    if not wanted:
        return set()
    site = current_site().name
    known = set(frappe.get_all("Mail Account", {"site": site, "name": ["in", list(wanted)]}, pluck="name"))
    if missing := sorted(wanted - known):
        raise frappe.DoesNotExistError(_("{0} {1} not found.").format(_("Mail Account"), missing[0]))
    return wanted


def _set_members(group, wanted: set[str]) -> None:
    """Membership lives on the accounts; add the group to new members and drop it from the rest.

    Only the accounts whose membership changes are loaded, each locked for the change, and each
    saved on its own: a refusal from the cluster rolls the request back, and the reconcile job
    reports any account the cluster kept in the group.
    """

    current = set(group.member_emails())
    for account_name in sorted(wanted - current):
        account = frappe.get_doc("Mail Account", account_name, for_update=True)
        account.append("groups", {"group": group.name})
        account.save(ignore_permissions=True)
    for account_name in sorted(current - wanted):
        account = frappe.get_doc("Mail Account", account_name, for_update=True)
        account.set("groups", [row for row in account.groups if row.group != group.name])
        account.save(ignore_permissions=True)
