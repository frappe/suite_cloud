"""What provisioning puts on a node for the agent: its config.json, its unit and the Server Jobs
that install it."""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint, get_url

from suite_cloud.mail.cluster import egress, plan, quota
from suite_cloud.mail.stalwart import verify_tls
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.telemetry.agent.build import (
    ARCHITECTURES,
    AgentBuild,
    is_quota_enabled,
    is_telemetry_enabled,
)
from suite_cloud.utils import get_config, log_error, mask_secrets, secret_spellings

if TYPE_CHECKING:
    from frappe.model.document import Document

SOURCE = "stalwart"
CONFIG_PATH = "/etc/suite-cloud-agent/config.json"
STATE_DIR = "/var/lib/suite-cloud-agent"
PROMETHEUS_URL = "https://127.0.0.1:443/metrics/prometheus"
PLAYBOOK = "install-agent.yml"
ROLL_OUT_KEY = "suite_cloud:agent_roll_out"  # the settings change the last roll-out applied
ROLL_OUT_TIMEOUT = 600  # seconds a roll-out may hold, or wait for, the roll-out lock
NODE_BUILDER = "suite_cloud.mail.cluster.bootstrap.build_node_variables"
GATEWAY_BUILDER = "suite_cloud.mail.cluster.egress.build_gateway_variables"


def agent_config(server: Document) -> dict:
    """``config.json`` for a Stalwart Node or Egress Gateway. The Go side reads exactly this shape
    (``agent/internal/config``), so the two change together."""

    config = {
        "suite_cloud": {
            "url": get_config("public_url") or get_url(),
            "secret": server.get_cluster().agent_token,
            "requests_per_minute": cint(get_config("agent_requests_per_minute")),
            "records_per_request": cint(get_config("agent_records_per_request")),
            "max_attempts": cint(get_config("agent_max_attempts")),
        },
        "node": {"source": SOURCE, "host_ip": server.ipv4_address, "hostname": server.hostname},
        "state_dir": STATE_DIR,
        "logs": [
            {
                "name": SOURCE,
                "path": f"{plan.LOG_DIRECTORY}/stalwart.*",
                "format": SOURCE,
                "ship_existing": bool(cint(get_config("agent_ship_existing_logs"))),
                "delete_after_days": cint(get_config("agent_log_delete_after_days")),
            }
        ],
        "metrics": {"interval_seconds": cint(get_config("agent_metrics_interval_seconds")), "mounts": ["/"]},
    }
    if scrapes_metrics(server):
        owner = server if server.doctype == "Egress Gateway" else server.get_cluster()
        config["metrics"]["stalwart"] = {
            "url": PROMETHEUS_URL,
            "username": plan.METRICS_USERNAME,
            "password": owner.get_password("metrics_password"),
            "tls_server_name": server.hostname,
            "verify_tls": verify_tls(),
        }
    if is_quota_hook_server(server):
        config["quota"] = quota_config(server.get_cluster())
    return config


def is_quota_hook_server(server: Document) -> bool:
    """Stalwart calls the hook on the node that took the message: a node with listeners. Gateways
    and outbound-only nodes only relay."""

    is_client_node = server.doctype == "Stalwart Node" and (server.role or "full") != "outbound"
    return is_client_node and is_quota_enabled()


def quota_config(cluster: Document) -> dict:
    """The sending-limit hook the Agent answers on loopback, and the store all of the cluster's
    Agents share."""

    return {
        "hook_port": quota.HOOK_PORT,
        "hook_token": cluster.get_password("quota_hook_token"),
        "store": quota.store_config(cluster),
    }


def scrapes_metrics(server: Document) -> bool:
    """The cluster's switch turns the exporter on. A gateway keeps every listener on; an
    outbound-only node runs none, so there is nothing to scrape."""

    if not server.get_cluster().enable_metrics:
        return False
    return server.doctype == "Egress Gateway" or (server.role or "full") != "outbound"


def systemd_unit() -> str:
    """Runs as the stalwart user: the log directory is its, and deleting shipped files needs write
    on it. The sandbox keeps Stalwart's own config and data out of reach all the same."""

    return f"""[Unit]
Description=Suite Cloud Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stalwart
Group=stalwart
ExecStartPre=/usr/local/bin/suite-cloud-agent --config {CONFIG_PATH} --check
ExecStart=/usr/local/bin/suite-cloud-agent --config {CONFIG_PATH}
Restart=always
RestartSec=5
TimeoutStopSec=90
StateDirectory=suite-cloud-agent
StateDirectoryMode=0750
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={plan.LOG_DIRECTORY}
InaccessiblePaths=/etc/stalwart /var/lib/stalwart
MemoryMax=256M

[Install]
WantedBy=multi-user.target
"""


def agent_variables(server: Document, restart: bool = False) -> dict:
    """What install-agent.yml needs, merged into the node and gateway variables so the two never
    differ. With telemetry off or without a build the play stops the agent instead. With
    ``restart`` the play restarts the agent even when nothing on the node changed."""

    disabled = {
        "agent_enabled": False,
        "agent_version": "",
        "agent_binaries": {},
        "agent_config_json": "",
        "agent_systemd_unit": "",
        "agent_restart": restart,
        "__secret_values__": [],
    }
    if not is_telemetry_enabled():
        return disabled
    build = AgentBuild.last_built()
    if not build or not build.is_built:
        log_error(
            "Agent not built",
            f"{server.hostname}: telemetry is on but no agent build exists; run Build Agent.",
        )
        return disabled

    config = agent_config(server)
    return {
        "agent_enabled": True,
        "agent_version": build.version,
        "agent_binaries": {arch: str(build.binary_path(arch)) for arch in ARCHITECTURES},
        "agent_config_json": frappe.as_json(config),
        "agent_systemd_unit": systemd_unit(),
        "agent_restart": restart,
        "__secret_values__": agent_secrets(config),
    }


