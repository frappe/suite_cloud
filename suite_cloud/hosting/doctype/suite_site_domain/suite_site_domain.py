# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SuiteSiteDomain(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        dns_records: DF.JSON | None
        domain: DF.Data
        failure_reason: DF.SmallText | None
        is_primary: DF.Check
        managed: DF.Check
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        primary_skipped: DF.Check
        records_fetched_at: DF.Datetime | None
        status: DF.Literal["Pending", "Active", "Failed"]
        task: DF.Data | None
    # end: auto-generated types
