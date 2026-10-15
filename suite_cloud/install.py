import frappe

# Desk role for operators, plus the API-only role of the shared service user every tenant
# request runs as. The API role holds no DocType permissions; the endpoints check ownership.
ROLES = (
    ("Suite Cloud Manager", 1),
    ("Suite Tenant", 0),
)

TENANT_SERVICE_USER = "suite-tenant@suite-cloud.internal"


def after_install() -> None:
    setup()


def after_migrate() -> None:
    setup()


def setup() -> None:
    """Creates the roles and the shared tenant service user; safe to run on every migrate."""

    for role_name, desk_access in ROLES:
        ensure_role(role_name, desk_access)

    ensure_tenant_service_user()
    ensure_setting_defaults()


def ensure_role(role_name: str, desk_access: int) -> None:
    if frappe.db.exists("Role", role_name):
        return

    frappe.get_doc(
        {"doctype": "Role", "role_name": role_name, "desk_access": desk_access, "is_custom": 0}
    ).insert(ignore_permissions=True)


def ensure_tenant_service_user() -> None:
    """The user every Suite Tenant API request runs as (see suite_cloud.api.tenant)."""

    if not frappe.db.exists("User", TENANT_SERVICE_USER):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": TENANT_SERVICE_USER,
                "first_name": "Suite Tenant",
                "user_type": "Website User",
                "send_welcome_email": 0,
                "roles": [{"role": "Suite Tenant"}],
            }
        )
        user.flags.ignore_password_policy = True
        user.insert(ignore_permissions=True)

    # A Website User cannot reach the desk; Frappe upgrades the type on save, so pin it here.
    if frappe.db.get_value("User", TENANT_SERVICE_USER, "user_type") != "Website User":
        frappe.db.set_value("User", TENANT_SERVICE_USER, "user_type", "Website User", update_modified=False)

    if frappe.db.get_single_value("Suite Cloud Settings", "tenant_service_user") != TENANT_SERVICE_USER:
        frappe.db.set_single_value("Suite Cloud Settings", "tenant_service_user", TENANT_SERVICE_USER)
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
        settings.save(ignore_permissions=True)
        frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
