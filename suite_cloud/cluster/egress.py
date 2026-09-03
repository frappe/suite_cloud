"""Egress gateways: outbound-only Stalwart instances that deliver from an IP pool.

The cluster keeps signing and queueing mail; for domains assigned to a pool it hands
messages to the pool's gateways over authenticated SMTP (an MtaRoute of type Relay), and the
gateway binds the outbound connection to one of the pool's addresses. Stalwart has no
forward-proxy support, so a relay hop is the only way to change the source address.
"""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now

from suite_cloud.cluster import dns, plan
from suite_cloud.stalwart.credentials import Credential
from suite_cloud.stalwart.errors import StalwartError

if TYPE_CHECKING:
    from frappe.model.document import Document

GATEWAY_ROLE = "egress"
GATEWAY_DEADLINE_MINUTES = 45
GATEWAY_VARIABLES_BUILDER = "suite_cloud.cluster.egress.build_gateway_variables"
SUITE_RULE_PREFIX = "'egress-"


# --- pool resolution ------------------------------------------------------------------------


def pool_for_domain(domain: Document, site_pool: str | None, cluster_pool: str | None) -> str | None:
    return domain.egress_pool or site_pool or cluster_pool


def domains_by_pool(cluster: Document) -> dict[str, list[str]]:
    """Enabled domains grouped by the pool they leave through (pools without addresses are skipped)."""

    pools = {
        p.name: p
        for p in frappe.get_all(
            "Egress IP Pool", {"cluster": cluster.name}, ["name", "relay_port", "hostname"]
        )
    }
    populated = {
        name
        for name in pools
        if frappe.db.exists("Egress IP Pool Address", {"parent": name, "parenttype": "Egress IP Pool"})
    }
    site_pools = dict(
        frappe.get_all("Suite Site", {"cluster": cluster.name}, ["name", "egress_pool"], as_list=True)
    )
    grouped: dict[str, list[str]] = {}
    domains = frappe.get_all(
        "Mail Domain",
        {"cluster": cluster.name, "enabled": 1},
        ["domain_name", "site", "egress_pool"],
        order_by="domain_name",
    )
    for domain in domains:
        pool = pool_for_domain(domain, site_pools.get(domain.site), cluster.default_egress_pool)
        if pool in populated:
            grouped.setdefault(pool, []).append(domain.domain_name)
    return grouped


# --- cluster side ------------------------------------------------------------------------------


def cluster_operations(cluster: Document) -> list[dict]:
    """Relay routes plus the routing rules that send pooled domains through them."""

    grouped = domains_by_pool(cluster)
    if not grouped:
        return [
            {
                "@type": "update",
                "object": "MtaOutboundStrategy",
                "value": {"route": route_expression(cluster, {})},
            }
        ]

    routes = {}
    for pool_name in grouped:
        pool = frappe.get_cached_doc("Egress IP Pool", pool_name)
        routes[f"egress-{pool.pool_name}"] = {
            "@type": "Relay",
            "name": f"egress-{pool.pool_name}",
            "description": f"Egress pool {pool.pool_name}",
            "address": pool.hostname,
            "port": pool.relay_port,
            "protocol": "smtp",
            "authUsername": cluster.relay_username or "relay",
            "authSecret": plan.secret_value(cluster.get_password("relay_password")),
            "implicitTls": False,
            "allowInvalidCerts": False,
        }

    return [
        {"@type": "upsert", "object": "MtaRoute", "matchOn": ["name"], "value": routes},
        {
            "@type": "update",
            "object": "MtaOutboundStrategy",
            "value": {"route": route_expression(cluster, grouped)},
        },
    ]


def route_expression(cluster: Document, grouped: dict[str, list[str]]) -> dict:
    """Suite Cloud's rules go first; whatever else the strategy holds is preserved."""

    current = current_route_expression(cluster)
    kept = [
        rule
        for rule in (current.get("match") or [])
        if not str(rule.get("then", "")).startswith(SUITE_RULE_PREFIX)
    ]
    rules = []
    for pool_name, domain_names in grouped.items():
        pool_short = frappe.get_cached_value("Egress IP Pool", pool_name, "pool_name")
        condition = " || ".join(f"sender_domain == '{d}'" for d in domain_names)
        rules.append({"if": condition, "then": f"'egress-{pool_short}'"})
    return {"match": rules + kept, "else": current.get("else") or "'mx'"}


