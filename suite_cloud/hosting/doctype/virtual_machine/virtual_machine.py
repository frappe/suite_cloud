# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.hosting import central, machines
from suite_cloud.hosting.central import pilot

PLACEABLE_STATUS = "Running"
BUILDING_STATUSES = ("Pending", "Provisioning", "Deploying", PLACEABLE_STATUS)


class VirtualMachine(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        booted_at: DF.Datetime | None
        central_action: DF.Data | None
        central_resource_id: DF.Data | None
        disk_gb: DF.Float
        enabled: DF.Check
        error: DF.SmallText | None
        foreign_site_count: DF.Int
        gateway_url: DF.Data | None
        has_public_ipv4: DF.Check
        has_public_ipv6: DF.Check
        host_verified: DF.Check
        image_id: DF.Data | None
        image_offering: DF.Data | None
        ipv6_address: DF.Data | None
        last_synced_at: DF.Datetime | None
        letsencrypt_email_set: DF.Check
        max_sites: DF.Int
        memory_mb: DF.Int
        pilot_audience: DF.Data | None
        plan: DF.Data | None
        public_ipv4: DF.Data | None
        public_ipv6: DF.Data | None
        ready_since: DF.Datetime | None
        region: DF.Link
        request_key: DF.Data | None
        site_count: DF.Int
        status: DF.Literal[
            "Pending", "Provisioning", "Deploying", "Running", "Paused", "Stopped", "Failed", "Terminated"
        ]
        suite_app_commit: DF.Data | None
        suite_app_status: DF.Literal["Pending", "Installing", "Installed", "Failed"]
        suite_app_task: DF.Data | None
        title: DF.Data | None
        vcpus: DF.Float
        wildcard_zone: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    def before_insert(self) -> None:
        self.status = "Pending"
        self.max_sites = cint(frappe.db.get_single_value("Suite Cloud Settings", "max_sites_per_vm"))
        machines.resolve_server_arguments(self)

    def validate(self) -> None:
        if self.is_new():
            # Central derives the server's digest from the title, so it carries the unique name.
            self.title = f"suite-{self.region}-{self.name}"
            self.request_key = f"suite-cloud-{self.name}"

    def after_insert(self) -> None:
        machines.enqueue_provision(self.name)

    def on_trash(self) -> None:
        if self.central_resource_id and self.status != "Terminated":
            frappe.throw(_("Terminate the server on Central before deleting it here."))

    # --- capacity ---------------------------------------------------------------

    @property
    def site_count(self) -> int:
        if self.is_new():
            return 0
        return frappe.db.count("Suite Site", {"virtual_machine": self.name, "status": ["!=", "Dropped"]})

    @property
    def free_slots(self) -> int:
        return cint(self.max_sites) - self.site_count - cint(self.foreign_site_count)

    @property
    def is_ready(self) -> bool:
        """Set up: Running on Central, its host verified and Suite installed."""

        return (
            self.status == PLACEABLE_STATUS
            and bool(self.host_verified)
            and self.suite_app_status == "Installed"
            and not self.error
        )

    @property
    def has_free_slot(self) -> bool:
        return bool(self.enabled) and self.is_ready and self.free_slots > 0

    @property
    def is_building(self) -> bool:
        """On its way to taking sites: being created or set up, and not stopped by an error. A paused
        or stopped server is not."""

        return not self.is_ready and not self.error and self.status in BUILDING_STATUSES

    def get_pilot_client(self):
        return pilot.get_pilot_client(self)

    # --- actions ----------------------------------------------------------------

    @frappe.whitelist()
    def sync_status(self) -> None:
        frappe.only_for("System Manager")
        machines.refresh_fleet()

    @frappe.whitelist()
    def verify_host(self) -> None:
        frappe.only_for("System Manager")
        self.db_set({"host_verified": 0, "error": None})
        machines.verify_host(self)

    @frappe.whitelist()
    def install_suite_app(self) -> None:
        frappe.only_for("System Manager")
        if self.suite_app_status == "Failed":
            self.db_set("error", None)
            machines.reset_failed_install(self)
        machines.enqueue_provision(self.name)

    @frappe.whitelist()
    def open_pilot(self) -> str:
        """A fresh browser link to Pilot's admin; the cached API token is never handed out."""

        frappe.only_for("System Manager")
        if not self.central_resource_id:
            frappe.throw(_("The server does not exist on Central yet."))
        return central.get_central_client().get_bench_link(self.central_resource_id)

    @frappe.whitelist()
    def decommission(self) -> None:
        frappe.only_for("System Manager")
        self.db_set("enabled", 0)

    @frappe.whitelist()
    def retry(self) -> None:
        frappe.only_for("System Manager")
        machines.retry(self)

    @frappe.whitelist()
    def terminate(self) -> None:
        """Ends the server on Central once none of its Suite Sites is left; needs server:terminate."""

        frappe.only_for("System Manager")
        if not self.central_resource_id:
            frappe.throw(_("The server never existed on Central; Retry closes the creation instead."))
        if self.site_count:
            frappe.throw(_("Drop the Suite Sites on this server first."))
        central.get_central_client().terminate_server(self.central_resource_id)
        self.db_set("enabled", 0)
