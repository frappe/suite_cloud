# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from suite_cloud.stalwart.directory import MailingList as StalwartMailingList
from suite_cloud.tenancy import sync
from suite_cloud.tenancy.addresses import assert_address_available, get_site_domain, validate_email_address


class MailingList(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.suite_cloud.doctype.mail_address_alias.mail_address_alias import MailAddressAlias
        from suite_cloud.suite_cloud.doctype.mailing_list_recipient.mailing_list_recipient import (
            MailingListRecipient,
        )

        aliases: DF.Table[MailAddressAlias]
        cluster: DF.Link | None
        description: DF.Data | None
        domain: DF.Link | None
        email: DF.Data
        recipients: DF.Table[MailingListRecipient]
        site: DF.Link | None
        stalwart_id: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        # Naming runs before validate, so the address is normalised here too.
        self.email = validate_email_address(self.email)
        self.name = self.email

    def validate(self) -> None:
        self.email = validate_email_address(self.email)
        domain = get_site_domain(self.site, self.email.split("@", 1)[1]) if self.site else None
        if domain is None:
            domain = frappe.get_cached_doc("Mail Domain", self.email.split("@", 1)[1])
        self.domain = domain.name
        self.site = domain.site
        self.cluster = domain.cluster
        assert_address_available(self.email, exclude=(self.doctype, self.name))
        sync.validate_aliases(self)
        self.validate_recipients()

    def validate_recipients(self) -> None:
        seen = set()
        for row in self.recipients:
            row.email = validate_email_address(row.email)
            if row.email == self.email:
                frappe.throw(_("A mailing list cannot be its own recipient."))
            if row.email in seen:
                frappe.throw(_("Recipient {0} is listed twice.").format(row.email))
            seen.add(row.email)

    def after_insert(self) -> None:
        sync.push_create(self, "mailing_lists", self.stalwart_payload())

    def on_update(self) -> None:
        if self.is_new() or not self.stalwart_id or self.flags.skip_push:
            return
        before = self.get_doc_before_save()
        if not before:
            return
        patch = {}
        if before.description != self.description:
            patch["description"] = self.description
        if sorted(r.email for r in before.recipients) != sorted(r.email for r in self.recipients):
            patch["recipients"] = {r.email: True for r in self.recipients}
        if sync.aliases_changed(before, self):
            patch["aliases"] = sync.aliases_payload(self)
        if patch:
            sync.push_update(self, "mailing_lists", patch)

    def on_trash(self) -> None:
        sync.push_destroy(self, "mailing_lists")

    def stalwart_payload(self) -> StalwartMailingList:
        return StalwartMailingList(
            name=self.email.split("@", 1)[0],
            domain_id=sync.domain_stalwart_id(self.domain),
            description=self.description or None,
            aliases=sync.aliases(self),
            recipients=[r.email for r in self.recipients],
        )

    def to_api(self) -> dict:
        return {
            "email": self.email,
            "domain": self.domain,
            "description": self.description,
            "recipients": [r.email for r in self.recipients],
            "aliases": [
                {"email": a.alias_email, "enabled": bool(a.enabled), "description": a.description}
                for a in self.aliases
            ],
            "created_at": self.creation,
        }
