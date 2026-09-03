# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.cluster import bootstrap, dns
from suite_cloud.dns.resolver import verify_ptr_record
from suite_cloud.provisioning.ansible import ping
from suite_cloud.provisioning.ssh import SSHTarget

REMOVABLE_STATUSES = ("Pending", "Failed", "Disabled")


class StalwartNode(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        cluster: DF.Link
        enabled: DF.Check
        hostname: DF.Data
        in_ingress_dns: DF.Check
        installed_version: DF.Data | None
        ipv4_address: DF.Data
        ipv6_address: DF.Data | None
        is_bootstrap_node: DF.Check
        last_error: DF.SmallText | None
        last_health_at: DF.Datetime | None
        node_id: DF.Int
        provisioned_at: DF.Datetime | None
        ptr_verified: DF.Check
        role: DF.Literal["full", "frontend", "outbound"]
        ssh_port: DF.Int
        ssh_user: DF.Data | None
        ssh_verified: DF.Check
        status: DF.Literal[
            "Pending", "Provisioning", "Provisioned", "Active", "Draining", "Failed", "Disabled"
        ]
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def validate(self) -> None:
        cluster = self.get_cluster()
        self.hostname = (self.hostname or "").strip().lower().rstrip(".")
        suffix = f".{cluster.default_domain}"
        if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
            frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

        self.ipv4_address = validate_ip(self.ipv4_address, 4)
        self.ipv6_address = validate_ip(self.ipv6_address, 6) if self.ipv6_address else None
        self.ssh_user = self.ssh_user or cluster.ssh_user
        self.ssh_port = self.ssh_port or cluster.ssh_port
        if self.is_new():
            self.status = "Pending"

        if self.has_value_changed("ipv4_address") or self.has_value_changed("ipv6_address"):
            self.ssh_verified = 0

    def after_insert(self) -> None:
        dns.sync_node_records(self)
        self.get_cluster().save(ignore_permissions=True)  # re-validates stores for the new node count

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if not before:
            return
        if (before.ipv4_address, before.ipv6_address) != (self.ipv4_address, self.ipv6_address):
            dns.sync_node_records(self)
            dns.sync_spf_record(self.get_cluster())

        if before.enabled and not self.enabled and self.status in ("Active", "Provisioned"):
            bootstrap.drain_node(self)

    def on_trash(self) -> None:
        if self.status not in REMOVABLE_STATUSES:
            frappe.throw(_("Drain and disable the node before deleting it."))
        cluster = self.get_cluster()
        if self.is_bootstrap_node and cluster.status == "Active":
            frappe.throw(_("The bootstrap node of an active cluster cannot be deleted."))
        if cluster.bootstrap_node == self.name:
            # A failed first attempt: let another node bootstrap the cluster.
            cluster.db_set({"bootstrap_node": None, "status": "Pending"}, update_modified=False)

        bootstrap.forget_node(self)
        dns.delete_node_records(self)

    def after_delete(self) -> None:
        dns.sync_spf_record(self.get_cluster())

    # --- helpers --------------------------------------------------------------

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def ssh_target(self) -> SSHTarget:
        cluster = self.get_cluster()
        return SSHTarget(
            host=self.ipv4_address,
            user=self.ssh_user or cluster.ssh_user,
            port=cint(self.ssh_port or cluster.ssh_port),
            private_key=cluster.get_password("ssh_private_key"),
        )

    def set_status(self, status: str, error: str | None = None) -> None:
        values = {"status": status}
        if error is not None:
            values["last_error"] = error[:1000]
        self.db_set(values, update_modified=False, notify=True)

    # --- actions --------------------------------------------------------------

    @frappe.whitelist()
    def verify_ssh(self) -> bool:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        ok, detail = ping(self.ssh_target())
        self.db_set({"ssh_verified": cint(ok), "last_error": None if ok else detail}, update_modified=False)
        if ok:
            frappe.msgprint(_("SSH connection verified."), indicator="green", alert=True)
        else:
            frappe.msgprint(_("SSH connection failed: {0}").format(detail), indicator="red")
        return ok

    @frappe.whitelist()
    def provision(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        if not self.ssh_verified:
            frappe.throw(_("Verify the SSH connection first."))
        if not self.enabled:
            frappe.throw(_("Enable the node first."))
        return bootstrap.provision_node(self).name

    @frappe.whitelist()
    def upgrade(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return bootstrap.upgrade_node(self).name

    @frappe.whitelist()
    def drain(self) -> None:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        bootstrap.drain_node(self)

    @frappe.whitelist()
    def restore(self) -> None:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        bootstrap.restore_node(self)

    @frappe.whitelist()
    def check_health(self) -> bool:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return bootstrap.check_node(self)

    @frappe.whitelist()
    def verify_ptr(self) -> bool:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        ok = verify_ptr_record(self.ipv4_address, self.hostname)
        self.db_set("ptr_verified", cint(ok), update_modified=False)
        return ok

    # --- Server Job callbacks -----------------------------------------------------

    def after_provision(self, job: Document) -> None:
        bootstrap.after_provision(self, job)

    def after_provision_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)
        cluster = self.get_cluster()
        exhausted = (job.retries or 0) > (job.max_retries or 0)
        if self.is_bootstrap_node and cluster.status == "Bootstrapping" and exhausted:
            cluster.db_set("status", "Failed", update_modified=False)

    def after_upgrade(self, job: Document) -> None:
        bootstrap.after_upgrade(self, job)

    def after_upgrade_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)


def validate_ip(value: str | None, version: int) -> str:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        frappe.throw(_("{0} is not a valid IP address.").format(value))
    if address.version != version:
        frappe.throw(_("{0} is not an IPv{1} address.").format(value, version))
    return str(address)


def poll_pending_nodes() -> None:
    """Cron: moves provisioned nodes to Active once the cluster registry (or TLS) confirms them."""

    for name in frappe.get_all("Stalwart Node", {"status": "Provisioned"}, pluck="name"):
        node = frappe.get_doc("Stalwart Node", name)
        try:
            bootstrap.check_node(node)
        except Exception:
            frappe.db.rollback()
            node.log_error(f"Health check failed for {name}")
            continue
        if not frappe.in_test:
            frappe.db.commit()


def verify_all_ptr_records() -> None:
    for name in frappe.get_all("Stalwart Node", {"enabled": 1}, pluck="name"):
        node = frappe.get_doc("Stalwart Node", name)
        node.db_set(
            "ptr_verified", cint(verify_ptr_record(node.ipv4_address, node.hostname)), update_modified=False
        )
