from frappe.model.utils.rename_field import rename_field


def execute() -> None:
    rename_field("Suite Tenant", "cluster", "stalwart_cluster")
