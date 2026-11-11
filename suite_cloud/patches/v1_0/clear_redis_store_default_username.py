import frappe


def execute() -> None:
    """Redis stores got the databases' default Username, stalwart, which nothing used. It would now
    become their login, and a server with only a password refuses that user."""

    frappe.db.set_value(
        "Mail Store",
        {"type": "Redis", "auth_username": "stalwart", "auth_secret": ["is", "not set"]},
        "auth_username",
        None,
        update_modified=False,
    )
