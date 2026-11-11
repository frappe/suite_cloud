# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class EgressIPPoolAddress(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        bind_to_private_ip: DF.Check
        ehlo_hostname: DF.Data
        gateway: DF.Link
        ip_address: DF.Data
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        private_ip_address: DF.Data | None
        ptr_verified: DF.Check
    # end: auto-generated types

    @property
    def source_ip(self) -> str:
        """The address the gateway binds to; the provider maps a private one to the public IP."""

        return self.private_ip_address if self.bind_to_private_ip else self.ip_address


def on_doctype_update() -> None:
    # An IP delivers for exactly one pool.
    frappe.db.add_unique("Egress IP Pool Address", ["ip_address"])
