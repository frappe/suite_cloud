import frappe

# Desk role for operators, plus two API-only roles: tenant sites authenticate as one shared
# service user carrying "Suite Site", and Frappe Cloud calls the provisioning endpoints with a
# user carrying "Frappe Cloud". Neither API role holds DocType permissions; the endpoints check
# ownership themselves.
ROLES = (
    ("Suite Cloud Manager", 1),
    ("Suite Site", 0),
    ("Frappe Cloud", 0),
)

SITE_SERVICE_USER = "suite-site@suite-cloud.internal"


def after_install() -> None:
    setup()


def after_migrate() -> None:
    setup()


def setup() -> None:
    """Creates the roles and the shared site service user; safe to run on every migrate."""

    for role_name, desk_access in ROLES:
        ensure_role(role_name, desk_access)

    ensure_site_service_user()
    ensure_setting_defaults()


def ensure_role(role_name: str, desk_access: int) -> None:
    if frappe.db.exists("Role", role_name):
        return

    frappe.get_doc(
        {"doctype": "Role", "role_name": role_name, "desk_access": desk_access, "is_custom": 0}
    ).insert(ignore_permissions=True)


def ensure_site_service_user() -> None:
    """The user every Suite Site API request runs as (see suite_cloud.api.site)."""

    if not frappe.db.exists("User", SITE_SERVICE_USER):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": SITE_SERVICE_USER,
                "first_name": "Suite Site",
                "user_type": "Website User",
                "send_welcome_email": 0,
                "roles": [{"role": "Suite Site"}],
            }
        )
        user.flags.ignore_password_policy = True
        user.insert(ignore_permissions=True)

    # A Website User cannot reach the desk; Frappe upgrades the type on save, so pin it here.
    if frappe.db.get_value("User", SITE_SERVICE_USER, "user_type") != "Website User":
        frappe.db.set_value("User", SITE_SERVICE_USER, "user_type", "Website User", update_modified=False)

    if frappe.db.get_single_value("Suite Cloud Settings", "site_service_user") != SITE_SERVICE_USER:
        frappe.db.set_single_value("Suite Cloud Settings", "site_service_user", SITE_SERVICE_USER)
        frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")


def ensure_setting_defaults() -> None:
    """Fills settings fields added after the singleton was first saved with their defaults.

    Field defaults only apply to new documents, so an existing site would otherwise fail the
    mandatory check the next time anyone saves Suite Cloud Settings.
    """

    settings = frappe.get_single("Suite Cloud Settings")
    changed = False
    for field in settings.meta.fields:
        if field.default and settings.get(field.fieldname) in (None, ""):
            settings.set(field.fieldname, field.default)
            changed = True

    if changed:
        settings.flags.ignore_mandatory = True
        settings.flags.skip_dns_provider_verification = True
        settings.save(ignore_permissions=True)
        frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
