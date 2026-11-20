import frappe


def execute() -> None:
    """Mail Send Usage keeps each row's tenant and cluster, so a tenant's totals and the dashboard
    outlive its deleted accounts. Existing rows take their account's; an account deleted before
    took its rows with it."""

    frappe.db.sql(
        """update `tabMail Send Usage` usage_row
        join `tabMail Account` account on account.name = usage_row.account
        set usage_row.tenant = coalesce(usage_row.tenant, account.tenant),
            usage_row.cluster = coalesce(usage_row.cluster, account.cluster)
        where usage_row.tenant is null or usage_row.cluster is null"""
    )
