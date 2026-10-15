import frappe
from frappe.model.utils.rename_field import rename_field

DOCTYPES = (
    "Mail Domain",
    "Mail Account",
    "Mail Group",
    "Mailing List",
    "Mailing List Recipient",
    "DMARC Report",
    "TLS Report",
)


def execute() -> None:
    for doctype in DOCTYPES:
        rename_field(doctype, "site", "tenant")