def agent_secrets(config: dict) -> list[str]:
    """Every secret in an agent's config.json, so job output and previews can mask them."""

    secrets = [config["suite_cloud"]["secret"]]
    if "stalwart" in config["metrics"]:
        secrets.append(config["metrics"]["stalwart"]["password"])
    if "quota" in config:
        secrets += [config["quota"]["hook_token"], *quota.store_secrets(config["quota"]["store"])]
    return [secret for secret in secrets if secret]


def masked_agent_config(server: Document) -> dict:
    """config.json as the next Install Agent writes it to the server, every secret masked."""

    config = agent_config(server)
    return mask_config(config, secret_spellings(agent_secrets(config)))


def mask_config(value, spellings: list[str]):
    """Masks every secret wherever it appears, a Redis URL holding a password included."""

    if isinstance(value, dict):
        return {key: mask_config(item, spellings) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_config(item, spellings) for item in value]
    if isinstance(value, str):
        return mask_secrets(value, spellings)
    return value


def install_agent(server: Document) -> Document:
    """A Server Job that installs the agent only: no drain, no Stalwart restart. The binary, the
    config and the unit are rewritten where they differ and the agent restarts in any case."""

    if not is_telemetry_enabled():
        frappe.throw(_("Turn on Enable Telemetry in Suite Cloud Settings first."))
    build = AgentBuild.last_built()
    if not build or not build.is_built:
        frappe.throw(_("No agent build on this bench host; run Build Agent in Suite Cloud Settings first."))
    return agent_job(server, build)


def agent_job(server: Document, build: AgentBuild | None) -> Document:
    """Without a build the job runs the play with the secret gone, which stops the agent."""

    is_gateway = server.doctype == "Egress Gateway"
    title = (
        f"Install agent {build.version} on {server.hostname}" if build else f"Stop agent on {server.hostname}"
    )
    return create_server_job(
        server,
        PLAYBOOK,
        title=title,
        context={"gateway" if is_gateway else "node": server.name, "agent_restart": True},
        variables_builder=GATEWAY_BUILDER if is_gateway else NODE_BUILDER,
    )


def upgrade_agents(cluster: Document) -> list[str]:
    """Rolls the built agent out to every server of the cluster that runs an agent."""

    return [install_agent(server).name for server in agent_servers(cluster)]


def agent_servers(cluster: Document | None = None) -> list[Document]:
    """The servers that run an agent, in one cluster or in every cluster: Active and Draining
    nodes (a draining node still ships its log) and Active gateways."""

    nodes = frappe.get_all("Stalwart Node", filters_for(cluster, ("Active", "Draining")), pluck="name")
    gateways = frappe.get_all("Egress Gateway", filters_for(cluster, ("Active",)), pluck="name")
    return [
        *(frappe.get_doc("Stalwart Node", name) for name in nodes),
        *(frappe.get_doc("Egress Gateway", name) for name in gateways),
    ]


def filters_for(cluster: Document | None, statuses: tuple[str, ...]) -> dict:
    filters = {"status": ("in", statuses)}
    if cluster:
        filters["cluster"] = cluster.name
    return filters


def switch_off_quota_hooks() -> None:
    """With telemetry off the Agents stop, so each active cluster's config is pushed again: its
    plan then holds the hook switched off, and Stalwart stops calling Agents that are gone."""

    for name in frappe.get_all("Stalwart Cluster", {"status": "Active"}, pluck="name"):
        egress.resync_cluster_after_commit(name)


def enqueue_roll_out_agents() -> None:
    """Enable Telemetry changed. In the background, since the roll-out loads every server of every
    cluster and creates a job for each. Queued even where jobs otherwise run at once
    (``do_not_enqueue``): it reaches every server of the site, so a test runs it only by name."""

    frappe.enqueue(roll_out_agents, queue="long", enqueue_after_commit=True)


def roll_out_agents() -> list[str]:
    """Each turn of the setting queues a roll-out, and each reads the setting when it runs. They run
    one at a time, and one that finds the settings as the last roll-out left them does nothing, so
    quick turns end in the last one without a second job per server."""

    lock = f"{frappe.local.site}:suite_cloud:roll-out-agents"
    with frappe.cache.lock(lock, timeout=ROLL_OUT_TIMEOUT, blocking_timeout=ROLL_OUT_TIMEOUT):
        change = str(frappe.db.get_single_value("Suite Cloud Settings", "modified"))
        if frappe.cache.get_value(ROLL_OUT_KEY) == change:
            return []
        names = start_agent_jobs()
        if not frappe.in_test:
            frappe.db.commit()  # the jobs exist before the change is marked as rolled out
        frappe.cache.set_value(ROLL_OUT_KEY, change)
    return names


def start_agent_jobs() -> list[str]:
    """Every server that runs an agent gets it installed, or stopped while telemetry is off. A
    missing build is an Error Log, not an exception: the setting is already saved."""

    enabled = is_telemetry_enabled()
    if not enabled:
        switch_off_quota_hooks()
    servers = agent_servers()
    if not servers:
        return []
    build = None
    if enabled:
        build = AgentBuild.last_built()
        if not build or not build.is_built:
            log_error(
                "Agent not built",
                "Telemetry was turned on but no agent build exists; run Build Agent, then Upgrade Agents on every cluster.",
            )
            return []
    return [agent_job(server, build).name for server in servers]
