import frappe

DEFAULT = "Suite Site"


def execute() -> None:
    """Tenants from before the field existed are Suite sites: that was the only kind."""

    frappe.db.set_value(
        "Suite Tenant", {"tenant_type": ["in", ("", None)]}, "tenant_type", DEFAULT, update_modified=False
    )
