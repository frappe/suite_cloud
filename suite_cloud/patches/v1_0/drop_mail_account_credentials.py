import frappe

# The site's app password and the API key are minted and handed over once; the encrypted copies
# kept so far go.


def execute() -> None:
    frappe.db.delete("__Auth", {"doctype": "Mail Account", "fieldname": ["in", ["app_password", "api_key"]]})
