import frappe
from frappe.utils.password import get_decrypted_password

# The root domain and its DNS provider used to be single-valued fields on Suite Cloud Settings.
# They became the DNS Zone DocType so several zones can coexist; this turns the old settings
# into the first (default) zone and points existing clusters and records at it.
SETTINGS = "Suite Cloud Settings"
PLAIN_FIELDS = (
    "dns_provider",
    "dns_provider_zone_id",
    "dns_provider_access_key",
    "dns_provider_key",
    "dns_provider_username",
    "dns_provider_client_ip",
    "dns_provider_private_zone",
)
SECRET_FIELDS = ("dns_provider_access_secret", "dns_provider_secret", "dns_provider_token")


def execute() -> None:
    root = old_value("root_domain_name")
    if root and not frappe.db.exists("DNS Zone", root):
        create_zone(root)
    if root:
        frappe.db.set_value("Stalwart Cluster", {"dns_zone": ["is", "not set"]}, "dns_zone", root)
        frappe.db.set_value("DNS Record", {"dns_zone": ["is", "not set"]}, "dns_zone", root)

    old_fields = ["root_domain_name", *PLAIN_FIELDS, *SECRET_FIELDS]
    frappe.db.delete("Singles", {"doctype": SETTINGS, "field": ["in", old_fields]})
    for field in SECRET_FIELDS:
        frappe.db.delete("__Auth", {"doctype": SETTINGS, "name": SETTINGS, "fieldname": field})


def create_zone(root: str) -> None:
    zone = frappe.new_doc("DNS Zone")
    zone.domain_name = root
    zone.is_default = 1
    for field in PLAIN_FIELDS:
        zone.set(field, old_value(field))
    for field in SECRET_FIELDS:
        zone.set(field, get_decrypted_password(SETTINGS, SETTINGS, field, raise_exception=False))
    zone.flags.skip_dns_provider_verification = True
    zone.insert(ignore_permissions=True)


def old_value(field: str) -> str | None:
    """The fields are gone from the DocType, so their values are read straight from tabSingles."""

    rows = frappe.db.sql(
        "select value from tabSingles where doctype = %s and field = %s", (SETTINGS, field), pluck=True
    )
    return rows[0] if rows else None
