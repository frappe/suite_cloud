# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ServerJobTask(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        duration: DF.Float
        ended_at: DF.Datetime | None
        exception: DF.Code | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        result: DF.JSON | None
        started_at: DF.Datetime | None
        status: DF.Literal["Pending", "Running", "Success", "Failed", "Unreachable", "Skipped"]
        stderr: DF.Code | None
        stdout: DF.Code | None
        task: DF.Data
    # end: auto-generated types

    pass
