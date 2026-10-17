# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SuiteTenantRequestStep(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        detail: DF.SmallText | None
        duration: DF.Duration | None
        ended_at: DF.Datetime | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        started_at: DF.Datetime | None
        status: DF.Literal["Pending", "Running", "Completed", "Skipped", "Failed"]
        step: DF.Data
        title: DF.Data
    # end: auto-generated types
