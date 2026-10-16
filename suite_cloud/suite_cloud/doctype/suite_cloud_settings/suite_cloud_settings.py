# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.utils import clear_config_cache, validate_version


class SuiteCloudSettings(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        acme_contact_email: DF.Data
        acme_directory_url: DF.Data
        allow_signup: DF.Check
        auto_create_vms: DF.Check
        auto_place_tenants: DF.Check
        central_api_key: DF.Data | None
        central_api_secret: DF.Password | None
        central_image_offering: DF.Data
        central_plan: DF.Data | None
        central_team: DF.Data | None
        central_url: DF.Data | None
        default_dns_ttl: DF.Int
        default_region: DF.Link | None
        dmarc_report_retention_days: DF.Int
        max_sites_per_vm: DF.Int
        min_free_site_slots: DF.Int
        notification_mailbox_local_part: DF.Data
        public_url: DF.Data | None
        ready_sites_per_region: DF.Int
        server_job_timeout: DF.Int
        sign_with_ed25519: DF.Check
        site_dns_zone: DF.Link | None
        skip_domain_verification: DF.Check
        spam_filter_rules_version: DF.Data | None
        stalwart_cli_download_url_template: DF.Data
        stalwart_cli_version: DF.Data
        stalwart_download_url_template: DF.Data
        stalwart_version: DF.Data
        suite_app_ref: DF.Data
        suite_app_repo: DF.Data
        tenant_mail_zone: DF.Link | None
        tls_report_retention_days: DF.Int
        verify_stalwart_tls: DF.Check
    # end: auto-generated types

    def validate(self) -> None:
        if self.public_url:
            self.public_url = self.public_url.strip().rstrip("/")
        self.stalwart_version = validate_version(self.stalwart_version, _("Stalwart Version"))
        self.stalwart_cli_version = validate_version(self.stalwart_cli_version, _("Stalwart CLI Version"))
        self.spam_filter_rules_version = validate_version(
            self.spam_filter_rules_version, _("Spam Filter Rules Version")
        )
        self.validate_report_retention()
        self.validate_hosting()

    def on_update(self) -> None:
        clear_config_cache()
        before = self.get_doc_before_save()
        if before and cint(before.skip_domain_verification) and not cint(self.skip_domain_verification):
            from suite_cloud.mail.doctype.mail_domain.mail_domain import (
                enqueue_recheck_of_vouched_domains,
            )

            enqueue_recheck_of_vouched_domains()

    def validate_report_retention(self) -> None:
        for field in ("dmarc_report_retention_days", "tls_report_retention_days"):
            if cint(self.get(field)) < 1:
                frappe.throw(_("{0} must be at least one day.").format(_(self.meta.get_label(field))))

    def validate_hosting(self) -> None:
        if self.central_url:
            self.central_url = self.central_url.strip().rstrip("/")
        for field in ("max_sites_per_vm", "ready_sites_per_region", "min_free_site_slots"):
            if cint(self.get(field)) < 0:
                frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(field))))
        if cint(self.max_sites_per_vm) < 1:
            frappe.throw(_("A server must take at least one site."))
        self.validate_hosting_zones()

    def validate_hosting_zones(self) -> None:
        """Site and mail domains share a label, so they need zones of their own: a CNAME at
        ``acme.<site zone>`` cannot sit next to the MX and TXT records of ``acme.<mail zone>``."""

        if self.site_dns_zone and self.site_dns_zone == self.tenant_mail_zone:
            frappe.throw(_("The site DNS zone and the tenant mail zone must be different zones."))
        for field in ("site_dns_zone", "tenant_mail_zone"):
            zone = self.get(field)
            if zone and not frappe.db.get_value("DNS Zone", zone, "dns_provider"):
                frappe.throw(_("DNS Zone {0} needs a DNS provider to publish records.").format(zone))
        if self.tenant_mail_zone and (
            cluster := frappe.db.get_value("Stalwart Cluster", {"dns_zone": self.tenant_mail_zone}, "name")
        ):
            frappe.throw(
                _("DNS Zone {0} is the zone of cluster {1}; tenant mail domains need another zone.").format(
                    self.tenant_mail_zone, cluster
                )
            )

    @property
    def is_central_configured(self) -> bool:
        return bool(
            self.central_url and self.central_team and self.central_api_key and self.central_api_secret
        )

    @frappe.whitelist()
    def test_central_connection(self) -> dict:
        """What the API key may do in the team, as Central reports it."""

        frappe.only_for("System Manager")
        from suite_cloud.hosting import central

        return central.get_central_client().my_capabilities()

    @frappe.whitelist()
    def sync_regions(self) -> int:
        frappe.only_for("System Manager")
        from suite_cloud.hosting.doctype.region.region import sync_regions

        return sync_regions()
