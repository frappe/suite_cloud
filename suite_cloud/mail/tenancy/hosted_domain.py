"""The mail domain a hosted site's tenant gets, ``<label>.<tenant mail zone>``, and the
``notification@`` account the site sends its own mail from.

Suite Cloud owns the zone, so it publishes the domain's records itself and the tenant can neither
change nor delete the domain or the account.
"""

import frappe
from frappe import _

from suite_cloud.hosting import checkpoint, labels, progress
from suite_cloud.mail.doctype.mail_account.mail_account import generate_password
from suite_cloud.suite_cloud.doctype.dns_record.dns_record import (
    confirm_managed_records,
    reconcile_managed_records,
)
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host
from suite_cloud.utils import get_config

# Submission records Stalwart publishes, preferred in order: (host, implicit TLS).
SUBMISSION_RECORDS = (("_submissions._tcp", True), ("_submission._tcp", False))
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
    if not confirm_managed_records("Mail Domain", domain.name):
        return False  # the next poll checks once the provider has them
    if not domain.is_live():
        domain.verify_dns_records()
        if not domain.is_live():
            return False
    progress.sync_site(site)  # verified now; the account takes its own time
    password = ensure_account(site, domain)
    checkpoint()
    site.get_site_client().ensure_email_account(email_account_payload(site, domain, password))
    site.db_set("email_account_created", 1)
    return True


def ensure_domain(site):
    if site.mail_domain:
        return frappe.get_doc("Mail Domain", site.mail_domain)
    name = labels.mail_domain(site.label)
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
    """In the zone the domain was created under, one label up: the tenant mail zone may have moved since."""

    zone = domain.domain_name.split(".", 1)[1]
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
        account = frappe.get_doc("Mail Account", email)
        if not account.is_system_account:
            frappe.throw(
                _("{0} is a mailbox of the tenant, not the site's notification account.").format(email)
            )
        account.reset_password(password)
    else:
        account = frappe.get_doc(
            {
                "doctype": "Mail Account",
                "email": email,
                "tenant": site.tenant,
                "display_name": "Notifications",
                "is_system_account": 1,
                "disable_receiving": 1,  # it only sends the site's mail
            }
        )
        account.flags.password = password
        account.insert(ignore_permissions=True)
    site.db_set("notification_account", email)
    return password


def email_account_payload(site, domain, password: str) -> dict:
    host, port, implicit_tls = submission_endpoint(domain)
    return {
        "email_account_name": "Notifications",
        "email_id": site.notification_account,
        "service": "",
        "smtp_server": host,
        "smtp_port": port,
        "use_ssl_for_outgoing": int(implicit_tls),
        "use_tls": int(not implicit_tls),
        "password": password,
        "enable_outgoing": 1,
        "default_outgoing": 1,
        "always_use_account_email_id_as_sender": 1,
        "always_use_account_name_as_sender_name": 0,
        "enable_incoming": 0,
    }


def submission_endpoint(domain) -> tuple[str, int, bool]:
    """Where mail clients submit, from the domain's own SRV records: what the cluster serves."""

    rows = {row.host: row for row in domain.dns_rows() if row.record_type == "SRV"}
    for host, implicit_tls in SUBMISSION_RECORDS:
        if row := rows.get(host):
            return row.value, row.port, implicit_tls
    frappe.throw(_("Mail Domain {0} publishes no submission record to send through.").format(domain.name))


def detach(site) -> None:
    """Drops the site's notification account, whose password only the site had. The domain and its
    mailboxes are the tenant's: they stay, for the tenant's next site, until the tenant is archived
    with its data."""

    account = site.notification_account
    site.db_set(
        {"notification_account": None, "mail_domain": None, "email_account_created": 0}
    )  # unlinked first: a dropped site must not hold the tenant's domain
    if account and frappe.db.exists("Mail Account", account):
        frappe.delete_doc("Mail Account", account, ignore_permissions=True)


def assert_not_hosted(doc) -> None:
    """The tenant API's guard for the hosted domain, which is Suite Cloud's. The notification
    account is a system account, which the tenant API does not see at all."""

    if doc.hosted:
        frappe.throw(
            _("{0} is managed by Suite Cloud and cannot be changed.").format(doc.name), HostedObjectError
        )
