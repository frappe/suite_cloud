from typing import TYPE_CHECKING

import frappe

if TYPE_CHECKING:
    from suite_cloud.dns.provider import DNSProvider


def get_dns_provider(zone: str) -> DNSProvider | None:
    """Returns the DNS Zone's provider client, or None when its records are published by hand."""

    return frappe.get_cached_doc("DNS Zone", zone).get_provider()
