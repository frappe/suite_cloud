import frappe

OLD = "Suite Site"
NEW = "Suite Tenant"
OLD_SERVICE_USER = "suite-site@suite-cloud.internal"


def execute() -> None:
    if frappe.db.exists("DocType", OLD) and not frappe.db.exists("DocType", NEW):
        frappe.rename_doc("DocType", OLD, NEW, force=True)
        # Encrypted fields are keyed by DocType and the rename leaves them behind.
        frappe.db.sql("update `__Auth` set `doctype` = %s where `doctype` = %s", (NEW, OLD))

    if frappe.db.exists("Role", OLD) and not frappe.db.exists("Role", NEW):
        frappe.rename_doc("Role", OLD, NEW, force=True)

    if frappe.db.exists("User", OLD_SERVICE_USER):
        frappe.db.set_value(
            "User", OLD_SERVICE_USER, {"first_name": NEW, "full_name": NEW}, update_modified=False
        )
