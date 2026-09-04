# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now

from suite_cloud.cluster import bootstrap, dns, egress, plan, reconcile
from suite_cloud.provisioning.ssh import generate_keypair
from suite_cloud.stalwart import forget_sessions, get_admin_client, get_client
from suite_cloud.stalwart.credentials import Credential
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import get_default_zone
from suite_cloud.utils import get_config

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
STORE_KINDS = {
    "data_store": "Data",
    "blob_store": "Blob",
    "search_store": "Search",
    "in_memory_store": "In-Memory",
}


class StalwartCluster(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        acme_contact_email: DF.Data | None
        acme_directory_url: DF.Data | None
        admin_password: DF.Password | None
        admin_username: DF.Data | None
        api_key: DF.Password | None
        base_url: DF.Data | None
        blob_store: DF.Link | None
        bootstrap_node: DF.Link | None
        cluster_name: DF.Data
        config_plan: DF.Code | None
        config_version: DF.Int
        coordinator: DF.Literal["Disabled", "Default"]
        data_store: DF.Link
        default_egress_pool: DF.Link | None
        default_domain: DF.Data | None
        dns_zone: DF.Link
        drift_report: DF.JSON | None
        enabled: DF.Check
        enterprise_api_key: DF.Password | None
        enterprise_license_key: DF.Password | None
        hostname: DF.Data
        in_memory_store: DF.Link | None
        is_default: DF.Check
        last_config_sync_at: DF.Datetime | None
        region: DF.Data | None
        relay_password: DF.Password | None
        relay_username: DF.Data | None
        search_store: DF.Link | None
        ssh_port: DF.Int
        ssh_private_key: DF.Password | None
        ssh_public_key: DF.Code | None
        ssh_user: DF.Data
        stalwart_version: DF.Data | None
        status: DF.Literal["Pending", "Bootstrapping", "Active", "Failed", "Disabled"]
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def before_insert(self) -> None:
        self.status = "Pending"
        self.admin_username = self.admin_username or "admin"
        if not self.admin_password:
            self.admin_password = frappe.generate_hash(length=32)
        self.relay_username = self.relay_username or "relay"
        if not self.relay_password:
            self.relay_password = frappe.generate_hash(length=32)
        if not self.ssh_public_key:
            self.ssh_private_key, self.ssh_public_key = generate_keypair(f"suite-cloud-{self.cluster_name}")

    def validate(self) -> None:
        self.validate_names()
        self.apply_defaults()
        self.validate_stores()
        self.validate_default()
        self.validate_egress_pool()

    def validate_egress_pool(self) -> None:
        if (
            self.default_egress_pool
            and frappe.db.get_value("Egress IP Pool", self.default_egress_pool, "cluster") != self.name
        ):
            frappe.throw(_("Egress pool {0} belongs to another cluster.").format(self.default_egress_pool))

    def after_insert(self) -> None:
        dns.sync_spf_record(self)

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if not before:
            return
        if before.enabled and not self.enabled and self.status == "Active":
            self.db_set("status", "Disabled")
        elif not before.enabled and self.enabled and self.status == "Disabled":
            self.db_set("status", "Active")
        if before.default_egress_pool != self.default_egress_pool:
            egress.resync_cluster(self)

    def on_trash(self) -> None:
        for doctype in ("Stalwart Node", "Suite Site", "Egress Gateway", "Egress IP Pool"):
            if frappe.db.exists("DocType", doctype) and frappe.db.exists(doctype, {"cluster": self.name}):
                frappe.throw(_("Remove every {0} of this cluster first.").format(_(doctype)))
        dns.delete_cluster_records(self)

    # --- validation -----------------------------------------------------------

    def validate_names(self) -> None:
        self.cluster_name = (self.cluster_name or "").strip().lower()
        if not SLUG.match(self.cluster_name):
            frappe.throw(_("Cluster Name must be a slug: lowercase letters, digits and dashes."))

        self.hostname = (self.hostname or "").strip().lower().rstrip(".")
        self.dns_zone = self.dns_zone or get_default_zone()
        if not self.dns_zone:
            frappe.throw(_("Create a DNS Zone before creating clusters."))
        zone = self.dns_zone
        if not self.hostname.endswith(f".{zone}") or self.hostname.count(".") < zone.count(".") + 2:
            frappe.throw(
                _("Hostname must be at least two labels under the DNS zone, e.g. mail.blr.{0}").format(zone)
            )

        self.default_domain = self.hostname.split(".", 1)[1]
        self.base_url = f"https://{self.hostname}"

    def apply_defaults(self) -> None:
        self.stalwart_version = self.stalwart_version or get_config("stalwart_version")
        self.acme_directory_url = self.acme_directory_url or get_config("acme_directory_url")
        self.acme_contact_email = self.acme_contact_email or get_config("acme_contact_email")
        self.region = (self.region or self.default_domain.split(".")[0]).strip().lower()

    def validate_stores(self) -> None:
        for field, kind in STORE_KINDS.items():
            if store_name := self.get(field):
                store = frappe.get_cached_doc("Stalwart Store", store_name)
                if store.kind != kind:
                    frappe.throw(_("{0} must be a {1} store.").format(self.meta.get_label(field), kind))

        in_memory = (
            frappe.get_cached_doc("Stalwart Store", self.in_memory_store) if self.in_memory_store else None
        )
        self.coordinator = "Default" if in_memory and in_memory.type.startswith("Redis") else "Disabled"

        if self.node_count() > 1:
            self.validate_multi_node_stores()

    def validate_multi_node_stores(self) -> None:
        """Every node reads the same stores, so embedded backends cannot be shared."""

        for field in STORE_KINDS:
            if store_name := self.get(field):
                if frappe.get_cached_value("Stalwart Store", store_name, "type") in ("RocksDb", "FileSystem"):
                    frappe.throw(_("{0} is embedded and cannot back a multi-node cluster.").format(field))

        if self.coordinator == "Disabled":
            frappe.throw(_("A multi-node cluster needs a Redis in-memory store to coordinate nodes."))

    def validate_default(self) -> None:
        if self.is_default:
            frappe.db.set_value(
                "Stalwart Cluster", {"is_default": 1, "name": ["!=", self.name]}, "is_default", 0
            )

    # --- helpers --------------------------------------------------------------

    def node_count(self, enabled_only: bool = True) -> int:
        if self.is_new():
            return 0
        filters = {"cluster": self.name}
        if enabled_only:
            filters["enabled"] = 1
        return frappe.db.count("Stalwart Node", filters)

    def get_nodes(self, statuses: tuple[str, ...] | None = None) -> list[Document]:
        filters = {"cluster": self.name}
        if statuses:
            filters["status"] = ["in", list(statuses)]
        return [
            frappe.get_doc("Stalwart Node", n) for n in frappe.get_all("Stalwart Node", filters, pluck="name")
        ]

    def get_store(self, field: str) -> Document | None:
        return frappe.get_cached_doc("Stalwart Store", self.get(field)) if self.get(field) else None

    def get_client(self):
        return get_client(self)

    def get_admin_client(self):
        return get_admin_client(self)

    def bump_config_version(self, rendered_plan: list[dict]) -> None:
        self.db_set(
            {
                "config_version": (self.config_version or 0) + 1,
                "config_plan": plan.redacted(rendered_plan),
            },
            update_modified=False,
        )

    # --- actions --------------------------------------------------------------

    @frappe.whitelist()
    def preview_plan(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return plan.redacted(plan.cluster_plan(self))

    @frappe.whitelist()
    def sync_config(self) -> dict:
        """Pushes the generated configuration to the running cluster and reloads it."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        if self.status != "Active":
            frappe.throw(_("Only an active cluster can be synced; provision the first node instead."))
        return self.push_config()

    def push_config(self) -> dict:
        """The sync itself; also run on behalf of documents whose change alters the plan."""

        rendered = plan.cluster_plan(self)
        result = self.get_client().apply(rendered)
        self.get_client().reload_settings()
        self.bump_config_version(rendered)
        self.db_set({"last_config_sync_at": now(), "drift_report": None}, update_modified=False)
        return {"created": result.created, "updated": result.updated, "unchanged": result.unchanged}

    @frappe.whitelist()
    def check_drift(self) -> dict:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        report = plan.drift_report(self)
        self.db_set("drift_report", frappe.as_json(report), update_modified=False)
        return report

    @frappe.whitelist()
    def reconcile_directory(self) -> dict:
        """Reports domains/accounts/lists that differ between Suite Cloud and the cluster; never mutates."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return reconcile.directory_report(self)

    @frappe.whitelist()
    def finish_bootstrap(self) -> bool:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return bootstrap.finish_bootstrap(self)

    @frappe.whitelist()
    def rotate_api_key(self) -> None:
        """Mints a fresh management key with the admin credentials and forgets the old one."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        client = self.get_admin_client()
        old = client.api_keys.find_local(description=plan.API_KEY_DESCRIPTION)
        _, secret = client.api_keys.create_secret(
            Credential(description=plan.API_KEY_DESCRIPTION, permissions=plan.api_key_permissions())
        )
        self.api_key = secret
        self.save(ignore_permissions=True)
        forget_sessions(self)
        if old:
            client.api_keys.delete(old["id"])

    @frappe.whitelist()
    def show_admin_password(self) -> str:
        frappe.only_for("Administrator")
        return self.get_password("admin_password")

    @frappe.whitelist()
    def upgrade_nodes(self) -> list[str]:
        """Upgrades every active node one after the other to the cluster's Stalwart version."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return [bootstrap.upgrade_node(node).name for node in self.get_nodes(("Active",))]


def check_all_clusters() -> None:
    """Daily: drift reports for active clusters."""

    for name in frappe.get_all("Stalwart Cluster", {"status": "Active", "enabled": 1}, pluck="name"):
        cluster = frappe.get_doc("Stalwart Cluster", name)
        try:
            cluster.check_drift()
        except Exception:
            cluster.log_error(f"Drift check failed for {name}")
