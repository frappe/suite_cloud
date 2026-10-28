import frappe
from frappe.model.rename_doc import get_link_fields, update_link_field_values
from pypika.functions import Replace

# The module was renamed from "Cloud Mail" to "Mail". A standard Module Def cannot be renamed, and
# the migrate registers the new row before the patches run, so the old row's links are repointed
# by hand and the row is dropped. Server Job stores the dotted path of its variables builder,
# which moved with the package.
OLD = "Cloud Mail"
NEW = "Mail"


def execute() -> None:
    job = frappe.qb.DocType("Server Job")
    (
        frappe.qb.update(job)
        .set(
            job.variables_builder,
            Replace(job.variables_builder, "suite_cloud.cloud_mail.", "suite_cloud.mail."),
        )
        .where(job.variables_builder.like("suite_cloud.cloud_mail.%"))
        .run()
    )

    update_link_field_values(get_link_fields("Module Def"), OLD, NEW, "Module Def")
    frappe.delete_doc("Module Def", OLD, force=True, ignore_missing=True)
