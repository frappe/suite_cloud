import frappe
from frappe.utils import cint

from suite_cloud.mail.doctype.mail_send_limit.mail_send_limit import create_default_limits

OLD_TENANT_DEFAULT = "default_daily_send_limit"


def execute() -> None:
    """Gives each cluster a Mail Send Limit for each limit its accounts and tenant defaults hold,
    and links them, so no account's limit changes. Drops Mail Send Usage's ``refused``, which
    Suite Cloud no longer counts now that Stalwart enforces the limits. The rows are written
    directly, so no config push runs: Sync Config on each cluster does that."""

    for name in frappe.get_all("Stalwart Cluster", pluck="name"):
        cluster = frappe.get_doc("Stalwart Cluster", name)
        maximum = cint(cluster.max_daily_send_limit)
        accounts = {
            cint(value)
            for value in frappe.get_all("Mail Account", {"cluster": name}, pluck="daily_send_limit")
        }
        tenants = get_old_tenant_defaults(name)
        create_default_limits(cluster, {min(value, maximum) for value in accounts | set(tenants.values())})
        link_accounts(name, maximum)
        for tenant, value in tenants.items():
            limit = frappe.db.get_value(
                "Mail Send Limit", {"cluster": name, "daily_limit": min(value, maximum)}
            )
            frappe.db.set_value("Suite Tenant", tenant, "default_send_limit", limit, update_modified=False)
    if frappe.db.has_column("Mail Send Usage", "refused"):
        frappe.db.sql_ddl("alter table `tabMail Send Usage` drop column `refused`")


def link_accounts(cluster: str, maximum: int) -> None:
    """One statement per limit. A limit above the max, which the fields once let through, takes the
    max; one under 1 takes the cluster's default."""

    account = frappe.qb.DocType("Mail Account")
    unlinked = (account.cluster == cluster) & (account.send_limit.isnull() | (account.send_limit == ""))
    limits = frappe.get_all("Mail Send Limit", {"cluster": cluster}, ["name", "daily_limit", "is_default"])
    for limit in limits:
        matches = account.daily_send_limit == limit.daily_limit
        if limit.daily_limit == maximum:
            matches |= account.daily_send_limit > maximum
        update_accounts(unlinked & matches, limit)
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


def get_old_tenant_defaults(cluster: str) -> dict[str, int]:
    """The tenants' old Default Daily Send Limit, which the column still holds; 0 is none."""

    if not frappe.db.has_column("Suite Tenant", OLD_TENANT_DEFAULT):
        return {}
    rows = frappe.db.sql(
        f"select name, `{OLD_TENANT_DEFAULT}` from `tabSuite Tenant` where stalwart_cluster = %s", cluster
    )
    return {name: cint(value) for name, value in rows if cint(value) > 0}
