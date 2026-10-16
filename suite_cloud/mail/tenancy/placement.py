"""Which Stalwart cluster a new tenant's mail lives on."""

import frappe


def pick_cluster(region: str | None) -> str | None:
    """An active cluster that lists the region, else one that serves any region, else the default.

    ``region`` names a Region; its ``stalwart_region`` (or its own code) is what a cluster lists.
    """

    code = region and (frappe.db.get_value("Region", region, "stalwart_region") or region)
    names = frappe.get_all(
        "Stalwart Cluster",
        {"status": "Active", "enabled": 1},
        pluck="name",
        order_by="is_default desc, creation asc",
    )
    clusters = [frappe.get_cached_doc("Stalwart Cluster", name) for name in names]
    for cluster in sorted(clusters, key=lambda c: not c.regions):  # listing the region comes first
        if cluster.serves(code):
            return cluster.name
    return next((c.name for c in clusters if c.is_default), None)
