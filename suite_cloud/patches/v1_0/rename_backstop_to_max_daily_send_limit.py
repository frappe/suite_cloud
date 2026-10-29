import frappe
from frappe.model.utils.rename_field import rename_field

FIELD = "max_daily_send_limit"


def execute() -> None:
    """The backstop becomes the cluster's max daily limit, and a cluster that had it turned off
    gets the default, which the field now requires. A direct update, so no config push runs; the
    cap reaches the cluster with the next Sync Config."""

    rename_field("Stalwart Cluster", "quota_backstop_per_day", FIELD)

    cluster = frappe.qb.DocType("Stalwart Cluster")
    default = int(frappe.get_meta("Stalwart Cluster").get_field(FIELD).default)
    frappe.qb.update(cluster).set(cluster[FIELD], default).where(cluster[FIELD] == 0).run()
