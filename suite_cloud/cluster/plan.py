"""Generates the Stalwart configuration Suite Cloud owns for a cluster.

Two plans exist. The bootstrap plan is applied once on the first node in bootstrap mode and
only names the stores and hostnames (Stalwart provisions everything else with defaults).
The cluster plan holds the objects Suite Cloud manages afterwards (roles, coordinator, ACME,
DNS provider, default domain, system settings, licences) and is re-applied on every sync.
"""

import hashlib
import json
from typing import TYPE_CHECKING

import frappe

from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

BOOTSTRAP_PORT = 8080
API_KEY_DESCRIPTION = "suite-cloud"
DISABLED_ROLE_DESCRIPTION = "suite-disabled"
FIREWALL_PORTS = (25, 465, 587, 143, 993, 110, 995, 443, 4190)
SECRET_MARKER = "***"


def as_set(values) -> dict:
    """Stalwart encodes Set<T> as ``{value: true}``; an empty set is ``{}``."""

    return {value: True for value in values}


def as_list(items) -> dict:
    """Stalwart encodes List<T> as an index-keyed object, ``{"0": item, "1": item}``."""

    return {str(index): item for index, item in enumerate(items)}


PUBLISHED_RECORD_TYPES = {"mx": True}

CLUSTER_ROLES = {
    "full": {
        "name": "full",
        "description": "All tasks and listeners",
        "tasks": {"@type": "EnableAll"},
        "listeners": {"@type": "EnableAll"},
    },
    "frontend": {
        "name": "frontend",
        "description": "Serves clients, never delivers the outbound queue",
        "tasks": {"@type": "DisableSome", "taskTypes": as_set(["outboundMta"])},
        "listeners": {"@type": "EnableAll"},
    },
    "outbound": {
        "name": "outbound",
        "description": "Delivers the outbound queue only",
        "tasks": {"@type": "EnableSome", "taskTypes": as_set(["outboundMta", "taskQueueProcessing"])},
        "listeners": {"@type": "DisableAll"},
    },
}


# --- bootstrap ------------------------------------------------------------------------


def bootstrap_plan(cluster: Document) -> list[dict]:
    """The single Bootstrap update applied while the first node runs in bootstrap mode."""

    store = cluster.get_store
    value = {
        "serverHostname": cluster.hostname,
        "defaultDomain": cluster.default_domain,
        "requestTlsCertificate": False,
        "generateDkimKeys": False,
        "dataStore": store("data_store").config,
        "blobStore": store("blob_store").config if cluster.blob_store else {"@type": "Default"},
        "searchStore": store("search_store").config if cluster.search_store else {"@type": "Default"},
        "inMemoryStore": store("in_memory_store").config if cluster.in_memory_store else {"@type": "Default"},
        "directory": {"@type": "Internal"},
        "tracer": {"@type": "Journal", "level": "info"},
        "dnsServer": {"@type": "Manual"},
    }
    return [{"@type": "update", "object": "Bootstrap", "value": value}]


# --- cluster --------------------------------------------------------------------------


def cluster_plan(cluster: Document) -> list[dict]:
    plan: list[dict] = [
        {"@type": "update", "object": "Coordinator", "value": {"@type": cluster.coordinator or "Disabled"}},
        {"@type": "upsert", "object": "ClusterRole", "matchOn": ["name"], "value": CLUSTER_ROLES},
    ]

    dns_server = dns_server_object(cluster)
    if dns_server:
        plan.append(
            {
                "@type": "upsert",
                "object": "DnsServer",
                "matchOn": ["description"],
                "value": {"dns": dns_server},
            }
        )

    plan.append(
        {
            "@type": "upsert",
            "object": "AcmeProvider",
            "matchOn": ["directory"],
            "value": {"acme": acme_provider(cluster)},
        }
    )
    plan.append(
        {
            "@type": "upsert",
            "object": "Domain",
            "matchOn": ["name"],
            "value": {"default": default_domain(cluster, with_dns=bool(dns_server))},
        }
    )
    plan.append(
        {
            "@type": "update",
            "object": "SystemSettings",
            "value": {
                "defaultHostname": cluster.hostname,
                "defaultDomainId": "#default",
                "mailExchangers": as_list([{"hostname": cluster.hostname, "priority": 10}]),
            },
        }
    )
    plan.append(
        {
            "@type": "upsert",
            "object": "Role",
            "matchOn": ["description"],
            "value": {
                "disabled": {
                    "description": DISABLED_ROLE_DESCRIPTION,
                    "roleIds": {},
                    "enabledPermissions": {"emailReceive": True},
                    "disabledPermissions": {},
                }
            },
        }
    )

    plan.extend(egress_operations(cluster))
    return plan


