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
        default_dns_ttl: DF.Int
        dmarc_report_retention_days: DF.Int
        public_url: DF.Data | None
        server_job_timeout: DF.Int
        sign_with_ed25519: DF.Check
        skip_domain_verification: DF.Check
        spam_filter_rules_version: DF.Data | None
        stalwart_cli_download_url_template: DF.Data
        stalwart_cli_version: DF.Data
        stalwart_download_url_template: DF.Data
        stalwart_version: DF.Data
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
