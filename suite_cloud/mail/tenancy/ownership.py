"""Proof that a tenant controls a domain, required before the domain is added to it.

Clusters are shared between tenants, so a domain claimed by the wrong tenant would lock its real
owner out. Nothing is stored for an unverified claim: the tenant publishes a TXT record carrying
its own tenant id at the domain apex, and the Mail Domain is created only once that record
resolves. The id is fixed for the life of the tenant, so every domain it adds asks for the same
record and no other tenant can produce it.

The guard is aimed at tenants: it applies to requests made through the tenant API, which run as
the tenant's owner. Operators adding a domain from the desk vouch for it themselves, and a
development cloud's operator may vouch for every tenant's domains at once with Suite Cloud
Settings' "Skip Domain Verification" - test domains exist in no public DNS.
"""

import frappe
from frappe import _
from frappe.utils import cint

from suite_cloud.dns.resolver import verify_dns_record
from suite_cloud.utils import get_config

VALUE_PREFIX = "frappe-suite-verification="


class DomainNotVerifiedError(frappe.ValidationError):
    """The ownership record is not published yet; the message says what to publish."""

    http_status_code = 422


class OwnershipLookupError(frappe.ValidationError):
    """Public resolvers could not answer; the record may well be there."""

    http_status_code = 503


def required(tenant) -> bool:
    """Whether the request has to prove control of the domain: the tenant acting for itself, on a
    cloud that checks."""

    return frappe.session.user == tenant.user and not skipped()


def skipped() -> bool:
    """Whether this cloud takes tenants' word for their domains (Suite Cloud Settings)."""

    return bool(cint(get_config("skip_domain_verification")))


def assert_ownership(tenant, domain_name: str) -> None:
    verified = verify_ownership(tenant, domain_name)
    if verified is None:
        frappe.throw(
            _("The DNS records of {0} could not be resolved right now; try again shortly.").format(
                domain_name
            ),
            OwnershipLookupError,
        )
    if not verified:
        record = ownership_record(tenant, domain_name)
        frappe.throw(
            _("Publish a TXT record at {0} with the value {1}, then add the domain again.").format(
                domain_name, record["value"]
            ),
            DomainNotVerifiedError,
        )


def verify_ownership(tenant, domain_name: str) -> bool | None:
    """True/False when public resolvers answered; None when they could not."""

    return verify_dns_record(domain_name, "TXT", ownership_record(tenant, domain_name)["value"])


def ownership_record(tenant, domain_name: str) -> dict:
    return {
        "type": "TXT",
        "host": "@",
        "fqdn": domain_name,
        "value": f"{VALUE_PREFIX}{tenant.name}",
    }
