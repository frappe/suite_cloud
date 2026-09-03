# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MailDomainDNSRecord(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        category: DF.Literal[
            "Receiving",
            "Sending",
            "DKIM",
            "DMARC",
            "TLS Reporting",
            "MTA-STS",
            "Auto-config",
            "Auto-discover",
            "Other",
        ]
        host: DF.Data | None
        is_mandatory: DF.Check
        is_verified: DF.Check
        last_checked_at: DF.Datetime | None
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        priority: DF.Int
        record_type: DF.Literal["MX", "TXT", "CNAME", "SRV", "CAA"]
        ttl: DF.Int
        value: DF.Text | None
    # end: auto-generated types

    @property
    def fqdn(self) -> str:
        return self.parent if self.host in (None, "", "@") else f"{self.host}.{self.parent}"

    def to_api(self) -> dict:
        return {
            "category": self.category,
            "type": self.record_type,
            "host": self.host,
            "fqdn": self.fqdn,
            "value": self.value,
            "priority": self.priority,
            "ttl": self.ttl,
            "is_mandatory": bool(self.is_mandatory),
            "is_verified": bool(self.is_verified),
            "last_checked_at": self.last_checked_at,
        }
