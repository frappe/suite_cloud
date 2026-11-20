import frappe
from frappe.utils import cint

from suite_cloud.mail.doctype.mail_send_limit.mail_send_limit import create_default_limits, get_default_limit

OLD_TENANT_DEFAULT = "default_daily_send_limit"


def execute() -> None:
    """Gives each cluster the default Mail Send Limits plus one for each limit its accounts and
    tenant defaults hold, and links them, so no account's limit changes. A tenant without a default
    gets the cluster's. Drops Mail Send Usage's ``refused``, which Suite Cloud no longer counts now
    that Stalwart enforces the limits. The rows are written directly, so no config push runs: Sync
    Config on each cluster does that."""

    for name in frappe.get_all("Stalwart Cluster", pluck="name"):
        cluster = frappe.get_doc("Stalwart Cluster", name)
        accounts = {
            cint(value)
            for value in frappe.get_all("Mail Account", {"cluster": name}, pluck="daily_send_limit")
        }
        tenants = get_old_tenant_defaults(name)
        create_default_limits(cluster, accounts | set(tenants.values()))
        link_accounts(name)
        link_tenants(name, tenants)
    if frappe.db.has_column("Mail Send Usage", "refused"):
        frappe.db.sql_ddl("alter table `tabMail Send Usage` drop column `refused`")


def link_accounts(cluster: str) -> None:
    """One statement per limit; an account under 1, which the fields once let through, takes the
    cluster's default."""

    account = frappe.qb.DocType("Mail Account")
    unlinked = (account.cluster == cluster) & (account.send_limit.isnull() | (account.send_limit == ""))
    limits = frappe.get_all("Mail Send Limit", {"cluster": cluster}, ["name", "daily_limit", "is_default"])
    for limit in limits:
        update_accounts(unlinked & (account.daily_send_limit == limit.daily_limit), limit)
    update_accounts(unlinked, next(limit for limit in limits if limit.is_default))


def update_accounts(condition, limit: dict) -> None:
    account = frappe.qb.DocType("Mail Account")
    (
        frappe.qb.update(account)
        .set(account.send_limit, limit.name)
        .set(account.daily_send_limit, limit.daily_limit)
        .where(condition)
        .run()
    )


def link_tenants(cluster: str, old_defaults: dict[str, int]) -> None:
    default = get_default_limit(cluster)
    for tenant in frappe.get_all(
        "Suite Tenant", {"stalwart_cluster": cluster}, ["name", "default_send_limit"]
    ):
        if tenant.default_send_limit:
            continue
        value = old_defaults.get(tenant.name)
        limit = (
            frappe.db.get_value("Mail Send Limit", {"cluster": cluster, "daily_limit": value})
            if value
            else None
        )
        frappe.db.set_value(
            "Suite Tenant", tenant.name, "default_send_limit", limit or default, update_modified=False
        )


def get_old_tenant_defaults(cluster: str) -> dict[str, int]:
    """The tenants' old Default Daily Send Limit, which the column still holds; 0 is none."""

    if not frappe.db.has_column("Suite Tenant", OLD_TENANT_DEFAULT):
        return {}
    rows = frappe.db.sql(
        f"select name, `{OLD_TENANT_DEFAULT}` from `tabSuite Tenant` where stalwart_cluster = %s", cluster
    )
    return {name: cint(value) for name, value in rows if cint(value) > 0}
