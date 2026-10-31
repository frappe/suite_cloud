"""Which Stalwart cluster a new tenant's mail lives on."""

import frappe


def pick_cluster(region: str | None) -> str | None:
    """The oldest active cluster of the region's Mail Region, else the default cluster.

    ``region`` names a Region; its ``mail_region`` is what a cluster is placed in.
    """

    mail_region = region and frappe.db.get_value("Region", region, "mail_region")
    active = {"status": "Active", "enabled": 1}
    if mail_region:
        serving = frappe.get_all(
            "Stalwart Cluster", {**active, "region": mail_region}, pluck="name", order_by="creation asc"
        )
        if serving:
            return serving[0]
    return frappe.db.get_value("Stalwart Cluster", {**active, "is_default": 1})