def current_route_expression(cluster: Document) -> dict:
    if cluster.status != "Active" or not cluster.get_password("api_key", raise_exception=False):
        return {}
    try:
        return (
            cluster.get_client().singleton("MtaOutboundStrategy").read(properties=["route"]).get("route")
            or {}
        )
    except StalwartError:
        return {}


def apply_pool_changes(pool: Document) -> None:
    """A pool changed: the cluster's routes and every hosting gateway's listeners follow."""

    resync_cluster(pool.get_cluster())
    for gateway_name in pool.gateway_names():
        gateway = frappe.get_doc("Egress Gateway", gateway_name)
        if gateway.status == "Active":
            gateway.push_config()


def resync_cluster_after_commit(cluster_name: str) -> None:
    """Deferred variant for deletes: a remote failure must not roll the delete back."""

    if frappe.flags.do_not_enqueue:
        resync_cluster(frappe.get_doc("Stalwart Cluster", cluster_name))
        return
    frappe.enqueue(
        resync_cluster_job,
        cluster=cluster_name,
        queue="short",
        job_id=f"resync-cluster:{cluster_name}",
        deduplicate=True,
        enqueue_after_commit=True,
    )


def resync_cluster_job(cluster: str) -> None:
    resync_cluster(frappe.get_doc("Stalwart Cluster", cluster))


def resync_cluster(cluster: Document) -> None:
    if cluster.status == "Active" and cluster.get_password("api_key", raise_exception=False):
        cluster.push_config()


# --- gateway side --------------------------------------------------------------------------------


