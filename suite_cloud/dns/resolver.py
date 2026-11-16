import socket
from collections.abc import Iterator

import dns.exception
import dns.name
import dns.resolver
import dns.reversename
import frappe

# Public resolvers, so verification sees what the outside world sees rather than a local cache.
NAMESERVERS = ["1.1.1.1", "8.8.4.4", "8.8.8.8", "9.9.9.9"]
LOOKUP_TIMEOUT = 4  # seconds per resolver; an answer normally takes well under one


def verify_dns_record(fqdn: str, type: str, expected_value: str, debug: bool = False) -> bool | None:
    """True when a public resolver returns the expected value; False when every resolver that
    answered lacks it; None when none could answer.
    """

    expected = normalize_record_value(type, expected_value, fqdn)
    answered = False
    for values in answers_from_each_resolver(fqdn, type):
        answered = True
        for actual in values:
            if normalize_record_value(type, actual, fqdn) == expected:
                return True
            if debug:
                frappe.msgprint(f"Expected: {expected_value} Got: {actual}")
    return False if answered else None


def verify_spf_record(fqdn: str, include_host: str) -> bool | None:
    """True when ``fqdn`` publishes exactly one SPF record and it passes ``include_host``.

    The owner may merge other senders into the record. Two SPF records are a permerror at the
    receiver, so they fail however each one reads. Same None contract as ``verify_dns_record``.
    """

    answered = False
    for values in answers_from_each_resolver(fqdn, "TXT"):
        answered = True
        texts = [normalize_record_value("TXT", value, fqdn) for value in values]
        spf_texts = [text for text in texts if is_spf_text(text)]
        if len(spf_texts) == 1 and has_spf_include(spf_texts[0], include_host):
            return True
    return False if answered else None


def normalize_record_value(type: str, value: str, fqdn: str) -> str:
    """Makes provider and resolver renderings comparable (quotes, chunking, trailing dots)."""

    value = (value or "").strip().replace('" "', "").replace('"', "")
    if type in ("MX", "CNAME", "SRV", "A", "AAAA"):
        value = value.rstrip(".").lower()
    if type == "TXT" and "._domainkey." in fqdn:
        value = value.replace(" ", "")
    return value


def is_spf_text(text: str) -> bool:
    return text.lower() == "v=spf1" or text.lower().startswith("v=spf1 ")


def has_spf_include(spf_text: str, include_host: str) -> bool:
    """True when a pass-qualified ``include:`` of ``include_host`` comes before any ``all``,
    since a receiver never evaluates the mechanisms after ``all``."""

    expected = f"include:{include_host.rstrip('.').lower()}"
    for term in spf_text.lower().split()[1:]:
        if term.lstrip("+-~?") == "all":
            return False
        if term.removeprefix("+").rstrip(".") == expected:
            return True
    return False


def verify_ptr_record(ip_address: str, expected_hostname: str) -> bool | None:
    """True when the reverse record of ``ip_address`` names ``expected_hostname``.

    None when the lookup itself failed (timeout, no nameserver answered): that says nothing about
    the record, so callers keep the state they had instead of flapping to unverified.
    """

    try:
        name = dns.reversename.from_address(ip_address)
    except (dns.exception.SyntaxError, ValueError):
        return None

    expected = expected_hostname.rstrip(".").lower()
    answered = False
    for values in answers_from_each_resolver(name, "PTR"):
        answered = True
        if any(value.rstrip(".").lower() == expected for value in values):
            return True
    return False if answered else None


def answers_from_each_resolver(name: str | dns.name.Name, type: str) -> Iterator[list[str]]:
    """Each public resolver's answer as text, asking one resolver at a time.

    Anycast resolvers keep many separate caches, and one still holding "does not exist" from
    before a record was created must not outvote the others, so a caller stops at the first
    resolver that agrees. An empty list means the resolver says the record does not exist;
    resolvers that could not answer (timeout, SERVFAIL) are skipped.
    """

    for nameserver in NAMESERVERS:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [nameserver]
        resolver.lifetime = LOOKUP_TIMEOUT
        try:
            answer = resolver.resolve(name, type)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            yield []
            continue
        except dns.exception.DNSException:
            continue
        yield [record.exchange.to_text() if type == "MX" else record.to_text() for record in answer]
