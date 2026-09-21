# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from uuid import uuid7

from frappe.model.document import Document


class TLSReportFailure(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        additional_information: DF.SmallText | None
        failed_sessions: DF.Int
        failure_reason_code: DF.SmallText | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        policy_domain: DF.Data | None
        policy_type: DF.Data | None
        receiving_ip: DF.Data | None
        receiving_mx_helo: DF.SmallText | None
        receiving_mx_hostname: DF.Data | None
        result_type: DF.Data | None
        sending_mta_ip: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())
