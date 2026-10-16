"""The mail domain a hosted site's tenant gets, ``<label>.<tenant mail zone>``, and the
``notification@`` account the site sends its own mail from.

Suite Cloud owns the zone, so it publishes the domain's records itself and the tenant can neither
change nor delete the domain or the account.
"""

import frappe
from frappe import _

from suite_cloud.hosting import checkpoint
from suite_cloud.mail.doctype.mail_account.mail_account import generate_password
from suite_cloud.suite_cloud.doctype.dns_record.dns_record import reconcile_managed_records
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host
from suite_cloud.utils import get_config

SMTP_PORT = 587
PUBLISHED_TYPES = ("MX", "TXT", "CNAME")  # DNS Record has no SRV


class HostedObjectError(frappe.PermissionError):
    """The domain or account belongs to the tenant's hosted site; Suite Cloud manages it."""


def ensure(site) -> bool:
    """One step towards the site sending through its notification account; True once it does."""

    tenant = frappe.get_doc("Suite Tenant", site.tenant)
    if not tenant.stalwart_cluster:
        site.db_set("error", _("Tenant {0} has no Stalwart cluster; mail is skipped.").format(tenant.name))
        return False
    domain = ensure_domain(site)
    publish_records(domain)
    checkpoint()
    if not domain.is_live():
        domain.verify_dns_records()
        if not domain.is_live():
            return False
    password = ensure_account(site, domain)
    checkpoint()
    site.get_site_client().ensure_email_account(email_account_payload(site, tenant, password))
    site.db_set("email_account_created", 1)
    return True


def ensure_domain(site):
    if site.mail_domain:
        return frappe.get_doc("Mail Domain", site.mail_domain)
    name = f"{site.label}.{get_config('tenant_mail_zone')}"
    if frappe.db.exists("Mail Domain", name):
        domain = frappe.get_doc("Mail Domain", name)
    else:
        domain = frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": name, "tenant": site.tenant, "hosted": 1}
        )
        domain.insert(ignore_permissions=True)
    if domain.tenant != site.tenant or not domain.hosted:
        frappe.throw(_("Mail Domain {0} belongs to someone else.").format(name))
    site.db_set("mail_domain", domain.name)
    return domain


def publish_records(domain) -> None:
    zone = get_config("tenant_mail_zone")
    reconcile_managed_records("Mail Domain", domain.name, record_rows(domain, zone))


def record_rows(domain, zone: str) -> list[dict]:
    """The domain's record rows as DNS Records of ``zone``; SRV rows are left out."""

    return [
        {
            "dns_zone": zone,
            "host": relative_host(row.fqdn, zone),
            "type": row.record_type,
            "value": row.value,
            "priority": row.priority,
            "ttl": row.ttl,
            "category": "SPF" if row.category == "SPF" else "Other",
        }
        for row in domain.dns_rows()
        if row.record_type in PUBLISHED_TYPES
    ]


def ensure_account(site, domain) -> str:
    """The notification account's password, set now: it is never stored, so a retry sets a new one."""

    email = f"{get_config('notification_mailbox_local_part')}@{domain.name}"
    password = generate_password()
    if frappe.db.exists("Mail Account", email):
        frappe.get_doc("Mail Account", email).reset_password(password)
    else:
        account = frappe.get_doc(
            {
                "doctype": "Mail Account",
                "email": email,
                "tenant": site.tenant,
                "display_name": "Notifications",
            }
        )
        account.flags.password = password
        account.insert(ignore_permissions=True)
    site.db_set("notification_account", email)
    return password


def email_account_payload(site, tenant, password: str) -> dict:
    cluster = tenant.get_cluster()
    return {
        "email_account_name": "Notifications",
        "email_id": site.notification_account,
        "service": "",
        "smtp_server": cluster.hostname,
        "smtp_port": SMTP_PORT,
        "use_tls": 1,
        "password": password,
        "enable_outgoing": 1,
        "default_outgoing": 1,
        "always_use_account_email_id_as_sender": 1,
        "always_use_account_name_as_sender_name": 0,
        "enable_incoming": 0,
    }


def remove(site) -> None:
    """Drops the notification account and the hosted domain; the domain takes its records along."""

    account, domain = site.notification_account, site.mail_domain
    site.db_set(
        {"notification_account": None, "mail_domain": None, "email_account_created": 0}
    )  # unlinked first
    if account and frappe.db.exists("Mail Account", account):
        frappe.delete_doc("Mail Account", account, ignore_permissions=True)
    if domain and frappe.db.exists("Mail Domain", domain):
        # Mailboxes the tenant added on the domain stop the delete: they are the tenant's to remove.
        frappe.delete_doc("Mail Domain", domain, ignore_permissions=True)


def assert_not_hosted(doc) -> None:
    """The tenant API's guard: the hosted domain and the notification account are Suite Cloud's."""

    hosted = (
        doc.hosted
        if doc.doctype == "Mail Domain"
        else frappe.db.exists("Suite Site", {"notification_account": doc.name, "status": ["!=", "Dropped"]})
    )
    if hosted:
        frappe.throw(
            _("{0} is managed by Suite Cloud and cannot be changed.").format(doc.name), HostedObjectError
        )
