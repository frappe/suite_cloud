"""What provisioning puts on a node for the agent: its config.json, its unit and the Server Jobs
that install it."""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint, get_url

from suite_cloud.mail.cluster import plan
from suite_cloud.mail.stalwart import verify_tls
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.telemetry.agent.build import ARCHITECTURES, AgentBuild
from suite_cloud.utils import get_config, log_error

if TYPE_CHECKING:
    from frappe.model.document import Document

SOURCE = "stalwart"
CONFIG_PATH = "/etc/suite-cloud-agent/config.json"
STATE_DIR = "/var/lib/suite-cloud-agent"
PROMETHEUS_URL = "https://127.0.0.1:443/metrics/prometheus"
PLAYBOOK = "install-agent.yml"
NODE_BUILDER = "suite_cloud.mail.cluster.bootstrap.build_node_variables"
GATEWAY_BUILDER = "suite_cloud.mail.cluster.egress.build_gateway_variables"


def agent_config(server: Document) -> dict:
    """``config.json`` for a Stalwart Node or Egress Gateway. The Go side reads exactly this shape
    (``agent/internal/config``), so the two change together."""

    settings = frappe.get_cached_doc("Suite Cloud Settings")
    config = {
        "suite_cloud": {
            "url": get_config("public_url") or get_url(),
            "secret": settings.get_password("telemetry_secret", raise_exception=False),
            "requests_per_minute": cint(get_config("telemetry_requests_per_minute")),
            "records_per_request": cint(get_config("telemetry_records_per_request")),
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
    return config


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


def has_secret() -> bool:
    settings = frappe.get_cached_doc("Suite Cloud Settings")
    return bool(settings.get_password("telemetry_secret", raise_exception=False))


def agent_variables(server: Document, restart: bool = False) -> dict:
    """What install-agent.yml needs, merged into the node and gateway variables so the two never
    differ. Without a secret or a build the play stops the agent instead of installing it. With
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
    if not has_secret():
        return disabled
    build = AgentBuild.last_built()
    if not build or not build.is_built:
        log_error(
            "Agent not built",
            f"{server.hostname}: telemetry is on but no agent build exists; run Build Agent.",
        )
        return disabled

    config = agent_config(server)
    secrets = [config["suite_cloud"]["secret"]]
    if "stalwart" in config["metrics"]:
        secrets.append(config["metrics"]["stalwart"]["password"])
    return {
        "agent_enabled": True,
        "agent_version": build.version,
        "agent_binaries": {arch: str(build.binary_path(arch)) for arch in ARCHITECTURES},
        "agent_config_json": frappe.as_json(config),
        "agent_systemd_unit": systemd_unit(),
        "agent_restart": restart,
        "__secret_values__": secrets,
    }


def install_agent(server: Document) -> Document:
    """A Server Job that installs the agent only: no drain, no Stalwart restart. The binary, the
    config and the unit are rewritten where they differ and the agent restarts in any case."""

    if not has_secret():
        frappe.throw(_("Set the Telemetry Secret in Suite Cloud Settings first."))
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
    """Rolls the built agent out to every Active node and gateway of the cluster."""

    return [install_agent(server).name for server in active_servers(cluster)]


def active_servers(cluster: Document | None = None) -> list[Document]:
    """The Active nodes and gateways of one cluster, or of every cluster."""

    filters = {"status": "Active"}
    if cluster:
        filters["cluster"] = cluster.name
    nodes = frappe.get_all("Stalwart Node", filters, pluck="name")
    gateways = frappe.get_all("Egress Gateway", filters, pluck="name")
    return [
        *(frappe.get_doc("Stalwart Node", name) for name in nodes),
        *(frappe.get_doc("Egress Gateway", name) for name in gateways),
    ]


def roll_out_agents(secret_set: bool) -> list[str]:
    """After the Telemetry Secret changed: every Active node and gateway gets the new config, or
    its agent stopped when the secret was cleared. Runs inside the settings save, so a missing
    build is an Error Log, not an exception."""

    servers = active_servers()
    if not servers:
        return []
    build = None
    if secret_set:
        build = AgentBuild.last_built()
        if not build or not build.is_built:
            log_error(
                "Agent not built",
                "The Telemetry Secret changed but no agent build exists; run Build Agent, then Upgrade Agents on every cluster.",
            )
            return []
    return [agent_job(server, build).name for server in servers]
