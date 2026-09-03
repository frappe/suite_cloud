"""Turns Stalwart's ``dnsZoneFile`` for a domain into the records a site has to publish.

Stalwart lists everything a full deployment could use. Suite Cloud keeps the receiving and
authentication records, rewrites SPF to include the cluster's sending IPs (the generated
``v=spf1 mx`` would miss egress gateways), and only hands out client-discovery records on
request because the cluster does not hold certificates for customer hostnames.
"""

import re
import shlex
from dataclasses import dataclass

RECORD_LINE = re.compile(r"^(?P<name>\S+)\s+(?:(?P<ttl>\d+)\s+)?(?:IN\s+)?(?P<type>[A-Z]+)\s+(?P<rdata>.+)$")
CLIENT_DISCOVERY = {"MTA-STS", "Auto-config", "Auto-discover"}
MANDATORY = {"Receiving", "Sending", "DKIM", "DMARC"}


@dataclass
class ZoneRecord:
    name: str
    ttl: int | None
    type: str
    rdata: str


def parse_zone_file(text: str) -> list[ZoneRecord]:
    records = []
    for raw in (text or "").splitlines():
        line = _strip_comment(raw).strip()
        if not line or line.startswith("$"):
            continue
        match = RECORD_LINE.match(line)
        if not match:
            continue
        records.append(
            ZoneRecord(
                name=match["name"].rstrip(".").lower(),
                ttl=int(match["ttl"]) if match["ttl"] else None,
                type=match["type"],
                rdata=match["rdata"].strip(),
            )
        )
    return records


def build_domain_records(
    domain: str,
    zone_file: str,
    spf_include: str,
    include_client_discovery: bool = False,
    default_ttl: int = 300,
) -> list[dict]:
    """Rows for Mail Domain DNS Record, in a stable order (mandatory first)."""

    rows = []
    for record in parse_zone_file(zone_file):
        row = _to_row(record, domain, spf_include, default_ttl)
        if row is None:
            continue
        if row["category"] in CLIENT_DISCOVERY and not include_client_discovery:
            continue
        rows.append(row)

    rows.sort(key=lambda r: (0 if r["is_mandatory"] else 1, r["category"], r["host"]))
    return rows


def _to_row(record: ZoneRecord, domain: str, spf_include: str, default_ttl: int) -> dict | None:
    host = _relative_host(record.name, domain)
    if host is None:
        return None

    row = {
        "record_type": record.type,
        "host": host,
        "ttl": record.ttl or default_ttl,
        "priority": 0,
        "value": record.rdata,
        "category": "Other",
    }

    if record.type == "MX":
        priority, _, target = record.rdata.partition(" ")
        row.update(priority=int(priority or 0), value=target.strip().rstrip("."), category="Receiving")
    elif record.type == "TXT":
        text = _unquote(record.rdata)
        row["value"] = text
        if host == "@" and text.startswith("v=spf1"):
            row.update(value=f"v=spf1 include:{spf_include} -all", category="Sending")
        elif "_domainkey" in host:
            row["category"] = "DKIM"
        elif host == "_dmarc":
            row["category"] = "DMARC"
        elif host == "_smtp._tls":
            row["category"] = "TLS Reporting"
        elif host == "_mta-sts":
            row["category"] = "MTA-STS"
        else:
            return None
    elif record.type == "CNAME":
        row["value"] = record.rdata.rstrip(".")
        if host == "mta-sts":
            row["category"] = "MTA-STS"
        elif host == "autoconfig":
            row["category"] = "Auto-config"
        elif host == "autodiscover":
            row["category"] = "Auto-discover"
        else:
            return None
    elif record.type == "SRV":
        row["category"] = "Auto-config"
        row["value"] = record.rdata.rstrip(".")
    elif record.type == "CAA":
        row["value"] = record.rdata
    else:
        return None

    row["is_mandatory"] = int(row["category"] in MANDATORY)
    return row


def _strip_comment(line: str) -> str:
    """Drops a trailing ``; comment`` but leaves semicolons inside quoted TXT data alone."""

    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            return line[:index]
    return line


def _relative_host(name: str, domain: str) -> str | None:
    if name == domain:
        return "@"
    suffix = f".{domain}"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return None


def _unquote(rdata: str) -> str:
    """TXT rdata may be several quoted strings; they concatenate into one value."""

    try:
        parts = shlex.split(rdata)
    except ValueError:
        return rdata.strip('"')
    return "".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
