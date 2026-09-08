# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from suite_cloud.stalwart.directory import MailingList as StalwartMailingList
from suite_cloud.tenancy import sync
from suite_cloud.tenancy.addresses import assert_address_available, get_site_domain, validate_email_address

# Keys per JMAP patch when recipients change in bulk; keeps requests well under server limits.
PATCH_BATCH = 1000


class MailingList(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.suite_cloud.doctype.mail_address_alias.mail_address_alias import MailAddressAlias

        aliases: DF.Table[MailAddressAlias]
        cluster: DF.Link | None
        description: DF.Data | None
        domain: DF.Link | None
        email: DF.Data
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
        if self.is_new():
            frappe.get_cached_doc("Suite Site", self.site).assert_can_add_mailing_list()
        assert_address_available(self.email, exclude=(self.doctype, self.name))
        sync.validate_aliases(self)

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
        if sync.aliases_changed(before, self):
            patch["aliases"] = sync.aliases_payload(self)
        if patch:
            sync.push_update(self, "mailing_lists", patch)

    def on_trash(self) -> None:
        # The recipients go with the list; the cluster object carries them, so no patch per row.
        for name in frappe.get_all("Mailing List Recipient", {"mailing_list": self.name}, pluck="name"):
            row = frappe.get_doc("Mailing List Recipient", name)
            row.flags.skip_push = True
            row.delete(ignore_permissions=True)
        sync.push_destroy(self, "mailing_lists")

    def stalwart_payload(self) -> StalwartMailingList:
        return StalwartMailingList(
            name=self.email.split("@", 1)[0],
            domain_id=sync.domain_stalwart_id(self.domain),
            description=self.description or None,
            aliases=sync.aliases(self),
            recipients=self.recipient_emails() if not self.is_new() else [],
        )

    # --- recipients ---------------------------------------------------------------------------------

    def recipient_count(self, enabled_only: bool = False) -> int:
        filters = {"mailing_list": self.name}
        if enabled_only:
            filters["enabled"] = 1
        return frappe.db.count("Mailing List Recipient", filters)

    def recipient_emails(self, enabled_only: bool = True) -> list[str]:
        filters = {"mailing_list": self.name}
        if enabled_only:
            filters["enabled"] = 1
        return frappe.get_all("Mailing List Recipient", filters, pluck="email", order_by="email asc")

    def add_recipients(self, emails: list[str]) -> list[str]:
        """Adds the addresses not yet on the list and pushes them in one patch. Returns the added ones."""

        wanted = [validate_email_address(e) for e in emails]
        existing = set(self.recipient_emails(enabled_only=False))
        added = []
        for email in dict.fromkeys(wanted):
            if email in existing:
                continue
            row = frappe.get_doc(
                {"doctype": "Mailing List Recipient", "mailing_list": self.name, "email": email}
            )
            row.flags.skip_push = True
            row.insert(ignore_permissions=True)
            added.append(email)
        self.push_recipient_changes(added=added)
        return added

    def remove_recipients(self, emails: list[str]) -> list[str]:
        """Removes the addresses that are on the list and pushes them in one patch. Returns the removed ones."""

        wanted = {validate_email_address(e) for e in emails}
        removed = []
        for row in frappe.get_all(
            "Mailing List Recipient",
            {"mailing_list": self.name, "email": ["in", list(wanted)]},
            ["name", "email", "enabled"],
        ):
            doc = frappe.get_doc("Mailing List Recipient", row.name)
            doc.flags.skip_push = True
            doc.delete(ignore_permissions=True)
            if row.enabled:
                removed.append(row.email)
        self.push_recipient_changes(removed=removed)
        return removed

    def set_recipients(self, emails: list[str]) -> None:
        """Makes the list exactly ``emails``: a full replace, meant for small lists."""

        wanted = {validate_email_address(e) for e in emails}
        current = set(self.recipient_emails(enabled_only=False))
        self.remove_recipients(sorted(current - wanted))
        self.add_recipients(sorted(wanted - current))

    def push_recipient_changes(
        self, added: list[str] | None = None, removed: list[str] | None = None
    ) -> None:
        """Patches the cluster's recipient set key by key, in batches; never re-sends the whole set."""

        if not self.stalwart_id:
            return
        changes = {**{e: True for e in added or []}, **{e: None for e in removed or []}}
        keys = list(changes)
        for start in range(0, len(keys), PATCH_BATCH):
            batch = keys[start : start + PATCH_BATCH]
            sync.push_update(self, "mailing_lists", {f"recipients/{e}": changes[e] for e in batch})

    def to_api(self) -> dict:
        return {
            "email": self.email,
            "domain": self.domain,
            "description": self.description,
            "recipient_count": self.recipient_count(),
            "aliases": [
                {"email": a.alias_email, "enabled": bool(a.enabled), "description": a.description}
                for a in self.aliases
            ],
            "created_at": self.creation,
        }
