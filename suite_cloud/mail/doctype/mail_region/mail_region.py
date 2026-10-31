# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

CODE = re.compile(r"^[a-z][a-z0-9]*$")


class MailRegion(Document):
    """A place with a Stalwart cluster, shared by the tenants placed there; its code names the
    cluster's hosts under the DNS zone."""

    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        code: DF.Data
        title: DF.Data
    # end: auto-generated types

    def autoname(self) -> None:
        self.code = (self.code or "").strip().lower()
        if not CODE.match(self.code):
            frappe.throw(_("Code must be lowercase letters and digits, starting with a letter, e.g. bom."))
        self.name = self.code

    def validate(self) -> None:
        self.title = (self.title or "").strip()
