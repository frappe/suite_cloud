# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.cloud_mail.doctype.dmarc_report.dmarc_report import DEFAULT_RETENTION_DAYS
from suite_cloud.utils import validate_version


class SuiteCloudSettings(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        acme_contact_email: DF.Data | None
        acme_directory_url: DF.Data
        default_dns_ttl: DF.Int
        public_url: DF.Data | None
        server_job_timeout: DF.Int
        sign_with_ed25519: DF.Check
        site_service_user: DF.Link | None
        stalwart_cli_download_url_template: DF.Data
        stalwart_cli_version: DF.Data
        stalwart_download_url_template: DF.Data
        stalwart_version: DF.Data
    # end: auto-generated types

    def validate(self) -> None:
        if self.public_url:
            self.public_url = self.public_url.strip().rstrip("/")
        self.stalwart_version = validate_version(self.stalwart_version, _("Stalwart Version"))
        self.stalwart_cli_version = validate_version(self.stalwart_cli_version, _("Stalwart CLI Version"))
        # A site set up before the field existed has it empty: the default applies rather than a
        # refusal to save anything else. An explicit value under a day is still a mistake.
        if self.dmarc_report_retention_days in (None, ""):
            self.dmarc_report_retention_days = DEFAULT_RETENTION_DAYS
        elif cint(self.dmarc_report_retention_days) < 1:
            frappe.throw(_("DMARC Report Retention must be at least one day."))
