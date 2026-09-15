"""DNS records Suite Cloud keeps in a cluster's DNS Zone for the cluster, its nodes and pools.

Every record is a DNS Record owned (``managed_by``) by the document that needs it, so
reconciling an owner's desired set adds, keeps and removes rows (and provider records) exactly.
"""

from typing import TYPE_CHECKING

import frappe

from suite_cloud.suite_cloud.doctype.dns_record.dns_record import (
    delete_managed_records,
    reconcile_managed_records,
)
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host

if TYPE_CHECKING:
    from frappe.model.document import Document


def cluster_zone(cluster_name: str) -> str:
    return frappe.get_cached_value("Stalwart Cluster", cluster_name, "dns_zone")


def address_records(zone: str, fqdn: str, ipv4: str | None, ipv6: str | None, category: str) -> list[dict]:
    host = relative_host(fqdn, zone)
    records = []
    if ipv4:
        records.append({"dns_zone": zone, "host": host, "type": "A", "value": ipv4, "category": category})
    if ipv6:
        records.append({"dns_zone": zone, "host": host, "type": "AAAA", "value": ipv6, "category": category})
    return records


# --- nodes --------------------------------------------------------------------------


def node_records(node: Document, include_ingress: bool) -> list[dict]:
    zone, ingress = frappe.get_cached_value("Stalwart Cluster", node.cluster, ["dns_zone", "hostname"])
    records = address_records(zone, node.hostname, node.ipv4_address, node.ipv6_address, "Node")
    if include_ingress:
        records += address_records(zone, ingress, node.ipv4_address, node.ipv6_address, "Ingress")
    return records


def sync_node_records(node: Document, include_ingress: bool | None = None) -> None:
    if include_ingress is None:
        include_ingress = bool(node.in_ingress_dns)
    reconcile_managed_records("Stalwart Node", node.name, node_records(node, include_ingress))
    if include_ingress != bool(node.in_ingress_dns):
        node.db_set("in_ingress_dns", int(include_ingress), update_modified=False)


def delete_node_records(node: Document) -> None:
    delete_managed_records("Stalwart Node", node.name)


# --- cluster --------------------------------------------------------------------------


def spf_host(cluster: Document) -> str:
    """The include target sites use: ``spf.<cluster zone>`` relative to the DNS Zone."""

    return relative_host(spf_include(cluster), cluster.dns_zone)


def spf_include(cluster: Document) -> str:
    return f"spf.{cluster.default_domain}"


# A TXT character-string holds 255 bytes; a longer SPF text is a permerror at the receiver.
SPF_TEXT_LIMIT = 255


def spf_texts(include_host: str, mechanisms: list[str]) -> list[tuple[str, str]]:
    """``[(host, text)]`` for the include target: one record while it fits, else the target lists
    ``include:`` of numbered children that each stay within a TXT string."""

    single = " ".join(["v=spf1", *mechanisms, "-all"])
    if len(single) <= SPF_TEXT_LIMIT:
        return [(include_host, single)]

    chunks: list[list[str]] = [[]]
    for mechanism in mechanisms:
        candidate = " ".join(["v=spf1", *chunks[-1], mechanism, "-all"])
        if chunks[-1] and len(candidate) > SPF_TEXT_LIMIT:
            chunks.append([])
        chunks[-1].append(mechanism)

    base = include_host.split(".", 1)[1]  # spf.<zone> -> <zone>
    children = [(f"spf{i}.{base}", chunk) for i, chunk in enumerate(chunks, start=1)]
    parent = " ".join(["v=spf1", *[f"include:{host}" for host, _ in children], "-all"])
    if len(parent) > SPF_TEXT_LIMIT:
        raise ValueError("too many sending addresses for one SPF include chain")
    return [
        (include_host, parent),
        *[(host, " ".join(["v=spf1", *chunk, "-all"])) for host, chunk in children],
    ]


