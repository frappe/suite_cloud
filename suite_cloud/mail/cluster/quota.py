"""The sending limits on the cluster's side: the MTA Hook that asks each node's Agent before a
signed-in sender's message is accepted, a native throttle that caps every account while an Agent
is down, and the counter store the Agents share.

The hook and the Agents' ``quota`` config go out together, and only once the last agent build
answers the hook (``build.is_quota_enabled``); until then the plan leaves both out.
"""

from typing import TYPE_CHECKING

from frappe.utils import cint

from suite_cloud.mail.cluster.plan import as_list, as_set, secret_value
from suite_cloud.mail.doctype.stalwart_store.stalwart_store import lines
from suite_cloud.telemetry.agent.build import is_quota_enabled

if TYPE_CHECKING:
    from frappe.model.document import Document

HOOK_PORT = 8095  # loopback only, the same on every node
HOOK_URL = f"http://127.0.0.1:{HOOK_PORT}/hooks/quota"
HOOK_TIMEOUT_MS = 2000
BACKSTOP_DESCRIPTION = "suite-cloud-quota-backstop"
DAY_MS = 86_400_000
SIGNED_IN = {"match": as_list([{"if": "!is_empty(authenticated_as)", "then": "true"}]), "else": "false"}
NEVER = {"match": as_list([]), "else": "false"}
REDIS_STORE_TYPES = {"Redis": "redis", "RedisCluster": "redis_cluster", "RedisSentinel": "redis_sentinel"}


def cluster_operations(cluster: Document) -> list[dict]:
    """The hook and the backstop, switched off while quota is: the plan only upserts, so leaving
    them out would keep Stalwart calling an Agent that no longer answers."""

    enabled = is_quota_enabled()
    return [hook_operation(cluster, enabled), backstop_operation(cluster, enabled)]


def hook_operation(cluster: Document, enabled: bool = True) -> dict:
    """At the data stage every recipient is known, and JMAP sends pass through it too. Stalwart
    accepts the message when the Agent fails or times out."""

    hook = {
        "enable": SIGNED_IN if enabled else NEVER,
        "url": HOOK_URL,
        "stages": as_set(["data"]),
        "timeout": HOOK_TIMEOUT_MS,
        "tempFailOnError": False,
        "httpAuth": {
            "@type": "Bearer",
            "bearerToken": secret_value(cluster.get_password("quota_hook_token")),
        },
    }
    # MtaHook has no description, so the URL identifies Suite Cloud's hook.
    return {"@type": "upsert", "object": "MtaHook", "matchOn": ["url"], "value": {"quota": hook}}


def backstop_operation(cluster: Document, enabled: bool = True) -> dict:
    """The cluster's cap per signed-in account and UTC day, checked at MAIL FROM. Without
    ``match`` every unauthenticated session would share one bucket, since Stalwart hashes an
    empty login too."""

    throttle = {
        "enable": enabled,
        "description": BACKSTOP_DESCRIPTION,
        "key": as_set(["authenticatedAs"]),
        "match": SIGNED_IN,
        "rate": {"count": cint(cluster.quota_backstop_per_day), "period": DAY_MS},
    }
    return {
        "@type": "upsert",
        "object": "MtaInboundThrottle",
        "matchOn": ["description"],
        "value": {"quota-backstop": throttle},
    }


def store_config(cluster: Document) -> dict:
    """Where the Agents keep the counters: the cluster's Redis in-memory store, or each Agent's own
    memory when there is none, which only a single-node cluster can be."""

    store = cluster.get_store("in_memory_store")
    store_type = REDIS_STORE_TYPES.get(store.type) if store else None
    if not store_type:
        return {"type": "memory"}
    if store_type == "redis":
        return {"type": store_type, "urls": [store.url]}
    config = {
        "type": store_type,
        "urls": lines(store.urls),
        "username": store.auth_username or "",
        "password": store.get_password("auth_secret", raise_exception=False) or "",
    }
    if store_type == "redis_sentinel":
        config["service_name"] = store.service_name or "mymaster"
        config["sentinel_username"] = store.sentinel_username or ""
        config["sentinel_password"] = store.get_password("sentinel_secret", raise_exception=False) or ""
    return config


def store_secrets(store: dict) -> list[str]:
    """What masks the store in job output: its passwords, and its URLs, which may carry one."""

    found = [*store.get("urls", []), store.get("password"), store.get("sentinel_password")]
    return [secret for secret in found if secret]
