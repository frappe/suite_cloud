import frappe

FIELD = "quota_backstop_per_day"


def execute() -> None:
    """Clusters with the backstop turned off get the default, which the field now requires. A
    direct update, so no config push runs; the cap reaches the cluster with the next Sync Config."""

    cluster = frappe.qb.DocType("Stalwart Cluster")
    default = int(frappe.get_meta("Stalwart Cluster").get_field(FIELD).default)
    frappe.qb.update(cluster).set(cluster[FIELD], default).where(cluster[FIELD] == 0).run()
