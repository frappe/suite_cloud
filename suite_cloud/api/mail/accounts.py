import frappe

from suite_cloud.api.site import as_alias_rows, as_list, current_site, owned, owned_names, site_api
from suite_cloud.cloud_mail.doctype.mail_account.mail_account import validate_password


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_accounts(
    domain: str | None = None, search: str | None = None, start: int = 0, limit: int = 50
) -> dict:
    filters = {"site": current_site().name}
    if domain:
        filters["domain"] = owned("Mail Domain", domain).name
    or_filters = None
    if search:
        like = f"%{search.strip()}%"
        or_filters = [["email", "like", like], ["display_name", "like", like]]

    total = frappe.db.count("Mail Account", filters)  # search narrows the page, not the total
    names = frappe.get_all(
        "Mail Account",
        filters=filters,
        or_filters=or_filters,
        pluck="name",
        order_by="email asc",
        limit_start=int(start),
        limit_page_length=min(int(limit), 200),
    )
    return {"items": [frappe.get_doc("Mail Account", n).to_api() for n in names], "total": total}


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_account(email: str) -> dict:
    return owned("Mail Account", email).to_api(with_usage=True)


QUOTA_LOOKUP_LIMIT = 500


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_quotas(emails: list[str] | str) -> dict:
    """``{email: allotted GB}`` for the site's accounts among ``emails``, from Suite Cloud alone.

    Usage would cost a cluster call per account; the allotment is one query, so a list page can
    show it for every row.
    """

    wanted = [e.strip().lower() for e in as_list(emails) if e and e.strip()][:QUOTA_LOOKUP_LIMIT]
    if not wanted:
        return {}
    rows = frappe.get_all(
        "Mail Account",
        filters={"site": current_site().name, "name": ["in", wanted]},
        fields=["name", "disk_quota_gb"],
    )
    return {row.name: row.disk_quota_gb for row in rows}


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
            "locale": locale or "en-US",
            "time_zone": time_zone,
            "aliases": as_alias_rows(aliases),
            "groups": [{"group": owned("Mail Group", g).name} for g in as_list(groups)],
        }
    )
    doc.flags.password = password
    doc.insert(ignore_permissions=True)

    for mailing_list in lists:
        mailing_list.add_recipients([doc.email])

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
    locale: str | None = None,
    time_zone: str | None = None,
) -> dict:
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
