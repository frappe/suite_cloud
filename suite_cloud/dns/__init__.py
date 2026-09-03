from typing import TYPE_CHECKING

import frappe

from suite_cloud.dns.provider import DNSProvider
from suite_cloud.utils import password_or_none

if TYPE_CHECKING:
    from frappe.model.document import Document


def get_dns_provider(settings: Document | None = None) -> DNSProvider | None:
    """Returns the DNS provider configured in Suite Cloud Settings, or None when there is none."""

    settings = settings or frappe.get_cached_doc("Suite Cloud Settings")

    if not settings.dns_provider:
        return None

    return DNSProvider(
        provider=settings.dns_provider,
        domain=settings.root_domain_name,
        access_key=settings.dns_provider_access_key,
        access_secret=password_or_none(settings, "dns_provider_access_secret"),
        auth_key=settings.dns_provider_key,
        auth_secret=password_or_none(settings, "dns_provider_secret"),
        username=settings.dns_provider_username,
        token=password_or_none(settings, "dns_provider_token"),
        client_ip=settings.dns_provider_client_ip,
        zone_id=settings.dns_provider_zone_id,
        private_zone=bool(settings.dns_provider_private_zone),
    )
