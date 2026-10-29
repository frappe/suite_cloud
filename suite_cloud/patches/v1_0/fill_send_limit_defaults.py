import frappe
from frappe.utils import now

FIELDS = ("daily_send_limit", "monthly_send_limit")


def execute() -> None:
    """Accounts from before the limits were required hold 0, which the fields now refuse. Each
    gets the field default with a fresh ``modified``, so the Agents pull it."""

    meta = frappe.get_meta("Mail Account")
    account = frappe.qb.DocType("Mail Account")
    for field in FIELDS:
        (
            frappe.qb.update(account)
            .set(account[field], int(meta.get_field(field).default))
            .set(account.modified, now())
            .where(account[field] == 0)
            .run()
        )
