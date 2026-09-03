# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from functools import cached_property

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.dns import get_dns_provider
from suite_cloud.dns.resolver import verify_dns_record
from suite_cloud.utils import enqueue_job, get_config, user_context


class DNSRecord(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        category: DF.Literal["Node", "Ingress", "Egress", "SPF", "Other"]
        host: DF.Data
        is_verified: DF.Check
        last_checked_at: DF.Datetime | None
        managed_by: DF.DynamicLink | None
        managed_by_doctype: DF.Literal[
            "", "Stalwart Cluster", "Stalwart Node", "Egress Gateway", "Egress IP Pool"
        ]
        priority: DF.Int
        ttl: DF.Int
        type: DF.Literal["", "A", "AAAA", "CNAME", "MX", "TXT"]
        value: DF.Text
    # end: auto-generated types

    @cached_property
    def fqdn(self) -> str:
        root_domain_name = get_config("root_domain_name")
        if not root_domain_name:
            frappe.throw(_("Please set the Root Domain Name in Suite Cloud Settings."))

        return f"{self.host}.{root_domain_name}"

    def validate(self) -> None:
        self.host = (self.host or "").strip().lower()
        self.value = (self.value or "").strip()
        if self.is_new():
            self.validate_duplicate_record()

        self.ttl = self.ttl or cint(get_config("default_dns_ttl"))

    def on_update(self) -> None:
        if self.has_value_changed("value") or self.has_value_changed("ttl") or self.is_new():
            self.push_to_provider()

    def on_trash(self) -> None:
        self.delete_from_provider()

    def validate_duplicate_record(self) -> None:
        """Several records may share host and type (round-robin), but not the value too.

        Different owners may want the same record (a pool address that reuses its gateway's
        hostname); each keeps its own row and the provider record lives as long as one remains.
        """

        if frappe.db.exists(
            "DNS Record",
            {
                "host": self.host,
                "type": self.type,
                "value": self.value,
                "name": ["!=", self.name],
                "managed_by_doctype": self.managed_by_doctype or "",
                "managed_by": self.managed_by or "",
            },
        ):
            frappe.throw(
                _("DNS Record {0} {1} {2} already exists.").format(self.host, self.type, self.value),
                title=_("Duplicate Record"),
            )

    # --- provider sync ------------------------------------------------------

    def push_to_provider(self) -> None:
        if frappe.flags.do_not_enqueue or frappe.flags.in_test:
            self.create_or_update_record_in_dns_provider()
        else:
            frappe.enqueue_doc(
                self.doctype,
                self.name,
                "create_or_update_record_in_dns_provider",
                queue="short",
                enqueue_after_commit=True,
                at_front=True,
            )

    @frappe.whitelist()
    def sync_dns_record(self) -> None:
        self.create_or_update_record_in_dns_provider()

    def create_or_update_record_in_dns_provider(self) -> None:
        result = False
        if provider := get_dns_provider():
            result = provider.ensure_dns_record(
                type=self.type, host=self.host, value=self.value, ttl=self.ttl, priority=self.priority
            )

        self.db_set({"is_verified": cint(result), "last_checked_at": now()}, notify=True)

    def delete_from_provider(self) -> None:
        still_wanted = frappe.db.exists(
            "DNS Record",
            {"host": self.host, "type": self.type, "value": self.value, "name": ["!=", self.name]},
        )
        if still_wanted:
            return
        if provider := get_dns_provider():
            provider.delete_dns_record(type=self.type, host=self.host, value=self.value)

    # --- verification -------------------------------------------------------

    @frappe.whitelist()
    def verify_dns_record(self, save: bool = False) -> bool:
        verified = verify_dns_record(self.fqdn, self.type, self.value)
        if verified is None:
            frappe.msgprint(
                _("Could not resolve {0} right now.").format(frappe.bold(self.fqdn)), indicator="orange"
            )
            return bool(self.is_verified)
        self.is_verified = cint(verified)
        self.last_checked_at = now()

        if self.is_verified:
            frappe.msgprint(
                _("Verified {0}:{1} record.").format(frappe.bold(self.fqdn), frappe.bold(self.type)),
                indicator="green",
                alert=True,
            )
        else:
            frappe.msgprint(
                _("Could not verify {0}:{1} record.").format(frappe.bold(self.fqdn), frappe.bold(self.type)),
                indicator="orange",
                alert=True,
            )

        if save:
            self.db_set({"is_verified": self.is_verified, "last_checked_at": self.last_checked_at})

        return bool(self.is_verified)


def reconcile_managed_records(owner_doctype: str, owner: str, desired: list[dict]) -> None:
    """Makes the owner's DNS Records exactly ``desired``.

    ``desired`` rows are ``{"host", "type", "value", "category", "priority"?, "ttl"?}``. Rows
    already present are left alone (their verification state survives); missing ones are
    inserted and stale ones deleted, which removes them at the provider too.
    """

    wanted = {(row["host"].lower(), row["type"], row["value"].strip()): row for row in desired}
    existing = frappe.get_all(
        "DNS Record",
        filters={"managed_by_doctype": owner_doctype, "managed_by": owner},
        fields=["name", "host", "type", "value"],
    )

    for record in existing:
        key = (record.host, record.type, (record.value or "").strip())
        if key in wanted:
            wanted.pop(key)
        else:
            frappe.delete_doc("DNS Record", record.name, ignore_permissions=True, force=True)

    for row in wanted.values():
        doc = frappe.new_doc("DNS Record")
        doc.update(row)
        doc.managed_by_doctype = owner_doctype
        doc.managed_by = owner
        doc.insert(ignore_permissions=True)


def delete_managed_records(owner_doctype: str, owner: str) -> None:
    reconcile_managed_records(owner_doctype, owner, [])


def verify_all_dns_records() -> None:
    for name in frappe.get_all("DNS Record", pluck="name"):
        frappe.get_doc("DNS Record", name).verify_dns_record(save=True)


@frappe.whitelist()
def enqueue_verify_all_dns_records() -> None:
    frappe.only_for(("System Manager", "Suite Cloud Manager"))

    with user_context("Administrator"):
        enqueue_job(verify_all_dns_records, queue="long", deduplicate=True)
