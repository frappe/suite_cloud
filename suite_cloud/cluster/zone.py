"""Turns Stalwart's ``dnsZoneFile`` for a domain into the records a site has to publish.

Stalwart lists everything a full deployment could use. Suite Cloud keeps the authentication and
receiving records, rewrites SPF to include the cluster's sending IPs (the generated ``v=spf1 mx``
would miss egress gateways), and only hands out records that need HTTPS on a customer hostname
(MTA-STS, autoconfig) on request because the cluster holds no certificates for those names.

Records are sorted into groups, each shown as its own table on Mail Domain and returned to the
site with the group's key. Only the authentication group decides verification.
"""

import re
import shlex
from dataclasses import dataclass

RECORD_LINE = re.compile(r"^(?P<name>\S+)\s+(?:(?P<ttl>\d+)\s+)?(?:IN\s+)?(?P<type>[A-Z]+)\s+(?P<rdata>.+)$")

GROUPS = (
    {
        "key": "authentication_records",
        "label": "Email Authentication",
        "is_mandatory": True,
        "categories": ("SPF", "DKIM", "DMARC"),
        "description": (
            "Records that stop others sending mail as this domain: SPF, DKIM and DMARC. "
            "Mail flows once SPF, DMARC and at least one DKIM selector resolve."
        ),
    },
    {
        "key": "routing_records",
        "label": "Inbound Mail Routing",
        "is_mandatory": False,
        "categories": ("MX",),
        "description": (
            "Points mail sent to this domain at the cluster. Leave it out to keep receiving "
            "elsewhere and use the cluster for sending only."
        ),
    },
    {
        "key": "transport_security_records",
        "label": "Transport Security",
        "is_mandatory": False,
        "categories": ("MTA-STS", "TLS-RPT"),
        "description": (
            "Asks sending servers to insist on encrypted delivery (MTA-STS) and to report failed "
            "or insecure connections (TLS-RPT)."
        ),
    },
    {
        "key": "discovery_records",
        "label": "Service Discovery",
        "is_mandatory": False,
        "categories": ("SRV",),
        "description": (
            "Lets mail, calendar and contacts apps find the servers for this domain on their own."
        ),
    },
    {
        "key": "autoconfig_records",
        "label": "Client Auto-configuration",
        "is_mandatory": False,
        "categories": ("Autoconfig", "Autodiscover", "UA Auto Config"),
        "description": (
            "Lets mail clients set themselves up from just an address. These hostnames are served "
            "over HTTPS by the cluster, which holds no certificate for them."
        ),
    },
)
GROUP_OF = {category: group for group in GROUPS for category in group["categories"]}
MANDATORY = {c for g in GROUPS if g["is_mandatory"] for c in g["categories"]}
# Served over HTTPS on a hostname of the customer's domain, for which the cluster has no certificate.
CERTIFICATE_BOUND = {"MTA-STS", "Autoconfig", "Autodiscover", "UA Auto Config"}


def group_summaries() -> list[dict]:
    """What a site needs to render the groups: key, label, description and whether it is mandatory."""

    return [{k: g[k] for k in ("key", "label", "description", "is_mandatory")} for g in GROUPS]


@dataclass
class ZoneRecord:
    name: str
    ttl: int | None
    type: str
    rdata: str


def parse_zone_file(text: str) -> list[ZoneRecord]:
    records = []
    for line in _logical_lines(text):
        if line.startswith("$"):
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
    """Rows for Mail Domain DNS Record, each carrying its ``group`` key, in group order."""

    rows = []
    for record in parse_zone_file(zone_file):
        row = _to_row(record, domain, spf_include, default_ttl)
        if row is None:
            continue
        if row["category"] in CERTIFICATE_BOUND and not include_client_discovery:
            continue
        rows.append(row)

    order = {c: (i, j) for i, g in enumerate(GROUPS) for j, c in enumerate(g["categories"])}
    rows.sort(key=lambda r: (order[r["category"]], r["host"]))
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
    }

    if record.type == "MX":
        priority, _, target = record.rdata.partition(" ")
        row.update(priority=int(priority or 0), value=target.strip().rstrip("."), category="MX")
    elif record.type == "TXT":
        text = _unquote(record.rdata)
        row["value"] = text
        if host == "@" and text.startswith("v=spf1"):
            row.update(value=f"v=spf1 include:{spf_include} -all", category="SPF")
        elif "_domainkey" in host:
            row["category"] = "DKIM"
        elif host == "_dmarc":
            row["category"] = "DMARC"
        elif host == "_smtp._tls":
            row["category"] = "TLS-RPT"
        elif host == "_mta-sts":
            row["category"] = "MTA-STS"
        elif host == "_ua-auto-config":
            row["category"] = "UA Auto Config"
        else:
            return None
    elif record.type == "CNAME":
        row["value"] = record.rdata.rstrip(".")
        if host == "mta-sts":
            row["category"] = "MTA-STS"
        elif host == "autoconfig":
            row["category"] = "Autoconfig"
        elif host == "autodiscover":
            row["category"] = "Autodiscover"
        elif host == "ua-auto-config":
            row["category"] = "UA Auto Config"
        else:
            return None
    elif record.type == "SRV":
        row.update(value=record.rdata.rstrip("."), category="SRV")
    else:
        return None

    row["group"] = GROUP_OF[row["category"]]["key"]
    row["is_mandatory"] = int(row["category"] in MANDATORY)
    return row


def _logical_lines(text: str):
    """Yields one record per item: parentheses let rdata span lines (Stalwart writes RSA DKIM
    keys that way), so a parenthesised group is joined into a single line without the parens."""

    pending: list[str] = []
    depth = 0
    for raw in (text or "").splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        depth += _paren_balance(line)
        pending.append(line)
        if depth > 0:
            continue
        joined = " ".join(pending)
        pending, depth = [], 0
        yield _drop_parens(joined) if "(" in joined else joined


def _paren_balance(line: str) -> int:
    """Opening minus closing parentheses outside quoted strings."""

    balance = 0
    quoted = False
    for char in line:
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            balance += 1
        elif not quoted and char == ")":
            balance -= 1
    return balance


def _drop_parens(line: str) -> str:
    out = []
    quoted = False
    for char in line:
        if char == '"':
            quoted = not quoted
        if quoted or char not in "()":
            out.append(char)
    return " ".join("".join(out).split())


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
