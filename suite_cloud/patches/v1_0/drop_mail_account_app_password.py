import frappe

# The Suite site's app password is minted and handed over once; the encrypted copies kept so far go.


def execute() -> None:
    frappe.db.delete("__Auth", {"doctype": "Mail Account", "fieldname": "app_password"})
