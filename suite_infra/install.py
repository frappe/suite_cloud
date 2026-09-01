import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

# Frappe Suite kept these in Mail Settings before the deployment DocTypes moved here. Suite
# drops the fields on upgrade but leaves the stored values behind in tabSingles / __Auth.
LEGACY_SETTINGS_DOCTYPE = "Mail Settings"
LEGACY_SETTINGS_FIELDS = (
    "root_domain_name",
    "default_dns_ttl",
    "dns_provider",
    "dns_provider_access_key",
    "dns_provider_access_secret",
    "dns_provider_client_ip",
    "dns_provider_key",
    "dns_provider_private_zone",
    "dns_provider_secret",
    "dns_provider_token",
    "dns_provider_username",
    "dns_provider_zone_id",
    "stalwart_version",
    "stalwart_cli_version",
    "ansible_play_timeout",
    "server_job_timeout",
    "server_deployment_timeout",
)
LEGACY_PASSWORD_FIELDS = ("dns_provider_access_secret", "dns_provider_secret", "dns_provider_token")


def before_install() -> None:
    """Refuses to install while an older Frappe Suite still ships the deployment DocTypes.

    Both apps would then sync the same DocTypes and the last one to migrate would own them.
    Suite's hand-over patch releases them (it deletes the DocType records and keeps the tables),
    so the fix is simply to migrate Suite first.
    """

    if "suite" not in frappe.get_installed_apps():
        return

    if frappe.db.get_value("DocType", "Mail Server", "module") not in (None, "Suite Infra"):
        frappe.throw(
            _(
                "Frappe Suite on this site still owns the mail server deployment DocTypes. "
                "Update Frappe Suite and run migrate before installing Suite Infra."
            )
        )


def after_install() -> None:
    import_legacy_settings()


def import_legacy_settings() -> None:
    """Copies the deployment settings a Frappe Suite site left behind in Mail Settings.

    Values are taken as-is (they are what the site was running with), so a root domain, DNS
    provider credentials or tuned timeouts survive the split without being re-entered.
    """

    legacy = frappe.db.get_singles_dict(LEGACY_SETTINGS_DOCTYPE)
    if not legacy:
        return

    settings = frappe.get_single("Suite Infra Settings")
    changed = False
    for field in LEGACY_SETTINGS_FIELDS:
        value = get_legacy_value(field, legacy)
        if value in (None, ""):
            continue

        settings.set(field, value)
        changed = True

    if not changed:
        return

    # Install must not depend on the DNS provider being reachable.
    settings.flags.skip_dns_provider_verification = True
    settings.flags.ignore_mandatory = True
    settings.save(ignore_permissions=True)


def get_legacy_value(field: str, legacy: dict) -> str | None:
    if field in LEGACY_PASSWORD_FIELDS:
        return get_decrypted_password(
            LEGACY_SETTINGS_DOCTYPE, LEGACY_SETTINGS_DOCTYPE, field, raise_exception=False
        )

    return legacy.get(field)
