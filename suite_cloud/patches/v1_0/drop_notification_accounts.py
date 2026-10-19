import frappe
from frappe.model.utils.rename_field import rename_field

from suite_cloud.utils import log_exception


def execute() -> None:
    """Sites send their mail through Suite Cloud now: the mail step ends at a verified domain, and
    the ``notification@`` accounts go, from Stalwart too, in the background."""

    rename_field("Suite Site", "email_account_created", "mail_domain_verified")
    if not frappe.db.has_column("Mail Account", "is_system_account"):
        return
    names = frappe.db.sql_list("select name from `tabMail Account` where is_system_account = 1")
    if names:
        frappe.enqueue(
            "suite_cloud.patches.v1_0.drop_notification_accounts.delete_accounts",
            queue="long",
            enqueue_after_commit=True,
            names=names,
        )


def delete_accounts(names: list[str]) -> None:
    for name in names:
        try:
            frappe.delete_doc("Mail Account", name, ignore_permissions=True, ignore_missing=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            log_exception(f"Notification account {name} could not be deleted")
