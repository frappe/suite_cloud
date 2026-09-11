# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Count
from frappe.utils import cint, now

from suite_cloud.cloud_mail.stalwart.directory import MailingList as StalwartMailingList
from suite_cloud.cloud_mail.tenancy import sync
from suite_cloud.cloud_mail.tenancy.addresses import (
    assert_address_available,
    assert_domain_live,
    get_site_domain,
    validate_email_address,
)
from suite_cloud.utils import alias_payloads, child_rows, utc_iso

# Keys per JMAP patch when recipients change in bulk; keeps requests well under server limits.
PATCH_BATCH = 1000
RECIPIENT_COLUMNS = [
    "name",
    "mailing_list",
    "email",
    "enabled",
    "site",
    "creation",
    "modified",
    "owner",
    "modified_by",
]


class MailingList(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.cloud_mail.doctype.mail_address_alias.mail_address_alias import MailAddressAlias

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
            assert_domain_live(domain)
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
        frappe.db.delete("Mailing List Recipient", {"mailing_list": self.name})
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
        """Adds the addresses not yet on the list and pushes them in one patch. Returns the added ones.

        Rows are written in bulk: a batch of thousands must not cost a document insert each. The
        checks the row's controller would run happen here instead.
        """

        wanted = [validate_email_address(e) for e in emails]
        if self.email in wanted:
            frappe.throw(_("A mailing list cannot be its own recipient."))
        existing = set(self.recipient_emails(enabled_only=False))
        added = [email for email in dict.fromkeys(wanted) if email not in existing]
        if added:
            stamp, user = now(), frappe.session.user
            frappe.db.bulk_insert(
                "Mailing List Recipient",
                RECIPIENT_COLUMNS,
                (
                    (
                        frappe.generate_hash(length=10),
                        self.name,
                        email,
                        1,
                        self.site,
                        stamp,
                        stamp,
                        user,
                        user,
                    )
                    for email in added
                ),
            )
        self.push_recipient_changes(added=added)
        return added

    def remove_recipients(self, emails: list[str]) -> list[str]:
        """Removes the addresses that are on the list and pushes them in one patch. Returns the removed ones."""

        wanted = {validate_email_address(e) for e in emails}
        rows = frappe.get_all(
            "Mailing List Recipient",
            {"mailing_list": self.name, "email": ["in", list(wanted)]},
            ["name", "email", "enabled"],
        )
        if rows:
            frappe.db.delete("Mailing List Recipient", {"name": ["in", [row.name for row in rows]]})
        removed = [row.email for row in rows if row.enabled]
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
        return list_payload(
            self, aliases=alias_payloads(self.aliases), recipient_count=self.recipient_count()
        )


def list_payload(row, aliases: list[dict], recipient_count: int) -> dict:
    return {
        "email": row.email,
        "domain": row.domain,
        "description": row.description,
        "recipient_count": recipient_count,
        "aliases": aliases,
        "created_at": utc_iso(row.creation),
    }


LIST_FIELDS = ["name", "email", "domain", "description", "creation"]


def list_payloads(names: list[str]) -> list[dict]:
    """A page of lists in three queries: the lists, their aliases, and one grouped recipient count."""

    if not names:
        return []
    rows = frappe.get_all("Mailing List", filters={"name": ["in", names]}, fields=LIST_FIELDS)
    by_name = {row.name: row for row in rows}
    rows = [by_name[n] for n in names if n in by_name]
    aliases = child_rows(
        "Mail Address Alias", "Mailing List", names, ["alias_email", "enabled", "description"]
    )
    recipient = frappe.qb.DocType("Mailing List Recipient")
    counts = dict(
        frappe.qb.from_(recipient)
        .select(recipient.mailing_list, Count("*"))
        .where(recipient.mailing_list.isin(names))
        .groupby(recipient.mailing_list)
        .run()
    )
    return [
        list_payload(
            row, aliases=alias_payloads(aliases[row.name]), recipient_count=cint(counts.get(row.name))
        )
        for row in rows
    ]
