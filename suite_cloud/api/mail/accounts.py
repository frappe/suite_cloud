import frappe
from frappe.utils import sbool

from suite_cloud.api.mail import aliases
from suite_cloud.api.site import (
    as_alias_rows,
    as_list,
    current_site,
    owned,
    owned_page,
    page_size,
    site_api,
)
from suite_cloud.cloud_mail.doctype.mail_account.mail_account import (
    mailing_lists_by_account,
    validate_password,
)
from suite_cloud.cloud_mail.tenancy import quotas as quota_rows
from suite_cloud.cloud_mail.tenancy import sync
from suite_cloud.cloud_mail.tenancy.usage import used_disk_by_name

ACCOUNT_PAGE_CAP = 200


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_accounts(
    domain: str | None = None, search: str | None = None, start: int = 0, limit: int = 50
) -> dict:
    filters = {"domain": owned("Mail Domain", domain).name} if domain else None
    names, total = owned_page(
        "Mail Account", search, start, limit, ACCOUNT_PAGE_CAP, ("name", "display_name"), filters
    )
    accounts = [frappe.get_doc("Mail Account", n) for n in names]
    lists = mailing_lists_by_account(accounts)
    usage = used_disk_by_name(accounts)
    items = [
        a.to_api(mailing_lists=lists.get(a.name, []), used_disk_bytes=usage.get(a.name)) for a in accounts
    ]
    return {"items": items, "total": total}


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_account(email: str) -> dict:
    return owned("Mail Account", email).to_api(with_usage=True)


QUOTA_LOOKUP_LIMIT = 500


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_quotas(emails: list[str] | str) -> dict:
    """``{email: {disk_quota_gb, used_disk_bytes}}`` for the site's accounts among ``emails``.

    The allotment is one query; usage is one cluster call per ``maxObjectsInGet`` accounts, so a
    list page shows both for every row.
    """

    wanted = [e.strip().lower() for e in as_list(emails) if e and e.strip()][:QUOTA_LOOKUP_LIMIT]
    if not wanted:
        return {}
    rows = frappe.get_all(
        "Mail Account",
        filters={"site": current_site().name, "name": ["in", wanted]},
        fields=["name", "disk_quota_gb", "stalwart_id", "cluster"],
    )
    usage = used_disk_by_name(rows)
    return {
        row.name: {"disk_quota_gb": row.disk_quota_gb, "used_disk_bytes": usage.get(row.name)} for row in rows
    }


@frappe.whitelist(methods=["POST"])
@site_api
def create_account(
    email: str,
    password: str,
    display_name: str | None = None,
    description: str | None = None,
    aliases: list | str | None = None,
    groups: list[str] | str | None = None,
    mailing_lists: list[str] | str | None = None,
    disk_quota_gb: float | None = None,
    quotas: dict | str | None = None,
    locale: str | None = None,
    time_zone: str | None = None,
) -> dict:
    validate_password(password)

    site = current_site()
    # Everything the account depends on is resolved first: a refusal after the insert would leave
    # the Stalwart account behind while the database change rolls back.
    lists = [owned("Mailing List", email_) for email_ in as_list(mailing_lists)]
    doc = frappe.get_doc(
        {
            "doctype": "Mail Account",
            "email": email,
            "site": site.name,
            "display_name": display_name,
            "description": description,
            "disk_quota_gb": disk_quota_gb,
            "quotas": quota_rows.as_rows(quotas),
            "locale": locale or "en-US",
            "time_zone": time_zone,
            "aliases": as_alias_rows(aliases),
            "groups": [{"group": owned("Mail Group", g).name} for g in as_list(groups)],
        }
    )
    doc.flags.password = password
    doc.insert(ignore_permissions=True)

    # The list memberships are separate cluster calls after the insert: if one fails the row
    # rolls back, so the cluster account must go too or a retry meets "already exists".
    try:
        for mailing_list in lists:
            mailing_list.add_recipients([doc.email])
    except Exception:
        sync.push_destroy(doc, "accounts")
        raise

    frappe.local.response["http_status_code"] = 201
    # The app password is minted on creation and returned once; rotate_app_password issues a
    # fresh one later. Suite Cloud keeps it, the account's own password it never does.
    return {**doc.to_api(), "app_password": doc.get_password("app_password")}


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def update_account(
    email: str,
    display_name: str | None = None,
    description: str | None = None,
    disk_quota_gb: float | None = None,
    quotas: dict | str | None = None,
    locale: str | None = None,
    time_zone: str | None = None,
) -> dict:
    """``quotas`` replaces the whole set of other limits; pass ``{}`` to lift them all."""

    doc = owned("Mail Account", email)
    for field, value in {
        "display_name": display_name,
        "description": description,
        "disk_quota_gb": disk_quota_gb,
        "locale": locale,
        "time_zone": time_zone,
    }.items():
        if value is not None:
            doc.set(field, value)
    if quotas is not None:
        doc.set("quotas", quota_rows.as_rows(quotas))
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def set_account_enabled(email: str, enabled: bool) -> dict:
    doc = owned("Mail Account", email)
    doc.set_enabled(bool(enabled))
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def set_password(email: str, password: str) -> None:
    owned("Mail Account", email).set_password(password)


@frappe.whitelist(methods=["POST"])
@site_api
def rotate_app_password(email: str) -> dict:
    """Mints a new app password for the account, revokes the previous one, and returns it once."""

    return {"app_password": owned("Mail Account", email).mint_credential("app_password")}


@frappe.whitelist(methods=["POST"])
@site_api
def create_app_password(email: str, description: str = "Suite") -> dict:
    """The secret is returned once and never stored by Suite Cloud."""

    return {"secret": owned("Mail Account", email).create_app_password(description)}


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_aliases(email: str, aliases: list | str | None = None) -> dict:
    doc = owned("Mail Account", email)
    doc.set("aliases", as_alias_rows(aliases))
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def add_alias(email: str, alias: str, description: str | None = None) -> dict:
    return aliases.add("Mail Account", email, alias, description).to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def remove_alias(email: str, alias: str) -> dict:
    return aliases.remove("Mail Account", email, alias).to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_alias_enabled(email: str, alias: str, enabled: bool) -> dict:
    return aliases.set_enabled("Mail Account", email, alias, sbool(enabled)).to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def set_groups(email: str, groups: list[str] | str | None = None) -> dict:
    doc = owned("Mail Account", email)
    doc.set("groups", [{"group": owned("Mail Group", g).name} for g in as_list(groups)])
    doc.save(ignore_permissions=True)
    return doc.to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def delete_account(email: str) -> None:
    owned("Mail Account", email).delete(ignore_permissions=True)
