# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from suite_cloud.tenancy.addresses import validate_email_address


class MailingListRecipient(Document):
    """One address on a list. Standalone rather than a child row: lists can hold hundreds of
    thousands of recipients, which a child table could neither save nor search at that size."""

    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        email: DF.Data
        enabled: DF.Check
        mailing_list: DF.Link
        site: DF.Link | None
    # end: auto-generated types

    def validate(self) -> None:
        self.email = validate_email_address(self.email)
        mailing_list = frappe.get_cached_doc("Mailing List", self.mailing_list)
        self.site = mailing_list.site
        if self.email == mailing_list.email:
            frappe.throw(_("A mailing list cannot be its own recipient."))
        if self.is_new() and frappe.db.exists(
            "Mailing List Recipient", {"mailing_list": self.mailing_list, "email": self.email}
        ):
            frappe.throw(
                _("{0} is already a recipient of {1}.").format(self.email, self.mailing_list),
                frappe.DuplicateEntryError,
            )

    # Bulk operations on the list push one patch for many rows and set skip_push on each row.

    def after_insert(self) -> None:
        if self.enabled and not self.flags.skip_push:
            self.get_list().push_recipient_changes(added=[self.email])

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if self.is_new() or self.flags.skip_push or not before:
            return
        if bool(before.enabled) != bool(self.enabled):
            if self.enabled:
                self.get_list().push_recipient_changes(added=[self.email])
            else:
                self.get_list().push_recipient_changes(removed=[self.email])

    def on_trash(self) -> None:
        if self.enabled and not self.flags.skip_push:
            self.get_list().push_recipient_changes(removed=[self.email])

    def get_list(self) -> Document:
        return frappe.get_doc("Mailing List", self.mailing_list)


def on_doctype_update() -> None:
    frappe.db.add_unique("Mailing List Recipient", ["mailing_list", "email"])
    frappe.db.add_index("Mailing List Recipient", ["email"])
