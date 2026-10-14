# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from uuid import uuid7

from frappe.model.document import Document


class TLSReportPolicy(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        failed_sessions: DF.Int
        mx_hosts: DF.SmallText | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        policy_domain: DF.Data | None
        policy_strings: DF.SmallText | None
        policy_type: DF.Data | None
        successful_sessions: DF.Int
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())
