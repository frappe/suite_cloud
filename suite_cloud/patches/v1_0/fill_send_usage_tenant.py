import frappe


def execute() -> None:
    """Mail Send Usage keeps each row's tenant, so a tenant's totals outlive its deleted accounts.
    Existing rows take their account's tenant; an account deleted before took its rows with it."""

    frappe.db.sql(
        """update `tabMail Send Usage` usage_row
        join `tabMail Account` account on account.name = usage_row.account
        set usage_row.tenant = account.tenant
        where usage_row.tenant is null"""
    )
