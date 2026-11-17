import frappe

from suite_cloud.utils import DEFAULT_CONFIG

SETTINGS = "Suite Cloud Settings"


def execute() -> None:
    """Clears saved copies of the app defaults, which the old JSON defaults left on every site."""

    for field, default in DEFAULT_CONFIG.items():
        if frappe.db.get_single_value(SETTINGS, field) == default:
            frappe.db.set_single_value(SETTINGS, field, None)
