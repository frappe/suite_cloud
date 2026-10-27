import frappe

from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import limits_by_account


def execute() -> None:
    """Rows reported before the limit was stored take the account's limit as it is now."""

    rows = frappe.get_all("Mail Send Usage", fields=["name", "account", "period"])
    limits = limits_by_account(list({row.account for row in rows}))
    for row in rows:
        limit = limits.get(row.account, {}).get(row.period[0], 0)
        frappe.db.set_value("Mail Send Usage", row.name, "send_limit", limit, update_modified=False)