def acme_provider(cluster: Document) -> dict:
    contact = cluster.acme_contact_email or get_config("acme_contact_email")
    provider = {
        "directory": cluster.acme_directory_url or get_config("acme_directory_url"),
        "challengeType": "Dns01",
    }
    if contact:
        provider["contact"] = {contact: True}
    return provider


def default_domain(cluster: Document, with_dns: bool) -> dict:
    """The cluster zone: carries the ACME certificate covering the ingress and node hostnames."""

    domain = {
        "name": cluster.default_domain,
        "description": "Cluster default domain",
        "isEnabled": True,
        "certificateManagement": {
            "@type": "Automatic",
            "acmeProviderId": "#acme",
            # The wildcard covers the ingress hostname and every node; Let's Encrypt rejects an
            # order that lists a name its wildcard already covers.
            "subjectAlternativeNames": as_set([f"*.{cluster.default_domain}"]),
        },
        "dkimManagement": {"@type": "Manual"},
        "subAddressing": {"@type": "Disabled"},
    }
    if with_dns:
        # Automatic DNS management is what gives DNS-01 its provider. Suite Cloud publishes the
        # zone's records itself, but Stalwart insists on publishing at least one type: the MX of
        # the cluster zone points at the cluster anyway, so it is the harmless choice.
        domain["dnsManagement"] = {
            "@type": "Automatic",
            "dnsServerId": "#dns",
            "origin": cluster.dns_zone,
            "publishRecords": PUBLISHED_RECORD_TYPES,
        }
    else:
        domain["dnsManagement"] = {"@type": "Manual"}
    return domain


def dns_server_object(cluster: Document) -> dict | None:
    """The cluster zone's provider as a Stalwart DnsServer, or None when records are published by hand."""

    return frappe.get_cached_doc("DNS Zone", cluster.dns_zone).stalwart_dns_server()


def admin_account_operation(server: Document, domain_ref: str) -> dict:
    """Pins the permanent administrator's password.

    Bootstrap creates the account with a password nobody is told (the recovery credential is a
    virtual login that bypasses the directory), so the recovery-stage plan sets it. Only that
    plan may carry it: credentials are a whole list, and once the management API key exists it
    lives in the same list and would be wiped by a later push.
    """

    username = server.admin_username or "admin"
    return {
        "@type": "upsert",
        "object": "Account",
        "matchOn": ["name"],
        "value": {
            "admin": {
                "@type": "User",
                "name": username,
                "domainId": domain_ref,
                "description": "Administrator",
                "credentials": {"0": {"@type": "Password", "secret": server.get_password("admin_password")}},
                "roles": {"@type": "Admin"},
                "permissions": {"@type": "Inherit"},
            }
        },
    }


def recovery_plan(cluster: Document) -> list[dict]:
    """The cluster plan as the first node applies it in recovery mode.

    Stalwart provisions its built-in roles only on a normal start that finds no Role objects,
    so the plan must not create any: the disabled-accounts role is pushed once the cluster is
    active. The admin password is pinned here and only here (see the helper).
    """

    operations = [op for op in cluster_plan(cluster) if op["object"] != "Role"]
    operations.append(admin_account_operation(cluster, "#default"))
    return operations


def egress_operations(cluster: Document) -> list[dict]:
    """Relay routes and routing rules for egress pools; filled in by suite_cloud.cluster.egress."""

    try:
        from suite_cloud.cluster.egress import cluster_operations
    except ImportError:
        return []
    return cluster_operations(cluster)


def api_key_permissions() -> dict:
    """Permissions of Suite Cloud's own management key.

    Inherit (the admin account's full set) for now; a Replace list scoped to the object types
    Suite Cloud manages is the follow-up once the identifiers are confirmed on a live server.
    """

    return {"@type": "Inherit"}


# --- per node -----------------------------------------------------------------------------


