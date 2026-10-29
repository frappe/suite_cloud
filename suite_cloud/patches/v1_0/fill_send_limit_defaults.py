import frappe
from frappe.utils import now

FIELDS = {"daily_send_limit": "default_daily_send_limit", "monthly_send_limit": "default_monthly_send_limit"}


def execute() -> None:
    """Accounts from before the limits were required hold 0, which the fields now refuse. Each
    gets its cluster's default with a fresh ``modified``, so the Agents pull it."""

    account = frappe.qb.DocType("Mail Account")
    for cluster in frappe.get_all("Stalwart Cluster", fields=["name", *FIELDS.values()]):
        for field, default in FIELDS.items():
            (
                frappe.qb.update(account)
                .set(account[field], cluster[default])
                .set(account.modified, now())
                .where((account.cluster == cluster.name) & (account[field] == 0))
                .run()
            )
