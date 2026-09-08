import frappe

# Clusters used to be named by a slug (cluster_name) and pools by "<cluster>-<pool>". Both are
# named by their hostname now; the old slug survives as the cluster's editable title.


def execute() -> None:
    frappe.reload_doc("suite_cloud", "doctype", "stalwart_cluster")
    frappe.reload_doc("suite_cloud", "doctype", "egress_ip_pool")

    for cluster in frappe.get_all("Stalwart Cluster", fields=["name", "hostname", "title"]):
        if not cluster.title:
            frappe.db.set_value(
                "Stalwart Cluster", cluster.name, "title", cluster.name, update_modified=False
            )
        if cluster.hostname and cluster.name != cluster.hostname:
            frappe.rename_doc("Stalwart Cluster", cluster.name, cluster.hostname, force=True)

    for pool in frappe.get_all("Egress IP Pool", fields=["name", "hostname"]):
        if pool.hostname and pool.name != pool.hostname:
            frappe.rename_doc("Egress IP Pool", pool.name, pool.hostname, force=True)
