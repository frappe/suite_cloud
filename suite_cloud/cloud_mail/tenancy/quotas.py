"""What an account or group may hold: Stalwart's storage quotas, kept as Mail Quota rows.

Disk space (``maxDiskQuota``, in bytes) is one of the rows, and the one every account and group
must have: it defaults to the site's default quota and counts against the site's total. The other
rows (messages, mailboxes, Sieve scripts, calendars and so on) are optional counts. Every push
sends the complete map, so a removed row lifts its limit on the cluster.
"""

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from suite_cloud.cloud_mail.stalwart.directory import DISK_QUOTA, GB, STORAGE_QUOTAS


class QuotaHolder:
    """Mixed into Mail Account and Mail Group: the disk quota, read and written in GB.

    Named so no column of an older schema (``disk_quota_gb``) can shadow a method on load."""

    def disk_row(self):
        return next((row for row in self.quotas if row.quota == DISK_QUOTA), None)

    def disk_quota_bytes(self) -> int:
        row = self.disk_row()
        return cint(row.value) if row else 0

    def allotted_disk_gb(self) -> float:
        return round(self.disk_quota_bytes() / GB, 6)

    def set_disk_quota_gb(self, gb: float) -> None:
        row = self.disk_row()
        if row is None:
            row = self.append("quotas", {"quota": DISK_QUOTA})
        row.value = int(flt(gb) * GB)

    def quota_map(self) -> dict[str, int]:
        return {row.quota: cint(row.value) for row in self.quotas}


def validate(doc: Document) -> None:
    """Known names, listed once, above 0. The disk row's presence is the site's concern."""

    seen = set()
    for row in doc.quotas:
        if row.quota not in STORAGE_QUOTAS:
            frappe.throw(_("{0} is not a quota the cluster knows.").format(row.quota))
        if row.quota in seen:
            frappe.throw(_("Quota {0} is listed twice.").format(row.quota))
        seen.add(row.quota)
        if cint(row.value) <= 0:
            if row.quota == DISK_QUOTA:
                frappe.throw(_("Disk Quota must be above 0 GB."))
            frappe.throw(_("Quota {0} must be above 0; remove the row for no limit.").format(row.quota))


def changed(before: Document, after: Document) -> bool:
    return before.quota_map() != after.quota_map()


def apply(doc: Document, disk_quota_gb: float | None, quotas: Any) -> None:
    """API input: ``quotas`` replaces the optional rows (the disk row stays unless it names one),
    ``disk_quota_gb`` sets the disk row and wins over a ``maxDiskQuota`` inside ``quotas``."""

    if quotas is not None:
        wanted = as_map(quotas)
        disk = doc.disk_row()
        rows = [{"quota": name, "value": limit} for name, limit in wanted.items()]
        if DISK_QUOTA not in wanted and disk is not None:
            rows.append({"quota": DISK_QUOTA, "value": disk.value})
        doc.set("quotas", rows)
    if disk_quota_gb is not None:
        doc.set_disk_quota_gb(disk_quota_gb)


def as_map(value: Any) -> dict[str, int]:
    """``{"maxEmails": 1000, ...}`` or its JSON text; names are checked on save."""

    if isinstance(value, str):
        value = frappe.parse_json(value) if value.strip() else {}
    if value is None:
        return {}
    if not isinstance(value, dict):
        frappe.throw(_("Quotas must be an object of quota names to limits."))
    return {str(name): cint(limit) for name, limit in value.items()}


def disk_quota_gb_by_name(doctype: str, names: list[str]) -> dict[str, float]:
    """The disk quota of many accounts or groups in one query."""

    if not names:
        return {}
    rows = frappe.get_all(
        "Mail Quota",
        filters={"parenttype": doctype, "parent": ["in", names], "quota": DISK_QUOTA},
        fields=["parent", "value"],
    )
    return {row.parent: round(cint(row.value) / GB, 6) for row in rows}
