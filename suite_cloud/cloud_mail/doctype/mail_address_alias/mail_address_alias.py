# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MailAddressAlias(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        alias_email: DF.Data
        description: DF.Data | None
        enabled: DF.Check
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
    # end: auto-generated types

    pass


def on_doctype_update() -> None:
    # One table serves accounts, groups and lists, so this index makes aliases globally unique.
    frappe.db.add_unique("Mail Address Alias", ["alias_email"])
