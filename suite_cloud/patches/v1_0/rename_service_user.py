import frappe

from suite_cloud.install import SITE_SERVICE_USER

OLD = "suite-site@suite-cloud.internal"


def execute() -> None:
    if frappe.db.exists("User", OLD) and not frappe.db.exists("User", SITE_SERVICE_USER):
        frappe.rename_doc("User", OLD, SITE_SERVICE_USER, force=True, show_alert=False)