def node_config(cluster: Document) -> dict:
    """``/etc/stalwart/config.json``: only the data store; everything else lives in it."""

    return cluster.get_store("data_store").config


def node_env(node: Document, mode: str = "normal") -> dict[str, str]:
    """``stalwart.env`` for a node in ``normal``, ``bootstrap`` or ``recovery`` mode."""

    cluster = node.get_cluster()
    env = {
        "STALWART_HOSTNAME": node.hostname,
        "STALWART_PUBLIC_URL": cluster.base_url,
    }
    if mode == "normal":
        env["STALWART_ROLE"] = node.role or "full"
        return env

    env["STALWART_RECOVERY_ADMIN"] = f"{cluster.admin_username}:{cluster.get_password('admin_password')}"
    env["STALWART_RECOVERY_MODE_PORT"] = str(BOOTSTRAP_PORT)
    if mode == "recovery":
        env["STALWART_RECOVERY_MODE"] = "1"
    return env


def systemd_unit() -> str:
    return """[Unit]
Description=Stalwart Mail and Collaboration Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stalwart
Group=stalwart
EnvironmentFile=/etc/stalwart/stalwart.env
ExecStart=/usr/local/bin/stalwart --config /etc/stalwart/config.json
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


# --- rendering ------------------------------------------------------------------------------


def render_env(env: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in env.items())


def to_ndjson(plan: list[dict]) -> str:
    return "".join(json.dumps(operation, separators=(",", ":")) + "\n" for operation in plan)


def secret_value(secret: str | None) -> dict | None:
    return {"@type": "Value", "secret": secret} if secret else None


def secret_strings(plan: list[dict]) -> list[str]:
    """Every literal secret in a plan, so job output that echoes the plan can be masked."""

    found: list[str] = []
    _collect_secrets(plan, found)
    return list(dict.fromkeys(found))


def _collect_secrets(value, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SECRET_KEYS and isinstance(item, str) and item:
                found.append(item)
            else:
                _collect_secrets(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_secrets(item, found)


def marker(plan: list[dict]) -> str:
    """Name of the file a node keeps once it applied this exact plan (secrets excluded)."""

    digest = hashlib.sha1(redacted(plan).encode()).hexdigest()[:12]
    return f".suite-cloud-plan-{digest}"


def redacted(plan: list[dict]) -> str:
    """The plan for display: every secret-carrying value replaced by a marker."""

    return json.dumps([_redact(op) for op in plan], indent=2)


SECRET_KEYS = {
    "secret",
    "secretKey",
    "secretAccessKey",
    "apiKey",
    "authSecret",
    "sentinelSecret",
    "bearerToken",
}


def _redact(value):
    if isinstance(value, dict):
        if value.get("@type") == "Value" and "secret" in value:
            return {"@type": "Value", "secret": SECRET_MARKER}
        return {
            k: (SECRET_MARKER if k in SECRET_KEYS and isinstance(v, str) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


# --- drift ------------------------------------------------------------------------------------


def drift_report(cluster: Document) -> dict:
    """Compares the generated plan with what the cluster currently holds (never mutates)."""

    client = cluster.get_client()
    differences: list[dict] = []
    for operation in cluster_plan(cluster):
        object_type = operation["object"]
        if operation["@type"] == "upsert":
            match_on = operation.get("matchOn") or ["name"]
            existing = client.objects(object_type).get_all()
            for ref, value in operation["value"].items():
                live = next((o for o in existing if all(o.get(k) == value.get(k) for k in match_on)), None)
                if live is None:
                    differences.append({"object": object_type, "ref": ref, "missing": True})
                    continue
                for key, wanted in value.items():
                    if isinstance(wanted, str) and wanted.startswith("#"):
                        continue
                    if live.get(key) != wanted:
                        differences.append({"object": object_type, "ref": ref, "property": key})
        elif operation["@type"] == "update" and not operation.get("id"):
            live = client.singleton(object_type).read()
            for key, wanted in operation["value"].items():
                if isinstance(wanted, str) and wanted.startswith("#"):
                    continue
                if isinstance(wanted, dict) and wanted.get("@type") == "Value":
                    continue
                if live.get(key) != wanted:
                    differences.append({"object": object_type, "property": key})

    return {"checked_at": frappe.utils.now(), "differences": differences}
