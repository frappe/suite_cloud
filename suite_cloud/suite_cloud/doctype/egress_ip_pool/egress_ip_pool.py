# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.cluster import dns, egress
from suite_cloud.dns.resolver import verify_ptr_record
from suite_cloud.suite_cloud.doctype.stalwart_node.stalwart_node import validate_ip

POOL_NAME = re.compile(r"^[a-z0-9]{1,8}$")
FIRST_RELAY_PORT = 2525


class EgressIPPool(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.suite_cloud.doctype.egress_ip_pool_address.egress_ip_pool_address import (
            EgressIPPoolAddress,
        )

        addresses: DF.Table[EgressIPPoolAddress]
        cluster: DF.Link
        description: DF.Data | None
        hostname: DF.Data | None
        pool_name: DF.Data
        relay_port: DF.Int
    # end: auto-generated types

    def autoname(self) -> None:
        self.pool_name = (self.pool_name or "").strip().lower()
        self.name = f"{self.cluster}-{self.pool_name}"

    def validate(self) -> None:
        if not POOL_NAME.match(self.pool_name or ""):
            frappe.throw(_("Pool Name must be 1-8 lowercase letters or digits."))

        cluster = self.get_cluster()
        self.hostname = f"{self.pool_name}.out.{cluster.default_domain}"
        if not self.relay_port:
            self.relay_port = self.next_relay_port()
        self.validate_addresses(cluster)

    def validate_addresses(self, cluster: Document) -> None:
        seen: set[str] = set()
        for row in self.addresses:
            row.ip_address = validate_ip(row.ip_address, 6 if ":" in (row.ip_address or "") else 4)
            row.ehlo_hostname = (row.ehlo_hostname or "").strip().lower().rstrip(".")
            if row.ip_address in seen:
                frappe.throw(_("Address {0} is listed twice.").format(row.ip_address))
            seen.add(row.ip_address)
            if frappe.db.get_value("Egress Gateway", row.gateway, "cluster") != self.cluster:
                frappe.throw(_("Gateway {0} belongs to another cluster.").format(row.gateway))
            suffix = f".{cluster.default_domain}"
            if not row.ehlo_hostname.endswith(suffix) or "." in row.ehlo_hostname[: -len(suffix)]:
                frappe.throw(
                    _("EHLO hostname {0} must be a single label under {1}.").format(
                        row.ehlo_hostname, cluster.default_domain
                    )
                )
            other = frappe.db.get_value(
                "Egress IP Pool Address",
                {"ip_address": row.ip_address, "parent": ["!=", self.name]},
                "parent",
            )
            if other:
                frappe.throw(_("Address {0} already belongs to pool {1}.").format(row.ip_address, other))

    def next_relay_port(self) -> int:
        ports = frappe.get_all("Egress IP Pool", {"cluster": self.cluster}, pluck="relay_port")
        return max([FIRST_RELAY_PORT - 1, *[p for p in ports if p]]) + 1

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if before and self.snapshot(before) == self.snapshot(self):
            return
        dns.sync_pool_records(self)
        dns.sync_spf_record(self.get_cluster())
        egress.apply_pool_changes(self)

    @staticmethod
    def snapshot(doc: Document) -> tuple:
        addresses = sorted((r.gateway, r.ip_address, r.ehlo_hostname) for r in doc.addresses)
        return (doc.relay_port, doc.hostname, tuple(addresses))

    def on_trash(self) -> None:
        for doctype, field in (
            ("Stalwart Cluster", "default_egress_pool"),
            ("Suite Site", "egress_pool"),
            ("Mail Domain", "egress_pool"),
        ):
            if user := frappe.db.exists(doctype, {field: self.name}):
                frappe.throw(_("Pool is still used by {0} {1}.").format(_(doctype), user))
        dns.delete_pool_records(self)

    def after_delete(self) -> None:
        cluster = self.get_cluster()
        dns.sync_spf_record(cluster)
        egress.resync_cluster(cluster)

    # --- actions ----------------------------------------------------------------

    @frappe.whitelist()
    def verify_ptr(self, address: str | None = None) -> dict[str, bool]:
        """Check the reverse DNS of one address row (by row name) or of every address."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        rows = [row for row in self.addresses if not address or row.name == address]
        if address and not rows:
            frappe.throw(_("Address row {0} not found.").format(address))
        return {row.ip_address: verify_address_ptr(row) for row in rows}

    # --- helpers --------------------------------------------------------------

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def gateway_names(self) -> list[str]:
        return sorted({row.gateway for row in self.addresses})

    def addresses_on(self, gateway: str) -> list[Document]:
        return [row for row in self.addresses if row.gateway == gateway]


def verify_address_ptr(row: Document) -> bool:
    ok = verify_ptr_record(row.ip_address, row.ehlo_hostname)
    row.db_set("ptr_verified", cint(ok), update_modified=False)
    return ok


def verify_all_ptr_records() -> None:
    for name in frappe.get_all("Egress IP Pool", pluck="name"):
        for row in frappe.get_doc("Egress IP Pool", name).addresses:
            verify_address_ptr(row)
