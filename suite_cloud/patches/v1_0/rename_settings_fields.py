from frappe.model.utils.rename_field import rename_field


def execute() -> None:
    rename_field("Suite Cloud Settings", "site_service_user", "tenant_service_user")
