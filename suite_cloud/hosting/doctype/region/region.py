# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.hosting import central

SYNCED_FIELDS = ("display_name", "provider", "country_code", "status")


class Region(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        country_code: DF.Data | None
        display_name: DF.Data | None
        enabled: DF.Check
        last_synced_at: DF.Datetime | None
        mail_region: DF.Link | None
        provider: DF.Data | None
        proxy_zone: DF.Data | None
        reachable: DF.Check
        region: DF.Data
        status: DF.Data | None
    # end: auto-generated types

    def validate(self) -> None:
        self.proxy_zone = (self.proxy_zone or "").strip().lower().strip(".") or None


def sync_regions() -> int:
    """Upserts Central's regions; ``enabled``, ``mail_region`` and the learned ``proxy_zone`` stay."""

    if not frappe.get_cached_doc("Suite Cloud Settings").is_central_configured:
        return 0
    instances = central.get_central_client().list_instances()
    for instance in instances:
        code = instance["region"]
        doc = frappe.get_doc("Region", code) if frappe.db.exists("Region", code) else frappe.new_doc("Region")
        doc.region = code
        doc.update({field: instance.get(field) for field in SYNCED_FIELDS})
        doc.reachable = cint(instance.get("reachable"))
        doc.last_synced_at = now()
        doc.save(ignore_permissions=True)
    return len(instances)
