import frappe

from suite_cloud.mail.tenancy.permissions import TENANT_ROLE


def execute() -> None:
    """Every tenant owner holds the Suite Tenant role from now on; give it to the ones who do not."""

    for user in frappe.get_all("Suite Tenant", pluck="user", distinct=True):
        if user and not frappe.db.exists("Has Role", {"parent": user, "role": TENANT_ROLE}):
            frappe.get_doc("User", user).add_roles(TENANT_ROLE)
