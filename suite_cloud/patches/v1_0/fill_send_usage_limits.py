import frappe

LIMIT_FIELDS = {"d:%": "daily_send_limit", "m:%": "monthly_send_limit"}


def execute() -> None:
    """Rows reported before the limit was stored take the account's limit as it is now: one
    UPDATE per kind of period."""

    usage = frappe.qb.DocType("Mail Send Usage")
    account = frappe.qb.DocType("Mail Account")
    for pattern, field in LIMIT_FIELDS.items():
        (
            frappe.qb.update(usage)
            .join(account)
            .on(usage.account == account.name)
            .set(usage.send_limit, account[field])
            .where(usage.period.like(pattern))
        ).run()
