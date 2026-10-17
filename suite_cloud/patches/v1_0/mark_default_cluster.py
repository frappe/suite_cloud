import frappe


def execute() -> None:
    """Clusters from before there had to be an enabled default get one: the oldest active one, else
    the oldest enabled one. A disabled default, which was allowed then, gives way to it."""

    if frappe.db.exists("Stalwart Cluster", {"is_default": 1, "enabled": 1}):
        return
    name = frappe.db.get_value(
        "Stalwart Cluster", {"status": "Active", "enabled": 1}, "name", order_by="creation asc"
    ) or frappe.db.get_value("Stalwart Cluster", {"enabled": 1}, "name", order_by="creation asc")
    if name:
        frappe.db.set_value("Stalwart Cluster", {"is_default": 1}, "is_default", 0, update_modified=False)
        frappe.db.set_value("Stalwart Cluster", name, "is_default", 1, update_modified=False)
