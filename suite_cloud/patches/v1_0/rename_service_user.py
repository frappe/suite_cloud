import frappe

from suite_cloud.install import TENANT_SERVICE_USER

OLD = "suite-site@suite-cloud.internal"


def execute() -> None:
    if frappe.db.exists("User", OLD) and not frappe.db.exists("User", TENANT_SERVICE_USER):
        frappe.rename_doc("User", OLD, TENANT_SERVICE_USER, force=True, show_alert=False)
