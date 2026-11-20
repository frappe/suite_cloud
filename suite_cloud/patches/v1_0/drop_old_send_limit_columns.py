import frappe

# Fields the sending limits no longer have; their columns stay until dropped.
COLUMNS = {
    "Mail Account": ("monthly_send_limit",),
    "Suite Tenant": ("default_daily_send_limit", "default_monthly_send_limit"),
    "Stalwart Cluster": (
        "max_daily_send_limit",
        "max_monthly_send_limit",
        "sending_limit_hook_timeout",
        "quota_backstop_per_day",
    ),
}


def execute() -> None:
    """Runs after ``create_default_mail_send_limits``, which reads the old tenant defaults."""

    for doctype, columns in COLUMNS.items():
        meta = frappe.get_meta(doctype)
        for column in columns:
            if meta.get_field(column) or not frappe.db.has_column(doctype, column):
                continue
            frappe.db.sql_ddl(f"alter table `tab{doctype}` drop column `{column}`")
