# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class DMARCReportRecord(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        disposition: DF.Data | None
        dkim: DF.Data | None
        dkim_results: DF.JSON | None
        envelope_from: DF.Data | None
        envelope_to: DF.Data | None
        header_from: DF.Data | None
        message_count: DF.Int
        override_reasons: DF.SmallText | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        source_ip: DF.Data | None
        spf: DF.Data | None
        spf_results: DF.JSON | None
    # end: auto-generated types

    pass
