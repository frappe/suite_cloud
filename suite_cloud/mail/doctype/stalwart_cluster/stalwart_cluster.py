# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now

from suite_cloud.mail.cluster import bootstrap, dns, egress, naming, plan, reconcile
from suite_cloud.mail.stalwart import forget_sessions, get_admin_client, get_client
from suite_cloud.mail.stalwart.credentials import Credential
from suite_cloud.provisioning.ssh import generate_keypair, validate_ssh_user_field
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import get_default_zone
from suite_cloud.utils import dkim_algorithms, get_config, log_exception, validate_version

LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")
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

        from suite_cloud.mail.doctype.stalwart_cluster_region.stalwart_cluster_region import (
            StalwartClusterRegion,
        )

        acme_contact_email: DF.Data | None
        acme_directory_url: DF.Data | None
        agent_key: DF.Data | None
        agent_secret: DF.Password | None
        admin_password: DF.Password | None
        admin_username: DF.Data | None
        api_key: DF.Password | None
        base_url: DF.Data | None
        blob_store: DF.Link | None
        bootstrap_node: DF.Link | None
        config_plan: DF.Code | None
        config_version: DF.Int
        coordinator: DF.Literal["Disabled", "Default"]
        data_store: DF.Link
        default_domain: DF.Data | None
        default_egress_pool: DF.Link | None
        dns_zone: DF.Link
        drift_report: DF.JSON | None
        enable_metrics: DF.Check
        enabled: DF.Check
        hostname: DF.Data
        in_memory_store: DF.Link | None
        is_default: DF.Check
        label: DF.Data | None
        last_config_sync_at: DF.Datetime | None
        metrics_password: DF.Password | None
        quota_backstop_per_day: DF.Int
        quota_hook_token: DF.Password | None
        regions: DF.Table[StalwartClusterRegion]
        relay_password: DF.Password | None
        relay_username: DF.Data | None
        search_store: DF.Link | None
        single_node: DF.Check
        ssh_port: DF.Int
        ssh_private_key: DF.Password | None
        ssh_public_key: DF.Code | None
        ssh_user: DF.Data
        stalwart_version: DF.Data | None
        title: DF.Data
        status: DF.Literal["Pending", "Bootstrapping", "Active", "Failed", "Disabled"]
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def before_insert(self) -> None:
        self.status = "Pending"
        self.admin_username = self.admin_username or "admin"
        if not self.admin_password:
            self.admin_password = frappe.generate_hash(length=32)
        if not self.metrics_password:
            self.metrics_password = frappe.generate_hash(length=32)
        if not self.quota_hook_token:
            self.quota_hook_token = frappe.generate_hash(length=32)
        self.agent_key = self.agent_key or frappe.generate_hash(length=20)
        if not self.agent_secret:
            self.agent_secret = frappe.generate_hash(length=40)
        self.relay_username = self.relay_username or "relay"
        if not self.relay_password:
            self.relay_password = frappe.generate_hash(length=32)
        if not self.ssh_public_key:
            self.ssh_private_key, self.ssh_public_key = generate_keypair(f"suite-cloud-{self.hostname}")
        if not frappe.db.exists("Stalwart Cluster", {"is_default": 1}):
            self.is_default = 1  # the first cluster: there is always a default

    def validate(self) -> None:
        validate_ssh_user_field(self)
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
        if self.is_default and frappe.db.exists("Stalwart Cluster", {"name": ["!=", self.name]}):
            frappe.throw(_("Mark another cluster as default before deleting this one."))
        dependants = (
            ("Stalwart Node", "cluster"),
            ("Suite Tenant", "stalwart_cluster"),
            ("Egress Gateway", "cluster"),
            ("Egress IP Pool", "cluster"),
        )
        for doctype, field in dependants:
            if frappe.db.exists("DocType", doctype) and frappe.db.exists(doctype, {field: self.name}):
                frappe.throw(_("Remove every {0} of this cluster first.").format(_(doctype)))
        dns.delete_cluster_records(self)

    # --- validation -----------------------------------------------------------

    def autoname(self) -> None:
        # Naming runs before validate: the label and zone settle here so the hostname can be derived.
        self.resolve_label()
        self.name = self.hostname

    def resolve_label(self) -> None:
        """Label + zone give the hostname and default domain; both are fixed once the cluster exists."""

        self.dns_zone = self.dns_zone or get_default_zone()
        if not self.dns_zone:
            frappe.throw(_("Create a DNS Zone before creating clusters."))
        self.label = (self.label or "").strip().lower() or naming.next_cluster_label(self.dns_zone)
        if not LABEL.match(self.label):
            frappe.throw(_("Label must be lowercase letters, digits and dashes, e.g. c1 or eu."))
        taken = frappe.db.exists(
            "Stalwart Cluster", {"label": self.label, "dns_zone": self.dns_zone, "name": ["!=", self.name]}
        )
        if taken:
            frappe.throw(
                _("Label {0} is already used by another cluster in {1}.").format(self.label, self.dns_zone)
            )
        self.hostname = f"mail.{self.label}.{self.dns_zone}"
        self.default_domain = f"{self.label}.{self.dns_zone}"
        self.base_url = f"https://{self.hostname}"

    def validate_names(self) -> None:
        self.resolve_label()
        self.title = (self.title or "").strip() or self.hostname
        self.validate_regions()

    def validate_regions(self) -> None:
        seen = set()
        for row in self.regions:
            row.region = (row.region or "").strip().lower()
            if not row.region:
                frappe.throw(_("Region cannot be blank."))
            if row.region in seen:
                frappe.throw(_("Region {0} is listed twice.").format(row.region))
            seen.add(row.region)

    def serves(self, region: str | None) -> bool:
        """No regions means any region."""

        if not self.regions:
            return True
        return bool(region) and region.strip().lower() in {r.region for r in self.regions}

    def apply_defaults(self) -> None:
        self.stalwart_version = validate_version(
            self.stalwart_version or get_config("stalwart_version"), _("Stalwart Version")
        )
        self.acme_directory_url = self.acme_directory_url or get_config("acme_directory_url")
        self.acme_contact_email = self.acme_contact_email or get_config("acme_contact_email")

    def validate_stores(self) -> None:
        for field, kind in STORE_KINDS.items():
            if store_name := self.get(field):
                store = frappe.get_cached_doc("Stalwart Store", store_name)
                if store.kind != kind:
                    frappe.throw(_("{0} must be a {1} store.").format(self.meta.get_label(field), kind))

        # Embedded stores keep their data on one VPS, so such a cluster can never grow.
        self.single_node = int(any(store.is_embedded for store in self.stores()))
        if self.single_node and self.node_count() > 1:
            frappe.throw(
                _("An embedded store cannot be shared by the cluster's {0} nodes.").format(self.node_count())
            )

        in_memory = self.get_store("in_memory_store")
        has_redis = bool(in_memory and in_memory.type.startswith("Redis"))
        self.coordinator = "Default" if has_redis and not self.single_node else "Disabled"
        if self.node_count() > 1 and self.coordinator == "Disabled":
            frappe.throw(_("A multi-node cluster needs a Redis in-memory store to coordinate nodes."))

    def validate_default(self) -> None:
        """Exactly one cluster is the default: it moves by marking another, never by clearing it."""

        before = None if self.is_new() else self.get_doc_before_save()
        if before and before.is_default and not self.is_default:
            frappe.throw(_("Mark another cluster as default instead; there must always be one."))
        if not self.is_default:
            return
        if not self.enabled:
            frappe.throw(_("The default cluster cannot be disabled; mark another cluster as default first."))
        frappe.db.set_value("Stalwart Cluster", {"is_default": 1, "name": ["!=", self.name]}, "is_default", 0)

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

    def stores(self) -> list[Document]:
        return [store for field in STORE_KINDS if (store := self.get_store(field))]

    def get_client(self):
        return get_client(self)

    @property
    def agent_token(self) -> str:
        """The bearer token the cluster's Agents send to Suite Cloud: ``<agent key>:<agent secret>``."""

        return f"{self.agent_key}:{self.get_password('agent_secret')}"

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
        frappe.only_for("System Manager")
        return plan.redacted(plan.cluster_plan(self))

    @frappe.whitelist()
    def sync_config(self) -> dict:
        """Pushes the generated configuration to the running cluster and reloads it."""

        frappe.only_for("System Manager")
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
        frappe.only_for("System Manager")
        report = plan.drift_report(self)
        self.db_set("drift_report", frappe.as_json(report), update_modified=False)
        return report

    @frappe.whitelist()
    def reconcile_directory(self) -> dict:
        """Reports domains/accounts/lists that differ between Suite Cloud and the cluster; never mutates."""

        frappe.only_for("System Manager")
        return reconcile.directory_report(self)

    @frappe.whitelist()
    def finish_bootstrap(self) -> bool:
        frappe.only_for("System Manager")
        return bootstrap.finish_bootstrap(self)

    @frappe.whitelist()
    def rotate_api_key(self) -> None:
        """Mints a fresh management key with the admin credentials and forgets the old one."""

        frappe.only_for("System Manager")
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
    def replace_dkim_keys(self) -> None:
        """Emergency replacement of the default domain's keys after a leak, same selectors.

        Reports and notifications leave from that domain. With a DNS provider on the zone
        Stalwart publishes the new records itself; without one they must be published by hand.
        """

        frappe.only_for("System Manager")
        client = self.get_client()
        domain = client.domains.find_by_name(self.default_domain)
        if not domain:
            frappe.throw(
                _("The cluster does not hold its default domain {0} yet.").format(self.default_domain)
            )
        client.domains.replace_dkim_keys(domain["id"], dkim_algorithms())

    @frappe.whitelist()
    def show_admin_password(self) -> str:
        frappe.only_for("Administrator")
        return self.get_password("admin_password")

    @frappe.whitelist()
    def upgrade_nodes(self) -> list[str]:
        """Upgrades every active node one after the other to the cluster's Stalwart version."""

        frappe.only_for("System Manager")
        return [bootstrap.upgrade_node(node).name for node in self.get_nodes(("Active",))]

    @frappe.whitelist()
    def upgrade_agents(self) -> list[str]:
        """Installs the built Suite Cloud Agent on every active or draining node and every active gateway; Stalwart keeps running."""

        frappe.only_for("System Manager")
        from suite_cloud.telemetry.agent.deploy import upgrade_agents

        return upgrade_agents(self)

    @frappe.whitelist()
    def rotate_agent_secret(self) -> list[str]:
        """Replaces the Agent Secret and reinstalls the cluster's Agents with it. An Agent not yet
        reinstalled is refused and retries, so it loses nothing."""

        frappe.only_for("System Manager")
        from suite_cloud.telemetry.agent.deploy import is_telemetry_enabled, upgrade_agents

        self.agent_secret = frappe.generate_hash(length=40)
        self.save(ignore_permissions=True)
        return upgrade_agents(self) if is_telemetry_enabled() else []


def check_all_clusters() -> None:
    """Daily: drift reports for active clusters."""

    for name in frappe.get_all("Stalwart Cluster", {"status": "Active", "enabled": 1}, pluck="name"):
        cluster = frappe.get_doc("Stalwart Cluster", name)
        try:
            cluster.check_drift()
        except Exception:
            log_exception(f"Drift check failed for {name}", cluster)
