"""Stalwart's sending-usage WebHook: each node sends the messages it queued for signed-in senders,
and Suite Cloud adds their recipients to Mail Send Usage. Stalwart enforces the limits itself.

The bearer is the calling node's Suite Cloud token, which the ``authenticate`` auth hook checks; the
node's cluster is the caller's. It is accepted whether telemetry is on or not.
"""

import frappe

from suite_cloud.api.telemetry import hook_api
from suite_cloud.mail import send_usage


@frappe.whitelist(methods=["POST"])
@hook_api
def record(events: list) -> dict:
    """``{"events": [{id, createdAt, type, data}]}``. A failure answers 500, and Stalwart sends the
    batch again with the events that came after it."""

    server = frappe.local.stalwart_server
    return {"counted": send_usage.record_queued(server.cluster, server.name, events)}
