import frappe
from frappe.utils import cint

from suite_cloud.mail.stalwart import get_client
from suite_cloud.mail.tenancy.app_passwords import DEFAULT_LIMIT, QUOTA
from suite_cloud.utils import child_rows

# Accounts created before the app password limit existed get the default, on the cluster too.
# Rows are written directly: a save would also mint credentials for the new mirror.


def execute() -> None:
    accounts = frappe.get_all("Mail Account", fields=["name", "cluster", "stalwart_id"])
    quotas = child_rows("Mail Quota", "Mail Account", [a.name for a in accounts], ["quota", "value"])

    updates: dict[str, dict[str, dict]] = {}
    for account in accounts:
        rows = quotas[account.name]
        if any(row.quota == QUOTA for row in rows):
            continue
        frappe.get_doc(
            {
                "doctype": "Mail Quota",
                "parenttype": "Mail Account",
                "parent": account.name,
                "parentfield": "quotas",
                "idx": len(rows) + 1,
                "quota": QUOTA,
                "value": DEFAULT_LIMIT,
            }
        ).db_insert()
        if account.stalwart_id:
            quota_map = {row.quota: cint(row.value) for row in rows} | {QUOTA: DEFAULT_LIMIT}
            updates.setdefault(account.cluster, {})[account.stalwart_id] = {"quotas": quota_map}

    for cluster_name, patches in updates.items():
        get_client(frappe.get_doc("Stalwart Cluster", cluster_name)).accounts.update_many(patches)
