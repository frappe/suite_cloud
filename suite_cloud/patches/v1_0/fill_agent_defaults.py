import frappe

# A Single's field defaults only fill a site whose settings were never saved, so sites set up
# before the agent fields existed have them empty, which the now required fields refuse.
SETTINGS = "Suite Cloud Settings"
FIELDS = ("agent_max_attempts", "agent_metrics_interval_seconds")


def execute() -> None:
    meta = frappe.get_meta(SETTINGS)
    for field in FIELDS:
        if frappe.db.get_single_value(SETTINGS, field):
            continue
        frappe.db.set_single_value(SETTINGS, field, meta.get_field(field).default)
