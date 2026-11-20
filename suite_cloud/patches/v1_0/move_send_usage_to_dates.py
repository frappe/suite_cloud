import frappe


def execute() -> None:
    """Mail Send Usage keeps one row per account and UTC day, keyed on a date. A day row
    (``period`` ``d:YYYYMMDD``) takes its date and the name that goes with it; a month row
    (``m:YYYYMM``) goes, since a month is now the sum of its days. Then the old key goes too."""

    if not frappe.db.has_column("Mail Send Usage", "period"):
        return
    frappe.db.sql("delete from `tabMail Send Usage` where period not like %s", "d:%")
    frappe.db.sql(
        "update `tabMail Send Usage` set `date` = str_to_date(substring(period, 3), %s) where `date` is null",
        "%Y%m%d",
    )
    frappe.db.sql("delete from `tabMail Send Usage` where `date` is null")
    frappe.db.sql("update `tabMail Send Usage` set name = sha1(concat(account, ':', `date`))")
    # Dropping the column alone would leave the unique key on the account by itself.
    if frappe.db.has_index("tabMail Send Usage", "account_period"):
        frappe.db.sql_ddl("alter table `tabMail Send Usage` drop index account_period")
    frappe.db.sql_ddl("alter table `tabMail Send Usage` drop column period")
