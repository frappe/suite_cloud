import frappe

from suite_cloud.api.site import current_site, site_api
from suite_cloud.stalwart import get_client

SCHEMA_CACHE_TTL = 3600


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_account_options() -> dict:
    """Locales and time zones the cluster accepts for accounts (from Stalwart's schema)."""

    cluster = current_site().get_cluster()
    key = f"suite_cloud:schema-enums:{cluster.name}"
    cached = frappe.cache.get_value(key)
    if cached:
        return cached

    client = get_client(cluster)
    schema = client.connection.request("GET", f"{cluster.base_url}/api/schema")
    enums = schema.get("enums") or {}
    options = {
        "locales": [_enum_entry(e) for e in enums.get("Locale") or []],
        "time_zones": [_enum_entry(e) for e in enums.get("TimeZone") or []],
    }
    frappe.cache.set_value(key, options, expires_in_sec=SCHEMA_CACHE_TTL)
    return options


def _enum_entry(entry) -> dict:
    if isinstance(entry, dict):
        return {"id": entry.get("id"), "label": entry.get("description") or entry.get("id")}
    return {"id": entry, "label": entry}
