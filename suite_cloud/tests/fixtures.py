"""Shared builders for tests: settings, stores, clusters and nodes that never touch a network."""

from unittest.mock import patch

import frappe

ROOT_DOMAIN = "example.test"


def configure_settings(**overrides) -> None:
    settings = frappe.get_single("Suite Cloud Settings")
    settings.root_domain_name = ROOT_DOMAIN
    settings.dns_provider = ""
    settings.acme_contact_email = "ops@example.test"
    for key, value in overrides.items():
        settings.set(key, value)
    settings.flags.skip_dns_provider_verification = True
    settings.save()
    frappe.clear_document_cache("Suite Cloud Settings", "Suite Cloud Settings")
    clear_request_cache()


def clear_request_cache() -> None:
    cache = getattr(frappe.local, "request_cache", None)
    if cache is not None:
        cache.clear()


def make_store(kind: str, type: str, title: str | None = None, **fields):
    doc = frappe.get_doc(
        {
            "doctype": "Stalwart Store",
            "title": title or f"{kind} {type}",
            "kind": kind,
            "type": type,
            **fields,
        }
    )
    doc.insert()
    return doc


def make_cluster(name: str = "blr-1", hostname: str | None = None, multi_node: bool = True, **fields):
    if multi_node:
        data = make_store("Data", "PostgreSql", host="db.example.test", auth_secret="pg-secret")
        memory = make_store("In-Memory", "Redis", url="redis://redis.example.test:6379")
        blob = make_store("Blob", "S3", region="ap-south-1", bucket="mail", access_key="AK", secret_key="SK")
    else:
        data = make_store("Data", "RocksDb", path="/var/lib/stalwart")
        memory = blob = None

    remove_cluster(name)
    cluster = frappe.get_doc(
        {
            "doctype": "Stalwart Cluster",
            "cluster_name": name,
            "hostname": hostname or f"mail.blr.{ROOT_DOMAIN}",
            "data_store": data.name,
            "blob_store": blob.name if blob else None,
            "in_memory_store": memory.name if memory else None,
            **fields,
        }
    )
    cluster.insert()
    return cluster


def remove_cluster(name: str) -> None:
    """Tests share one transaction per class, so a cluster left by an earlier test is torn down here."""

    if not frappe.db.exists("Stalwart Cluster", name):
        return

    nodes = frappe.get_all("Stalwart Node", {"cluster": name}, pluck="name")
    jobs = frappe.get_all("Server Job", {"server": ["in", nodes]}, pluck="name") if nodes else []
    if jobs:
        frappe.db.delete("Server Job Task", {"parent": ["in", jobs]})
        frappe.db.delete("Server Job", {"name": ["in", jobs]})
    frappe.db.delete("DNS Record", {"managed_by": ["in", [*nodes, name]]})
    for node in nodes:
        frappe.delete_doc("Stalwart Node", node, force=True, ignore_permissions=True, ignore_on_trash=True)
    frappe.delete_doc("Stalwart Cluster", name, force=True, ignore_permissions=True, ignore_on_trash=True)


def make_node(cluster, label: str = "n1", ipv4: str = "203.0.113.10", **fields):
    node = frappe.get_doc(
        {
            "doctype": "Stalwart Node",
            "cluster": cluster.name,
            "hostname": f"{label}.{cluster.default_domain}",
            "ipv4_address": ipv4,
            **fields,
        }
    )
    node.insert()
    return node


def no_dns_provider():
    """DNS Record pushes go nowhere: the provider is unset, so records stay unverified."""

    return patch("suite_cloud.suite_cloud.doctype.dns_record.dns_record.get_dns_provider", return_value=None)
