"""Shared builders for tests: settings, stores, clusters and nodes that never touch a network."""

from unittest.mock import patch

import frappe

ROOT_DOMAIN = "example.test"


def configure_settings(**overrides) -> None:
    settings = frappe.get_single("Suite Cloud Settings")
    settings.acme_contact_email = "ops@example.test"
    settings.sign_with_ed25519 = 0  # tests assume the default; the local site may have it on
    for key, value in overrides.items():
        settings.set(key, value)
    settings.save()
    frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
    clear_request_cache()
    make_zone()


def make_zone(domain_name: str = ROOT_DOMAIN, **fields):
    """The default zone every fixture cluster lives under; provider fields default to "by hand"."""

    if frappe.db.exists("DNS Zone", domain_name):
        zone = frappe.get_doc("DNS Zone", domain_name)
    else:
        zone = frappe.new_doc("DNS Zone")
        zone.domain_name = domain_name
    zone.update({"enabled": 1, "is_default": 1, "dns_provider": "", **fields})
    zone.flags.skip_dns_provider_verification = True
    zone.save()
    frappe.clear_document_cache("DNS Zone", domain_name)
    return zone


def clear_request_cache() -> None:
    cache = getattr(frappe.local, "request_cache", None)
    if cache is not None:
        cache.clear()


def no_dns_provider():
    """DNS Record pushes go nowhere: the provider is unset, so records stay unverified."""

    return patch("suite_cloud.suite_cloud.doctype.dns_record.dns_record.get_dns_provider", return_value=None)
