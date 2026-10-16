import frappe

# A Single's field defaults only fill a site whose settings were never saved, so sites set up
# before Verify Stalwart TLS Certificates existed have no row for it, which reads as off.
SETTINGS = "Suite Cloud Settings"
FIELD = "verify_stalwart_tls"


def execute() -> None:
    if FIELD in frappe.db.get_singles_dict(SETTINGS):
        return
    frappe.db.set_single_value(SETTINGS, FIELD, 1)