def gateway_plan(gateway: Document) -> list[dict]:
    """The gateway's whole configuration: relay listeners per pool, source IPs, its own certificate."""

    cluster = gateway.get_cluster()
    zone = f"out.{cluster.default_domain}"
    pools = gateway.pools()
    listeners, strategies, rules = {}, {}, []
    for pool in pools:
        addresses = pool.addresses_on(gateway.name)
        if not addresses:
            continue
        listener = f"relay-{pool.pool_name}"
        listeners[listener] = {
            "name": listener,
            "protocol": "smtp",
            "bind": {f"0.0.0.0:{pool.relay_port}": True},
            "useTls": True,
            "tlsImplicit": False,
        }
        strategies[pool.pool_name] = {
            "name": pool.pool_name,
            "description": f"Egress pool {pool.pool_name}",
            "ehloHostname": gateway.hostname,
            "sourceIps": [{"sourceIp": a.ip_address, "ehloHostname": a.ehlo_hostname} for a in addresses],
        }
        rules.append({"if": f"listener == '{listener}'", "then": f"'{pool.pool_name}'"})

    operations: list[dict] = [
        {"@type": "update", "object": "Coordinator", "value": {"@type": "Disabled"}},
    ]
    dns_server = plan.dns_server_object()
    if dns_server:
        operations.append(
            {
                "@type": "upsert",
                "object": "DnsServer",
                "matchOn": ["description"],
                "value": {"dns": dns_server},
            }
        )
    operations.append(
        {
            "@type": "upsert",
            "object": "AcmeProvider",
            "matchOn": ["description"],
            "value": {"acme": plan.acme_provider(cluster)},
        }
    )
    domain = {
        "name": zone,
        "description": "Egress zone",
        "isEnabled": True,
        "certificateManagement": {
            "@type": "Automatic",
            "acmeProviderId": "#acme",
            "subjectAlternativeNames": [gateway.hostname, f"*.{zone}"],
        },
        "dkimManagement": {"@type": "Manual"},
        "dnsManagement": {
            "@type": "Automatic",
            "dnsServerId": "#dns",
            "origin": frappe.db.get_single_value("Suite Cloud Settings", "root_domain_name"),
            "publishRecords": [],
        }
        if dns_server
        else {"@type": "Manual"},
        "subAddressing": {"@type": "Disabled"},
    }
    operations.append(
        {"@type": "upsert", "object": "Domain", "matchOn": ["name"], "value": {"egress": domain}}
    )
    operations.append(
        {
            "@type": "update",
            "object": "SystemSettings",
            "value": {
                "defaultHostname": gateway.hostname,
                "defaultDomainId": "#egress",
                "mailExchangers": [],
            },
        }
    )
    operations.append(
        {
            "@type": "upsert",
            "object": "Account",
            "matchOn": ["name", "domainId"],
            "value": {
                "relay": {
                    "@type": "User",
                    "name": cluster.relay_username or "relay",
                    "domainId": "#egress",
                    "description": "Cluster relay login",
                    "credentials": {
                        "0": {"@type": "Password", "secret": cluster.get_password("relay_password")}
                    },
                    "roles": {"@type": "User"},
                    "permissions": {"@type": "Inherit"},
                    "quotas": {},
                    "aliases": {},
                    "memberGroupIds": {},
                    "locale": "en_US",
                    "encryptionAtRest": {"@type": "Disabled"},
                }
            },
        }
    )
    if listeners:
        operations.append(
            {"@type": "upsert", "object": "NetworkListener", "matchOn": ["name"], "value": listeners}
        )
        operations.append(
            {"@type": "upsert", "object": "MtaConnectionStrategy", "matchOn": ["name"], "value": strategies}
        )
    operations.append(
        {
            "@type": "upsert",
            "object": "ClusterRole",
            "matchOn": ["name"],
            "value": {
                GATEWAY_ROLE: {
                    "name": GATEWAY_ROLE,
                    "description": "Outbound relay only",
                    "tasks": {
                        "@type": "EnableSome",
                        "taskTypes": ["outboundMta", "taskQueueProcessing", "taskScheduler"],
                    },
                    # Every listener stays on so management HTTPS keeps working; the firewall only
                    # opens 443 and the relay ports.
                    "listeners": {"@type": "EnableAll"},
                }
            },
        }
    )
    operations.append(
        {
            "@type": "update",
            "object": "MtaOutboundStrategy",
            "value": {"connection": {"match": rules, "else": "'default'"}},
        }
    )
    return operations


def gateway_bootstrap_plan(gateway: Document) -> list[dict]:
    cluster = gateway.get_cluster()
    value = {
        "serverHostname": gateway.hostname,
        "defaultDomain": f"out.{cluster.default_domain}",
        "requestTlsCertificate": False,
        "generateDkimKeys": False,
        "dataStore": gateway.get_store("data_store").config,
        "blobStore": {"@type": "Default"},
        "searchStore": {"@type": "Default"},
        "inMemoryStore": {"@type": "Default"},
        "directory": {"@type": "Internal"},
        "tracer": {"@type": "Journal", "level": "info"},
        "dnsServer": {"@type": "Manual"},
    }
    return [{"@type": "update", "object": "Bootstrap", "value": value}]


def gateway_env(gateway: Document, mode: str = "normal") -> dict[str, str]:
    env = {"STALWART_HOSTNAME": gateway.hostname, "STALWART_PUBLIC_URL": gateway.base_url}
    if mode == "normal":
        env["STALWART_ROLE"] = GATEWAY_ROLE
        return env
    env["STALWART_RECOVERY_ADMIN"] = f"{gateway.admin_username}:{gateway.get_password('admin_password')}"
    env["STALWART_RECOVERY_MODE_PORT"] = str(plan.BOOTSTRAP_PORT)
    if mode == "recovery":
        env["STALWART_RECOVERY_MODE"] = "1"
    return env


# --- provisioning ----------------------------------------------------------------------------------


