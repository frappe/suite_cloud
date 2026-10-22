"""Where the agents stand: the telemetry API notes the last batch each host sent, and the node,
gateway and cluster forms show it beside the build the bench host holds."""

import re
from datetime import timedelta
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, pretty_date

from suite_cloud.telemetry.agent.build import AgentBuild
from suite_cloud.telemetry.agent.deploy import active_servers
from suite_cloud.telemetry.alerts import DEFAULT_SILENCE_MINUTES
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

CACHE_KEY = "suite_cloud_agent_contact"
USER_AGENT = re.compile(r"suite-cloud-agent/(\S+)")
SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")
STATES = ("silent", "outdated", "unknown", "ok")  # worst first


def record_contact(host_ip: str, user_agent: str | None) -> None:
    """Called by the API for every accepted batch; the cache costs one write, the database would
    cost a row update per request."""

    match = USER_AGENT.search(user_agent or "")
    contact = {"seen_at": now_datetime().isoformat(), "version": match.group(1) if match else ""}
    frappe.cache.hset(CACHE_KEY, host_ip, contact)


def forget_contacts() -> None:
    frappe.cache.delete_value(CACHE_KEY)


def agent_status(server: Document) -> dict:
    """``state`` is ``ok``, ``outdated`` (not the current build), ``silent`` (no batch within the
    silence window of the alerts) or ``unknown`` (no batch recorded); ``message`` says so."""

    contact = frappe.cache.hget(CACHE_KEY, server.ipv4_address) or {}
    build = AgentBuild.last_built()
    built = build.version if build else ""
    status = {"hostname": server.hostname, "version": contact.get("version", ""), "built_version": built}
    if not contact:
        status["state"] = "unknown"
        status["message"] = _("The agent has not sent a batch yet.")
        return status

    seen = get_datetime(contact["seen_at"])
    status["seen_at"] = seen
    silence = timedelta(minutes=cint(get_config("telemetry_silence_minutes")) or DEFAULT_SILENCE_MINUTES)
    version = status["version"] or _("of unknown version")
    if now_datetime() - seen > silence:
        status["state"] = "silent"
        status["message"] = _(
            "Agent {0} has sent nothing since {1}. Check journalctl -u suite-cloud-agent on the node."
        ).format(version, pretty_date(seen))
    elif built and status["version"] != built:
        status["state"] = "outdated"
        status["message"] = _(
            "Agent {0} sent its last batch {1}; the current build is {2}. Install Agent updates it."
        ).format(version, pretty_date(seen), built)
    else:
        status["state"] = "ok"
        status["message"] = _("Agent {0} sent its last batch {1}.").format(version, pretty_date(seen))
    return status


def agent_summary(cluster: Document) -> dict:
    """One line for the cluster form over its Active nodes and gateways; ``state`` is the worst."""

    statuses = [agent_status(server) for server in active_servers(cluster)]
    if not statuses:
        return {"state": "ok", "message": "", "statuses": []}

    by_state = {state: [s["hostname"] for s in statuses if s["state"] == state] for state in STATES}
    parts = []
    if by_state["ok"]:
        parts.append(_("{0} current").format(len(by_state["ok"])))
    for state, label in (
        ("outdated", _("{0} outdated ({1})")),
        ("silent", _("{0} silent ({1})")),
        ("unknown", _("{0} without a batch yet ({1})")),
    ):
        if by_state[state]:
            parts.append(label.format(len(by_state[state]), ", ".join(by_state[state])))
    worst = next(state for state in STATES if by_state[state])
    return {"state": worst, "message": _("Agents: {0}.").format(", ".join(parts)), "statuses": statuses}


@frappe.whitelist()
def get_agent_status(doctype: str, name: str) -> dict:
    if doctype not in SERVER_DOCTYPES:
        frappe.throw(_("Not a server doctype: {0}").format(doctype), frappe.ValidationError)
    server = frappe.get_doc(doctype, name)
    server.check_permission("read")
    return agent_status(server)


@frappe.whitelist()
def get_agent_summary(cluster: str) -> dict:
    doc = frappe.get_doc("Stalwart Cluster", cluster)
    doc.check_permission("read")
    return agent_summary(doc)
