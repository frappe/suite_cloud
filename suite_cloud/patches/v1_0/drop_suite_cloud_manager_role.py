import frappe

ROLE = "Suite Cloud Manager"


def execute() -> None:
    """Operators are System Managers; the extra desk role is gone."""

    frappe.db.delete("Has Role", {"role": ROLE})
    frappe.delete_doc("Role", ROLE, force=True, ignore_missing=True)
