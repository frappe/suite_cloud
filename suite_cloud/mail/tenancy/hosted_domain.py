"""The mail domain a hosted site's tenant gets, ``<label>.<tenant mail zone>``.

Suite Cloud owns the zone, so it publishes the domain's records itself and the tenant can neither
change nor delete the domain.
"""

import frappe
from frappe import _

from suite_cloud.hosting import checkpoint, labels
from suite_cloud.suite_cloud.doctype.dns_record.dns_record import (
    confirm_managed_records,
    reconcile_managed_records,
)
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host

PUBLISHED_TYPES = ("MX", "TXT", "CNAME")  # DNS Record has no SRV


class HostedObjectError(frappe.PermissionError):
    """The domain belongs to the tenant's hosted site; Suite Cloud manages it."""


def ensure(site) -> bool:
    """One step towards the tenant's mail domain being live; True once it is."""

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
    site.db_set("mail_domain_verified", 1)
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


def detach(site) -> None:
    """Unlinks the tenant's mail domain: a dropped site must not hold it. The domain and its
    mailboxes stay, for the tenant's next site, until the tenant is archived with its data."""

    site.db_set({"mail_domain": None, "mail_domain_verified": 0})


def assert_not_hosted(doc) -> None:
    """The tenant API's guard for the hosted domain, which is Suite Cloud's."""

    if doc.hosted:
        frappe.throw(
            _("{0} is managed by Suite Cloud and cannot be changed.").format(doc.name), HostedObjectError
        )
