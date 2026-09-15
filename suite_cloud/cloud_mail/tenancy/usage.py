"""Disk usage for many accounts or groups in as few cluster calls as possible.

Stalwart reports ``usedDiskQuota`` on ``x:Account/get``; asking for a whole page of ids at once
costs one round trip per ``maxObjectsInGet`` objects instead of one per row.
"""

import frappe
from frappe.utils import cint

from suite_cloud.cloud_mail.stalwart.errors import StalwartError
from suite_cloud.cloud_mail.tenancy import sync

USAGE_PROPERTIES = ["id", "usedDiskQuota"]


def used_disk_by_name(rows) -> dict[str, int]:
    """``{name: used bytes}`` for the rows that exist on the cluster.

    Rows need ``name``, ``stalwart_id`` and ``cluster`` (documents or query rows alike). A cluster
    that cannot answer costs the figures, not the listing: the error is logged and its rows come
    back without usage.
    """

    usage: dict[str, int] = {}
    by_cluster: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.stalwart_id and row.cluster:
            by_cluster.setdefault(row.cluster, {})[row.stalwart_id] = row.name
    for cluster, names_by_id in by_cluster.items():  # ids are per cluster; never ask one about another's
        try:
            client = sync.client_for(frappe._dict(cluster=cluster))
            objects = client.accounts.get_many(list(names_by_id), properties=USAGE_PROPERTIES)
        except StalwartError as e:
            frappe.log_error(
                title="[Suite Cloud] Disk usage lookup failed", message=str(e), defer_insert=True
            )
            continue
        for o in objects:
            if o.get("id") in names_by_id:
                usage[names_by_id[o["id"]]] = cint(o.get("usedDiskQuota"))
    return usage
