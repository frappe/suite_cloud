# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from suite_infra.suite_infra.doctype.dns_record.dns_record import get_dns_provider

# A change to any of these means the stored provider access may no longer work.
DNS_PROVIDER_FIELDS = (
    "root_domain_name",
    "dns_provider",
    "dns_provider_access_key",
    "dns_provider_access_secret",
    "dns_provider_token",
    "dns_provider_username",
    "dns_provider_client_ip",
    "dns_provider_key",
    "dns_provider_secret",
)


class SuiteInfraSettings(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        ansible_play_timeout: DF.Int
        default_dns_ttl: DF.Int
        dns_provider: DF.Literal[
            "", "AmazonRoute53", "DigitalOcean", "Cloudflare", "Hetzner", "Linode", "Namecheap", "GoDaddy"
        ]
        dns_provider_access_key: DF.Data | None
        dns_provider_access_secret: DF.Password | None
        dns_provider_client_ip: DF.Data | None
        dns_provider_key: DF.Data | None
        dns_provider_private_zone: DF.Check
        dns_provider_secret: DF.Password | None
        dns_provider_token: DF.Password | None
        dns_provider_username: DF.Data | None
        dns_provider_zone_id: DF.Data | None
        root_domain_name: DF.Data | None
        server_deployment_timeout: DF.Int
        server_job_timeout: DF.Int
        stalwart_cli_version: DF.Data
        stalwart_version: DF.Data
    # end: auto-generated types

    def validate(self) -> None:
        if not frappe.flags.in_migrate:
            self.validate_root_domain_name()
            self.validate_dns_provider()

    def on_update(self) -> None:
        if self.has_value_changed("root_domain_name"):
            self.reset_dns_record_verification()

    def validate_root_domain_name(self) -> None:
        if self.root_domain_name:
            self.root_domain_name = self.root_domain_name.lower()

    def validate_dns_provider(self) -> None:
        """Checks the provider credentials are complete and, when they changed, that they work."""

        if not self.dns_provider:
            return

        if not self.root_domain_name:
            frappe.throw(_("Please set the Root Domain Name before configuring the DNS Provider."))

        self.validate_dns_provider_credentials()

        # Set by the installer, which copies credentials over without a network round trip.
        if self.flags.skip_dns_provider_verification:
            return

        if any(self.has_value_changed(field) for field in DNS_PROVIDER_FIELDS):
            get_dns_provider(self).read_dns_records("MX")

    def validate_dns_provider_credentials(self) -> None:
        match self.dns_provider:
            case "AmazonRoute53":
                if not self.dns_provider_access_key or not self.dns_provider_access_secret:
                    frappe.throw(_("Please set the DNS Provider Access Key and Secret."))

            case "DigitalOcean" | "Cloudflare" | "Hetzner" | "Linode" | "Namecheap":
                if not self.dns_provider_token:
                    frappe.throw(_("Please set the DNS Provider Token."))
                elif self.dns_provider == "Namecheap":
                    if not self.dns_provider_username or not self.dns_provider_client_ip:
                        frappe.throw(_("Please set the DNS Provider Username and Client IP."))

            case "GoDaddy":
                if not self.dns_provider_key or not self.dns_provider_secret:
                    frappe.throw(_("Please set the DNS Provider Key and Secret."))

    def reset_dns_record_verification(self) -> None:
        """Every DNS Record hangs off the root domain, so a new root invalidates all of them."""

        frappe.db.set_value("DNS Record", {"is_verified": 1}, "is_verified", 0)

        dns_record_list_link = f'<a href="/app/dns-record">{_("DNS Records")}</a>'
        frappe.msgprint(
            _("Please verify the {0} for the new {1}.").format(
                dns_record_list_link, frappe.bold("Root Domain Name")
            )
        )
