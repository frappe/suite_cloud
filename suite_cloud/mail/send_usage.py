"""Send usage, for billing: Stalwart enforces each account's Mail Send Limit
(``suite_cloud.mail.cluster.quota``), and Suite Cloud counts what it accepted, from the queue
events a node's WebHook pushes, into Mail Send Usage."""

from collections import Counter
from datetime import UTC, datetime
from functools import cache
from typing import NamedTuple

import frappe
import redis

from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import add_usage, utc_date

QUEUED_EVENT = "queue.authenticated-message-queued"
SEEN_TTL = 3 * 86400  # longer than a WebHook keeps a batch it could not send


class QueuedMessage(NamedTuple):
    queue_id: str
    sender: str
    recipients: int
    queued_at: datetime


def record_queued(cluster: str, server: str, events: list[dict]) -> int:
    """Adds the recipients of each message the node queued for a signed-in sender to the sender's
    Mail Send Usage, and returns how many messages it counted. A message is counted once, even when
    a later batch brings its event again."""

    messages = [message for event in events if (message := parse_queued(event))]
    accounts = get_accounts_by_address(cluster, {message.sender for message in messages})
    messages = [message for message in messages if message.sender in accounts]
    fresh = mark_seen(server, messages)
    totals = Counter()
    for message in fresh:
        totals[(accounts[message.sender], utc_date(message.queued_at))] += message.recipients
    try:
        add_usage(totals)
    except Exception:
        forget_seen(server, fresh)  # so the retried batch counts them
        raise
    return len(fresh)


def parse_queued(event: dict) -> QueuedMessage | None:
    """``{type, createdAt, data: {queueId, from, to}}``; any other event is None."""

    data = event.get("data") or {}
    if event.get("type") != QUEUED_EVENT or not data.get("queueId"):
        return None
    to = data.get("to") or []
    return QueuedMessage(
        queue_id=str(data["queueId"]),
        sender=str(data.get("from") or "").strip().lower(),
        recipients=len(to) if isinstance(to, list) else 1,
        queued_at=datetime.fromisoformat(event["createdAt"]).astimezone(UTC),
    )


def get_accounts_by_address(cluster: str, addresses: set[str]) -> dict[str, str]:
    """``{address: account}`` for the addresses that are an account of the cluster or an alias of
    one. Nodes make a signed-in sender use one of its own addresses."""

    if not addresses:
        return {}
    owners = {address: address for address in addresses}
    aliases = frappe.get_all(
        "Mail Address Alias",
        filters={"parenttype": "Mail Account", "alias_email": ("in", list(addresses))},
        fields=["alias_email", "parent"],
    )
    owners.update({row.alias_email: row.parent for row in aliases})
    accounts = set(
        frappe.get_all(
            "Mail Account", {"cluster": cluster, "name": ("in", list(set(owners.values())))}, pluck="name"
        )
    )
    return {address: account for address, account in owners.items() if account in accounts}


def mark_seen(server: str, messages: list[QueuedMessage]) -> list[QueuedMessage]:
    """The messages no earlier batch counted, marked as counted now."""

    client = get_client()
    with client.pipeline(transaction=False) as pipe:
        for message in messages:
            pipe.set(seen_key(server, message), 1, nx=True, ex=SEEN_TTL)
        is_new = pipe.execute()
    return [message for message, new in zip(messages, is_new, strict=True) if new]


def forget_seen(server: str, messages: list[QueuedMessage]) -> None:
    if messages:
        get_client().delete(*(seen_key(server, message) for message in messages))


def seen_key(server: str, message: QueuedMessage) -> bytes:
    return frappe.cache.make_key(f"send_usage:queued:{server}:{message.queue_id}")


def get_client() -> redis.Redis:
    """The cache's connections, without Frappe's pickling wrapper."""

    return get_pool_client(frappe.cache.connection_pool)


@cache
def get_pool_client(pool: redis.ConnectionPool) -> redis.Redis:
    return redis.Redis(connection_pool=pool)