def sending_ips(cluster: Document) -> list[str]:
    """Every address that may deliver mail for the cluster: ingress nodes and egress pools."""

    ips: list[str] = []
    nodes = frappe.get_all(
        "Stalwart Node",
        {"cluster": cluster.name, "status": ["in", ["Provisioned", "Active", "Draining"]]},
        ["ipv4_address", "ipv6_address"],
    )
    for node in nodes:
        ips += [ip for ip in (node.ipv4_address, node.ipv6_address) if ip]

    if frappe.db.exists("DocType", "Egress IP Pool Address"):
        pools = frappe.get_all("Egress IP Pool", {"cluster": cluster.name}, pluck="name")
        if pools:
            ips += frappe.get_all(
                "Egress IP Pool Address",
                {"parent": ["in", pools], "parenttype": "Egress IP Pool"},
                pluck="ip_address",
            )
    return list(dict.fromkeys(ips))


def spf_records(cluster: Document) -> list[dict]:
    """The include target listing every sending address, and the cluster zone's own SPF."""

    mechanisms = [f"ip6:{ip}" if ":" in ip else f"ip4:{ip}" for ip in sending_ips(cluster)]
    records = [
        {
            "dns_zone": cluster.dns_zone,
            "host": relative_host(host, cluster.dns_zone),
            "type": "TXT",
            "value": value,
            "category": "SPF",
        }
        for host, value in spf_texts(spf_include(cluster), mechanisms)
    ]
    # Reports and notifications leave from the cluster zone itself, so it needs SPF like any
    # customer domain; it references the include target the same way customers do.
    zone_spf = {
        "dns_zone": cluster.dns_zone,
        "host": relative_host(cluster.default_domain, cluster.dns_zone),
        "type": "TXT",
        "value": f"v=spf1 include:{spf_include(cluster)} -all",
        "category": "SPF",
    }
    return [*records, zone_spf]


def egress_zone(cluster: Document) -> str:
    """The sub-zone pool hostnames live under; every gateway certificate carries its wildcard."""

    return f"out.{cluster.default_domain}"


def sync_spf_record(cluster: Document) -> None:
    reconcile_managed_records("Stalwart Cluster", cluster.name, spf_records(cluster))


def delete_cluster_records(cluster: Document) -> None:
    delete_managed_records("Stalwart Cluster", cluster.name)


# --- egress -----------------------------------------------------------------------------------


def gateway_records(gateway: Document) -> list[dict]:
    """The gateway's address, and SPF for its hostname: its notifications leave from that domain,
    from the same addresses every other sender of the cluster uses."""

    cluster = frappe.get_cached_doc("Stalwart Cluster", gateway.cluster)
    records = address_records(cluster.dns_zone, gateway.hostname, gateway.ipv4_address, None, "Egress")
    records.append(
        {
            "dns_zone": cluster.dns_zone,
            "host": relative_host(gateway.hostname, cluster.dns_zone),
            "type": "TXT",
            "value": f"v=spf1 include:{spf_include(cluster)} -all",
            "category": "SPF",
        }
    )
    return records


def sync_gateway_records(gateway: Document) -> None:
    reconcile_managed_records("Egress Gateway", gateway.name, gateway_records(gateway))


def delete_gateway_records(gateway: Document) -> None:
    delete_managed_records("Egress Gateway", gateway.name)


def pool_records(pool: Document) -> list[dict]:
    """``<pool>.out.<zone>`` -> every gateway hosting the pool, plus one A record per EHLO name."""

    zone = cluster_zone(pool.cluster)
    records = []
    for gateway_name in pool.gateway_names():
        ip, status = frappe.db.get_value("Egress Gateway", gateway_name, ["ipv4_address", "status"])
        if status != "Active":
            continue  # a gateway that is not serving must not be in the round robin
        records += address_records(zone, pool.hostname, ip, None, "Egress")
    for row in pool.addresses:
        ipv4, ipv6 = (None, row.ip_address) if ":" in row.ip_address else (row.ip_address, None)
        records += address_records(zone, row.ehlo_hostname, ipv4, ipv6, "Egress")
    return records


def sync_pool_records(pool: Document) -> None:
    reconcile_managed_records("Egress IP Pool", pool.name, pool_records(pool))


def delete_pool_records(pool: Document) -> None:
    delete_managed_records("Egress IP Pool", pool.name)
