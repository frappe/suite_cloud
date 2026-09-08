import frappe

# Mailing List Recipient used to be a child table of Mailing List. It is a standalone DocType now
# (lists can hold hundreds of thousands of addresses); rows created as child rows still carry the
# parent columns and get the new link fields from them.


def execute() -> None:
    frappe.reload_doc("suite_cloud", "doctype", "mailing_list_recipient")
    rows = frappe.db.sql(
        """select name, parent from `tabMailing List Recipient`
           where ifnull(mailing_list, '') = '' and ifnull(parent, '') != ''""",
        as_dict=True,
    )
    for row in rows:
        site = frappe.db.get_value("Mailing List", row.parent, "site")
        frappe.db.set_value(
            "Mailing List Recipient",
            row.name,
            {"mailing_list": row.parent, "site": site, "enabled": 1, "parent": None, "parenttype": None},
            update_modified=False,
        )
