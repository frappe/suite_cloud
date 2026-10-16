from frappe.model.utils.rename_field import rename_field


def execute() -> None:
    rename_field("Suite Tenant", "user", "owner_user")
