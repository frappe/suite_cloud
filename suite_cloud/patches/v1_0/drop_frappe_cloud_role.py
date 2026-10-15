import frappe

ROLE = "Frappe Cloud"


def execute() -> None:
    frappe.db.delete("Has Role", {"role": ROLE})
    frappe.delete_doc("Role", ROLE, force=True, ignore_missing=True)
