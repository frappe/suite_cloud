"""Hostnames under a cluster's default domain are handed out, never typed.

Nodes are n1, n2, ...; gateways g1, g2, ...; egress pools p1, p2, ... (under the ``out`` sub-zone
the gateway certificates cover); and the addresses of a pool p1-1, p1-2, ... Numbers count one
past the highest in use in the cluster, so a rebuilt server never inherits a name whose reverse
DNS may still point at the old one.
"""

import re
from collections.abc import Iterable

import frappe

CLUSTER_PREFIX = "c"
NODE_PREFIX = "n"
GATEWAY_PREFIX = "g"
POOL_PREFIX = "p"


def next_label(prefix: str, taken: Iterable[str]) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    highest = max((int(m.group(1)) for label in taken if (m := pattern.match(label or ""))), default=0)
    return f"{prefix}{highest + 1}"


def first_label(hostname: str | None) -> str:
    return (hostname or "").split(".", 1)[0]


def next_hostname(doctype: str, cluster: str, prefix: str) -> str:
    """The next free ``<prefix><n>.<default domain>`` among the cluster's documents of ``doctype``."""

    default_domain = frappe.get_cached_value("Stalwart Cluster", cluster, "default_domain")
    taken = [first_label(h) for h in frappe.get_all(doctype, {"cluster": cluster}, pluck="hostname")]
    return f"{next_label(prefix, taken)}.{default_domain}"


def next_pool_name(cluster: str) -> str:
    taken = frappe.get_all("Egress IP Pool", {"cluster": cluster}, pluck="pool_name")
    return next_label(POOL_PREFIX, taken)


def assign_ehlo_hostnames(pool, default_domain: str) -> None:
    """Names the pool's unnamed addresses ``<pool>-<n>``; rows already named keep their number."""

    prefix = f"{pool.pool_name}-"
    taken = [first_label(row.ehlo_hostname) for row in pool.addresses if row.ehlo_hostname]
    for row in pool.addresses:
        if row.ehlo_hostname:
            continue
        label = next_label(prefix, taken)
        taken.append(label)
        row.ehlo_hostname = f"{label}.{default_domain}"


def next_cluster_label(zone: str) -> str:
    """The next free ``c<n>`` among the clusters of a zone; operators may type a label instead."""

    taken = frappe.get_all("Stalwart Cluster", {"dns_zone": zone}, pluck="label")
    return next_label(CLUSTER_PREFIX, taken)