def provision_gateway(gateway: Document) -> Document:
    from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

    gateway.bump_config_version(gateway_plan(gateway))
    gateway.set_status("Provisioning")
    return create_server_job(
        gateway,
        "bootstrap-cluster.yml",
        title=f"Provision gateway {gateway.hostname}",
        context={"gateway": gateway.name},
        variables_builder=GATEWAY_VARIABLES_BUILDER,
        callback="after_provision",
    )


def upgrade_gateway(gateway: Document) -> Document:
    from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

    return create_server_job(
        gateway,
        "upgrade-stalwart.yml",
        title=f"Upgrade gateway {gateway.hostname}",
        context={"gateway": gateway.name},
        variables_builder=GATEWAY_VARIABLES_BUILDER,
        callback="after_upgrade",
    )


def build_gateway_variables(context: dict) -> dict:
    from suite_cloud.cluster.bootstrap import SECRET_VARIABLES
    from suite_cloud.utils import get_config

    gateway = frappe.get_doc("Egress Gateway", context["gateway"])
    relay_ports = sorted({p.relay_port for p in gateway.pools()})
    return {
        "node_hostname": gateway.hostname,
        "cluster_hostname": gateway.get_cluster().hostname,
        "ssh_port": gateway.ssh_port or gateway.get_cluster().ssh_port,
        "stalwart_version": gateway.stalwart_version or get_config("stalwart_version"),
        "stalwart_url_template": get_config("stalwart_download_url_template"),
        "stalwart_cli_version": get_config("stalwart_cli_version"),
        "stalwart_cli_url_template": get_config("stalwart_cli_download_url_template"),
        "systemd_unit": plan.systemd_unit(),
        "firewall_ports": [443, *relay_ports],
        "wait_ports": [443, *relay_ports],
        "recovery_port": plan.BOOTSTRAP_PORT,
        "admin_user": gateway.admin_username,
        "admin_password": gateway.get_password("admin_password"),
        "config_version": gateway.config_version or 0,
        "plan_marker": f".suite-cloud-plan-v{gateway.config_version or 0}",
        "env_normal": plan.render_env(gateway_env(gateway, "normal")),
        "env_bootstrap": plan.render_env(gateway_env(gateway, "bootstrap")),
        "env_recovery": plan.render_env(gateway_env(gateway, "recovery")),
        "config_json": frappe.as_json(gateway.get_store("data_store").config),
        "bootstrap_ndjson": plan.to_ndjson(gateway_bootstrap_plan(gateway)),
        "cluster_ndjson": plan.to_ndjson(gateway_plan(gateway)),
        "__secret_keys__": list(SECRET_VARIABLES),
    }


def after_gateway_provision(gateway: Document, job: Document) -> None:
    gateway.db_set(
        {"status": "Provisioned", "provisioned_at": now(), "installed_version": gateway.stalwart_version},
        update_modified=False,
    )
    check_gateway(gateway)


def check_gateway(gateway: Document) -> bool:
    """Activates a provisioned gateway once its certificate is live; then wires the cluster to it."""

    if gateway.status not in ("Provisioned", "Active"):
        return False
    try:
        admin = gateway.get_admin_client()
        if not gateway.get_password("api_key", raise_exception=False):
            _, secret = admin.api_keys.create_secret(
                Credential(description=plan.API_KEY_DESCRIPTION, permissions=plan.api_key_permissions())
            )
            gateway.api_key = secret
            gateway.save(ignore_permissions=True)
    except StalwartError as e:
        started = get_datetime(gateway.provisioned_at or now())
        expired = get_datetime(now()) > add_to_date(started, minutes=GATEWAY_DEADLINE_MINUTES)
        if expired and gateway.status == "Provisioned":
            gateway.set_status("Failed", f"Not reachable after {GATEWAY_DEADLINE_MINUTES} minutes: {e}")
        else:
            gateway.db_set("last_error", str(e)[:1000], update_modified=False)
        return False

    if gateway.status != "Active":
        gateway.db_set(
            {"status": "Active", "last_error": None, "last_config_sync_at": now()}, update_modified=False
        )
        resync_cluster(gateway.get_cluster())
    return True
