# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.cluster import dns, egress, plan
from suite_cloud.provisioning.ansible import ping
from suite_cloud.provisioning.ssh import SSHTarget
from suite_cloud.stalwart import get_admin_client, get_client
from suite_cloud.suite_cloud.doctype.stalwart_node.stalwart_node import validate_ip
from suite_cloud.utils import get_config


class EgressGateway(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        admin_password: DF.Password | None
        admin_username: DF.Data | None
        api_key: DF.Password | None
        base_url: DF.Data | None
        cluster: DF.Link
        config_plan: DF.Code | None
        config_version: DF.Int
        data_store: DF.Link | None
        enabled: DF.Check
        hostname: DF.Data
        installed_version: DF.Data | None
        ipv4_address: DF.Data
        last_config_sync_at: DF.Datetime | None
        last_error: DF.SmallText | None
        provisioned_at: DF.Datetime | None
        ssh_port: DF.Int
        ssh_user: DF.Data | None
        ssh_verified: DF.Check
        stalwart_version: DF.Data | None
        status: DF.Literal["Pending", "Provisioning", "Provisioned", "Active", "Failed", "Disabled"]
    # end: auto-generated types

    # --- lifecycle --------------------------------------------------------------

    def before_insert(self) -> None:
        self.status = "Pending"
        self.admin_username = self.admin_username or "admin"
        if not self.admin_password:
            self.admin_password = frappe.generate_hash(length=32)
        if not self.data_store:
            self.data_store = self.create_local_store().name

    def validate(self) -> None:
        cluster = self.get_cluster()
        self.hostname = (self.hostname or "").strip().lower().rstrip(".")
        suffix = f".{cluster.default_domain}"
        if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
            frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

        self.base_url = f"https://{self.hostname}"
        self.ipv4_address = validate_ip(self.ipv4_address, 4)
        self.ssh_user = self.ssh_user or cluster.ssh_user
        self.ssh_port = self.ssh_port or cluster.ssh_port
        self.stalwart_version = (
            self.stalwart_version or cluster.stalwart_version or get_config("stalwart_version")
        )

    def after_insert(self) -> None:
        dns.sync_gateway_records(self)

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if before and before.ipv4_address != self.ipv4_address:
            dns.sync_gateway_records(self)
            for pool in self.pools():
                dns.sync_pool_records(pool)

    def on_trash(self) -> None:
        if frappe.db.exists("Egress IP Pool Address", {"gateway": self.name}):
            frappe.throw(_("Remove this gateway's addresses from every pool first."))
        dns.delete_gateway_records(self)

    # --- helpers ------------------------------------------------------------------

    def create_local_store(self) -> Document:
        store = frappe.get_doc(
            {
                "doctype": "Stalwart Store",
                "title": f"{self.hostname} local data",
                "kind": "Data",
                "type": "RocksDb",
                "path": "/var/lib/stalwart",
            }
        )
        store.insert(ignore_permissions=True)
        return store

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def get_store(self, field: str = "data_store") -> Document | None:
        return frappe.get_cached_doc("Stalwart Store", self.get(field)) if self.get(field) else None

    def pools(self) -> list[Document]:
        names = frappe.get_all(
            "Egress IP Pool Address", {"gateway": self.name}, pluck="parent", distinct=True
        )
        return [frappe.get_doc("Egress IP Pool", name) for name in sorted(set(names))]

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

    def get_client(self):
        return get_client(self)

    def get_admin_client(self):
        return get_admin_client(self)

    def bump_config_version(self, rendered_plan: list[dict]) -> None:
        self.db_set(
            {"config_version": (self.config_version or 0) + 1, "config_plan": plan.redacted(rendered_plan)},
            update_modified=False,
        )

    # --- actions --------------------------------------------------------------------

    @frappe.whitelist()
    def verify_ssh(self) -> bool:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        ok, detail = ping(self.ssh_target())
        self.db_set({"ssh_verified": cint(ok), "last_error": None if ok else detail}, update_modified=False)
        return ok

    @frappe.whitelist()
    def provision(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        if not self.ssh_verified:
            frappe.throw(_("Verify the SSH connection first."))
        return egress.provision_gateway(self).name

    @frappe.whitelist()
    def sync_config(self) -> dict:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        if self.status != "Active":
            frappe.throw(_("Only an active gateway can be synced."))
        rendered = egress.gateway_plan(self)
        result = self.get_client().apply(rendered)
        self.get_client().reload_settings()
        self.bump_config_version(rendered)
        self.db_set("last_config_sync_at", now(), update_modified=False)
        return {"created": result.created, "updated": result.updated, "unchanged": result.unchanged}

    @frappe.whitelist()
    def preview_plan(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return plan.redacted(egress.gateway_plan(self))

    @frappe.whitelist()
    def check_health(self) -> bool:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return egress.check_gateway(self)

    @frappe.whitelist()
    def upgrade(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return egress.upgrade_gateway(self).name

    # --- Server Job callbacks ---------------------------------------------------------

    def after_provision(self, job: Document) -> None:
        egress.after_gateway_provision(self, job)

    def after_provision_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)

    def after_upgrade(self, job: Document) -> None:
        self.db_set("installed_version", self.stalwart_version, update_modified=False)
        self.set_status("Provisioned")
        egress.check_gateway(self)

    def after_upgrade_failed(self, job: Document) -> None:
        self.set_status("Failed", job.error_log)


def poll_pending_gateways() -> None:
    for name in frappe.get_all("Egress Gateway", {"status": "Provisioned"}, pluck="name"):
        gateway = frappe.get_doc("Egress Gateway", name)
        try:
            egress.check_gateway(gateway)
        except Exception:
            gateway.log_error(f"Health check failed for {name}")
        frappe.db.commit()
