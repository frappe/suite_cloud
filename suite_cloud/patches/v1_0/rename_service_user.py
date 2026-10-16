import frappe

OLD = "suite-site@suite-cloud.internal"
NEW = "suite-tenant@suite-cloud.internal"


def execute() -> None:
    if frappe.db.exists("User", OLD) and not frappe.db.exists("User", NEW):
        frappe.rename_doc("User", OLD, NEW, force=True, show_alert=False)
