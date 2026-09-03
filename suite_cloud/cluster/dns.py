"""DNS records Suite Cloud keeps under its root domain for clusters, nodes and pools.

Every record is a DNS Record owned (``managed_by``) by the document that needs it, so
reconciling an owner's desired set adds, keeps and removes rows (and provider records) exactly.
"""

from typing import TYPE_CHECKING

import frappe
from frappe import _

from suite_cloud.suite_cloud.doctype.dns_record.dns_record import (
    delete_managed_records,
    reconcile_managed_records,
)
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document


def relative_host(fqdn: str) -> str:
    """``n1.blr.frappemail.com`` -> ``n1.blr`` (records are relative to the root domain)."""

    root = get_config("root_domain_name")
    if not root:
        frappe.throw(_("Set the Root Domain Name in Suite Cloud Settings."))
    suffix = f".{root}"
    if not fqdn.endswith(suffix):
        frappe.throw(_("{0} is not under the root domain {1}.").format(fqdn, root))
    return fqdn[: -len(suffix)]


def address_records(host: str, ipv4: str | None, ipv6: str | None, category: str) -> list[dict]:
    records = []
    if ipv4:
        records.append({"host": host, "type": "A", "value": ipv4, "category": category})
    if ipv6:
        records.append({"host": host, "type": "AAAA", "value": ipv6, "category": category})
    return records


# --- nodes --------------------------------------------------------------------------


def node_records(node: Document, include_ingress: bool) -> list[dict]:
    records = address_records(relative_host(node.hostname), node.ipv4_address, node.ipv6_address, "Node")
    if include_ingress:
        cluster_host = relative_host(frappe.get_cached_value("Stalwart Cluster", node.cluster, "hostname"))
        records += address_records(cluster_host, node.ipv4_address, node.ipv6_address, "Ingress")
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
    """The include target sites use: ``spf.<zone>`` relative to the root domain."""

    return relative_host(f"spf.{cluster.default_domain}")


def spf_include(cluster: Document) -> str:
    return f"spf.{cluster.default_domain}"


def sending_ips(cluster: Document) -> list[str]:
    """Every address that may deliver mail for the cluster: ingress nodes and egress pools."""

    ips: list[str] = []
    nodes = frappe.get_all(
        "Stalwart Node",
        {"cluster": cluster.name, "enabled": 1, "status": ["in", ["Provisioned", "Active", "Draining"]]},
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
    mechanisms = [f"ip6:{ip}" if ":" in ip else f"ip4:{ip}" for ip in sending_ips(cluster)]
    value = " ".join(["v=spf1", *mechanisms, "-all"])
    return [{"host": spf_host(cluster), "type": "TXT", "value": value, "category": "SPF"}]


def sync_spf_record(cluster: Document) -> None:
    reconcile_managed_records("Stalwart Cluster", cluster.name, spf_records(cluster))


def delete_cluster_records(cluster: Document) -> None:
    delete_managed_records("Stalwart Cluster", cluster.name)
