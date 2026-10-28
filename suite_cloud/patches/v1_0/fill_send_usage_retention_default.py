import frappe

SETTINGS = "Suite Cloud Settings"
FIELD = "send_usage_retention_days"


def execute() -> None:
    """A Single's field defaults only fill a site whose settings were never saved, so sites set up
    before the field existed have it empty, which the now required field refuses."""

    if frappe.db.get_single_value(SETTINGS, FIELD):
        return
    frappe.db.set_single_value(SETTINGS, FIELD, frappe.get_meta(SETTINGS).get_field(FIELD).default)
