# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


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
        site_service_user: DF.Link | None
        stalwart_cli_download_url_template: DF.Data
        stalwart_cli_version: DF.Data
        stalwart_download_url_template: DF.Data
        stalwart_version: DF.Data
    # end: auto-generated types

    def validate(self) -> None:
        if self.public_url:
            self.public_url = self.public_url.strip().rstrip("/")
