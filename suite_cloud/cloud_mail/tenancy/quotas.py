"""The per-account limits Stalwart calls storage quotas, beyond disk space.

Disk space stays a field of its own on Mail Account and Mail Group because the site's total is
validated against it; the other limits (messages, mailboxes, Sieve scripts, calendars and so on)
are rows of Mail Quota under ``quotas``. Every push sends the complete map, so a removed row
lifts its limit on the cluster.
"""

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.cloud_mail.stalwart.directory import DISK_QUOTA, STORAGE_QUOTAS

QUOTA_NAMES = tuple(name for name in STORAGE_QUOTAS if name != DISK_QUOTA)


def validate(doc: Document) -> None:
    seen = set()
    for row in doc.quotas:
        if row.quota not in QUOTA_NAMES:
            frappe.throw(_("{0} is not a quota the cluster knows.").format(row.quota))
        if row.quota in seen:
            frappe.throw(_("Quota {0} is listed twice.").format(row.quota))
        seen.add(row.quota)
        if cint(row.value) <= 0:
            frappe.throw(_("Quota {0} must be above 0; remove the row for no limit.").format(row.quota))


def as_map(doc: Document) -> dict[str, int]:
    return {row.quota: cint(row.value) for row in doc.quotas}


def changed(before: Document, after: Document) -> bool:
    return as_map(before) != as_map(after)


def as_rows(value: Any) -> list[dict]:
    """``{"maxEmails": 1000, ...}`` (or its JSON text) as child rows; validation runs on save."""

    if isinstance(value, str):
        value = frappe.parse_json(value) if value.strip() else {}
    if value is None:
        return []
    if not isinstance(value, dict):
        frappe.throw(_("Quotas must be an object of quota names to limits."))
    return [{"quota": str(name), "value": cint(limit)} for name, limit in value.items()]
