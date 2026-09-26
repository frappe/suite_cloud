import frappe

# A Single's field defaults only fill a site whose settings were never saved, so sites set up before
# Spam Filter Rules Version existed get it empty. Empty lets Stalwart import the latest rules
# release, which may call expression functions the pinned server lacks.
SETTINGS = "Suite Cloud Settings"
FIELD = "spam_filter_rules_version"


def execute() -> None:
    if frappe.db.get_single_value(SETTINGS, FIELD):
        return
    frappe.db.set_single_value(SETTINGS, FIELD, frappe.get_meta(SETTINGS).get_field(FIELD).default)
