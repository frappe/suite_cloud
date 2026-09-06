"""Proof that a site controls a domain, required before the domain is added to it.

Clusters are shared between sites, so a domain claimed by the wrong site would lock its real
owner out. Nothing is stored for an unverified claim: the site publishes a TXT record carrying
its own token at the domain apex, and the Mail Domain is created only once that record
resolves. The token is fixed for the life of the site, so every domain it adds asks for the
same record and no other site can produce it.
"""

import frappe
from frappe import _

from suite_cloud.dns.resolver import verify_dns_record

VALUE_PREFIX = "frappe-suite-verification="


class DomainNotVerifiedError(frappe.ValidationError):
    """The ownership record is not published yet; the message says what to publish."""

    http_status_code = 422


class OwnershipLookupError(frappe.ValidationError):
    """Public resolvers could not answer; the record may well be there."""

    http_status_code = 503


def assert_ownership(site, domain_name: str) -> None:
    verified = verify_ownership(site, domain_name)
    if verified is None:
        frappe.throw(
            _("The DNS records of {0} could not be resolved right now; try again shortly.").format(
                domain_name
            ),
            OwnershipLookupError,
        )
    if not verified:
        record = ownership_record(site, domain_name)
        frappe.throw(
            _("Publish a TXT record at {0} with the value {1}, then add the domain again.").format(
                domain_name, record["value"]
            ),
            DomainNotVerifiedError,
        )


def verify_ownership(site, domain_name: str) -> bool | None:
    """True/False when public resolvers answered; None when they could not."""

    return verify_dns_record(domain_name, "TXT", ownership_record(site, domain_name)["value"])


def ownership_record(site, domain_name: str) -> dict:
    return {
        "type": "TXT",
        "host": "@",
        "fqdn": domain_name,
        "value": f"{VALUE_PREFIX}{site.domain_verification_token}",
    }
