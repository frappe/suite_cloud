from collections.abc import Callable

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.password import get_decrypted_password

from suite_cloud.utils import log_error

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

    if frappe.db.get_value("DocType", "Mail Server", "module") not in (None, "Suite Cloud"):
        frappe.throw(
            _(
                "Frappe Suite on this site still owns the mail server deployment DocTypes. "
                "Update Frappe Suite and run migrate before installing Suite Cloud."
            )
        )


def after_install() -> None:
    import_legacy_settings()
    complete_adopted_records()


def import_legacy_settings() -> None:
    """Copies the deployment settings a Frappe Suite site left behind in Mail Settings.

    Values are taken as-is (they are what the site was running with), so a root domain, DNS
    provider credentials or tuned timeouts survive the split without being re-entered.
    """

    legacy = frappe.db.get_singles_dict(LEGACY_SETTINGS_DOCTYPE)
    if not legacy:
        return

    settings = frappe.get_single("Suite Cloud Settings")
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


def complete_adopted_records() -> None:
    """Fills in what clusters and servers adopted from an older Suite may lack.

    Suite's retired cluster/server patches used to do this on migrate. A site that skipped them,
    or restored a standalone mail backup, arrives with clusters missing an SSH keypair, recovery
    admin or default domain and servers missing a recovery port and bootstrap plan. Everything here
    only fills blanks, so it is safe on every install; each record is handled on its own so one bad
    row (a server whose cluster is disabled, say) does not block the rest.
    """

    for name in frappe.get_all("Mail Cluster", pluck="name"):
        complete_record("Mail Cluster", name, complete_cluster)

    for name in frappe.get_all("Mail Server", pluck="name"):
        complete_record("Mail Server", name, complete_server)


def complete_record(doctype: str, name: str, complete: Callable[[Document], None]) -> None:
    try:
        complete(frappe.get_doc(doctype, name))
    except Exception:
        log_error(
            _("Could not complete adopted {0} {1}").format(doctype, name),
            frappe.get_traceback(with_context=False),
        )


def complete_cluster(cluster: Document) -> None:
    if not cluster.ssh_public_key:
        cluster.generate_ssh_keypair()

    # default_domain is set-once and save() refuses to change it even from blank, so the first
    # value goes straight to the table; the save below then sees nothing to object to.
    if not cluster.default_domain:
        cluster.db_set("default_domain", cluster.hostname, update_modified=False)

    # The standalone mail app kept the recovery admin in fallback_* columns; the columns outlive
    # the fields, so the values are still there to move over.
    if not cluster.recovery_admin_user and cluster.get("fallback_admin_user"):
        cluster.recovery_admin_user = cluster.get("fallback_admin_user")

    if not cluster.recovery_admin_password and cluster.get("fallback_admin_password"):
        if password := cluster.get_password("fallback_admin_password", raise_exception=False):
            cluster.recovery_admin_password = password

    cluster._initialize_data_store()
    cluster.save()


def complete_server(server: Document) -> None:
    server.recovery_http_port = server.recovery_http_port or 8080
    server.bootstrap_ndjson = server.bootstrap_ndjson or server._generate_bootstrap_ndjson()
    server.save()
