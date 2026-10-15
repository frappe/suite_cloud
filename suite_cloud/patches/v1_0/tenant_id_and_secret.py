from uuid import UUID, uuid7

import frappe
from frappe.utils.password import rename_password_field

# A tenant is named by a UUIDv7 and authenticates with its id and secret, so the id doubles as
# its domain verification token. Existing tenants, named after their site, are renamed; their
# links, comments and versions follow through rename_doc.


def execute() -> None:
    rename_password_field("Suite Tenant", "api_secret", "secret")

    for tenant in frappe.get_all("Suite Tenant", ["name", "title"]):
        if is_uuid(tenant.name):
            continue
        if not tenant.title:
            frappe.db.set_value("Suite Tenant", tenant.name, "title", tenant.name, update_modified=False)
        frappe.rename_doc("Suite Tenant", tenant.name, str(uuid7()), force=True, show_alert=False)


def is_uuid(name: str) -> bool:
    try:
        UUID(name)
    except ValueError:
        return False
    return True
