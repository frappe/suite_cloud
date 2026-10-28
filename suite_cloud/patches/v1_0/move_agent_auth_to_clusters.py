import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

SETTINGS = "Suite Cloud Settings"
OLD_FIELDS = ("telemetry_secret", "telemetry_previous_secret", "telemetry_previous_secret_until")


def execute() -> None:
    """The Agents signed in with one Telemetry Secret shared by every cluster. Each cluster now has
    its own Agent Key and Agent Secret, and Enable Telemetry takes over the secret's other job:
    turning telemetry on and off. Agents still sending the old secret are refused and retry,
    losing nothing, until Upgrade Agents gives them their cluster's credentials."""

    if get_decrypted_password(SETTINGS, SETTINGS, "telemetry_secret", raise_exception=False):
        frappe.db.set_single_value(SETTINGS, "enable_telemetry", 1)
    frappe.db.delete("__Auth", {"doctype": SETTINGS, "fieldname": ("in", OLD_FIELDS)})
    frappe.db.delete("Singles", {"doctype": SETTINGS, "field": ("in", OLD_FIELDS)})

    for name in frappe.get_all("Stalwart Cluster", filters={"agent_key": ("is", "not set")}, pluck="name"):
        frappe.db.set_value("Stalwart Cluster", name, "agent_key", frappe.generate_hash(length=20))
    for name in frappe.get_all("Stalwart Cluster", pluck="name"):
        if not get_decrypted_password("Stalwart Cluster", name, "agent_secret", raise_exception=False):
            set_encrypted_password("Stalwart Cluster", name, frappe.generate_hash(length=40), "agent_secret")
