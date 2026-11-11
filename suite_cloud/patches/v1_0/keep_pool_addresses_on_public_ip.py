import frappe


def execute() -> None:
    """Bind to Private IP is on for new addresses. Addresses made before it bound to their public IP,
    so they keep doing that until a Private IP Address is set."""

    frappe.db.set_value(
        "Egress IP Pool Address",
        {"private_ip_address": ["is", "not set"]},
        "bind_to_private_ip",
        0,
        update_modified=False,
    )
