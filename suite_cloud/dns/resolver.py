import socket

import dns.resolver
import frappe
from frappe import _

# Public resolvers, so verification sees what the outside world sees rather than a local cache.
NAMESERVERS = ["1.1.1.1", "8.8.4.4", "8.8.8.8", "9.9.9.9"]


class LookupInconclusive(Exception):
    """The resolver could not answer (timeout, SERVFAIL): says nothing about the record."""


def get_dns_record(fqdn: str, type: str = "A", raise_exception: bool = False) -> dns.resolver.Answer | None:
    """Returns the answer, None when the name/record does not exist, LookupInconclusive on failures."""

    try:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = NAMESERVERS
        resolver.lifetime = 8
        return resolver.resolve(fqdn, type)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer) as e:
        if raise_exception:
            frappe.throw(_("No {0} record found for {1}: {2}").format(type, frappe.bold(fqdn), e))
        return None
    except dns.exception.DNSException as e:
        if raise_exception:
            frappe.throw(_("Could not resolve {0}: {1}").format(frappe.bold(fqdn), e))
        raise LookupInconclusive(str(e)) from e


def verify_dns_record(fqdn: str, type: str, expected_value: str, debug: bool = False) -> bool | None:
    """True/False when the answer is conclusive; None when the resolver could not answer."""

    try:
        answer = get_dns_record(fqdn, type)
    except LookupInconclusive:
        return None
    if not answer:
        return False

    expected = normalize_record_value(type, expected_value, fqdn)
    for data in answer:
        actual = data.exchange.to_text() if type == "MX" else data.to_text()
        if normalize_record_value(type, actual, fqdn) == expected:
            return True
        if debug:
            frappe.msgprint(f"Expected: {expected_value} Got: {actual}")
    return False


def normalize_record_value(type: str, value: str, fqdn: str) -> str:
    """Makes provider and resolver renderings comparable (quotes, chunking, trailing dots)."""

    value = (value or "").strip().replace('" "', "").replace('"', "")
    if type in ("MX", "CNAME", "SRV", "A", "AAAA"):
        value = value.rstrip(".").lower()
    if type == "TXT" and "._domainkey." in fqdn:
        value = value.replace(" ", "")
    return value


def verify_ptr_record(ip_address: str, expected_hostname: str) -> bool:
    """True when the reverse record of ``ip_address`` names ``expected_hostname``."""

    try:
        import dns.reversename

        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = NAMESERVERS
        answer = resolver.resolve(dns.reversename.from_address(ip_address), "PTR")
    except Exception:
        return False

    expected = expected_hostname.rstrip(".").lower()
    return any(record.to_text().rstrip(".").lower() == expected for record in answer)
