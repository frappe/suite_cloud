import socket

import dns.resolver
import frappe
from frappe import _

# Public resolvers, so verification sees what the outside world sees rather than a local cache.
NAMESERVERS = ["1.1.1.1", "8.8.4.4", "8.8.8.8", "9.9.9.9"]


def get_dns_record(fqdn: str, type: str = "A", raise_exception: bool = False) -> dns.resolver.Answer | None:
    """Returns DNS record for the given FQDN and type."""

    err_msg = None

    try:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = NAMESERVERS
        return resolver.resolve(fqdn, type)
    except dns.resolver.NXDOMAIN:
        err_msg = _("{0} does not exist.").format(frappe.bold(fqdn))
    except dns.resolver.NoAnswer:
        err_msg = _("No answer for {0}.").format(frappe.bold(fqdn))
    except dns.exception.DNSException as e:
        err_msg = _(str(e))

    if raise_exception and err_msg:
        frappe.throw(err_msg)


def verify_dns_record(fqdn: str, type: str, expected_value: str, debug: bool = False) -> bool:
    """Verifies that the live DNS answer for fqdn/type contains the expected value."""

    answer = get_dns_record(fqdn, type)
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
