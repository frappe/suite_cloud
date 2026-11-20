"""The sending limits on the cluster's side. Stalwart enforces them: one inbound throttle per Mail
Send Limit of the cluster counts each signed-in account's recipients per UTC day, and a Memory
Lookup Key-Value names each account's limit. An account without a key gets the cluster's default. A
WebHook sends each queued message to Suite Cloud, which counts it. Mail Account owns the limit; the
keys are a copy."""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cint

from suite_cloud.mail.cluster import server_token
from suite_cloud.mail.cluster.plan import as_list, as_set
from suite_cloud.mail.cluster.telemetry import public_url
from suite_cloud.mail.send_usage import QUEUED_EVENT

if TYPE_CHECKING:
    from frappe.model.document import Document

    from suite_cloud.mail.stalwart.client import StalwartClient

LIMIT_NAMESPACE = "suite-cloud-send-limit"
LIMIT_PREFIX = "suite-cloud-send-limit-"
SUBMISSION_DESCRIPTION = "suite-cloud-submission-rate"
LOOKUP_KEY = "MemoryLookupKeyValue"
USAGE_METHOD = "/api/v2/method/suite_cloud.api.send_usage.record"
# WebHook has no description, and its URL follows the Public URL, so this header names it.
HOOK_HEADER = "X-Suite-Cloud-Hook"
USAGE_DISCARD_AFTER_MS = 24 * 3_600_000  # a lost batch is unbilled mail, so it waits out a long outage
USAGE_THROTTLE_MS = 30_000
USAGE_TIMEOUT_MS = 30_000
DAY_MS = 86_400_000
MINUTE_MS = 60_000
# Earlier releases asked Suite Cloud at RCPT; a sync removes the hook.
OLD_HOOK_HEADERS = {HOOK_HEADER: "sending-limits"}
NEVER = {"match": as_list([]), "else": "false"}


def cluster_operations(cluster: Document) -> list[dict]:
    return [throttle_operation(cluster), usage_webhook_operation(cluster)]


def throttle_operation(cluster: Document) -> dict:
    """One throttle per Mail Send Limit, checked at RCPT since its ``match`` reads ``rcpt``, plus
    the cap on messages per minute."""

    limits = get_cluster_limits(cluster.name)
    if not any(limit.is_default for limit in limits):
        frappe.throw(_("Cluster {0} has no default Mail Send Limit.").format(cluster.name))
    throttles = {limit_ref(limit): limit_throttle(limit) for limit in limits}
    throttles["submission-rate"] = submission_throttle(cluster)
    return throttles_operation(throttles)


def submission_throttle(cluster: Document) -> dict:
    """Messages per signed-in sender and minute, checked at MAIL FROM, whatever its limit."""

    return throttle(
        SUBMISSION_DESCRIPTION, "!is_empty(authenticated_as)", cint(cluster.submission_rate), MINUTE_MS
    )


def limit_throttle(limit: Document | dict) -> dict:
    """The default takes every signed-in account without a key; the others, the accounts whose key
    holds their limit."""

    value = cint(limit.daily_limit)
    if limit.is_default:
        condition = f"!is_empty(authenticated_as) && !is_empty(rcpt) && !key_exists('{LIMIT_NAMESPACE}', authenticated_as)"
    else:
        condition = f"!is_empty(rcpt) && key_get('{LIMIT_NAMESPACE}', authenticated_as) == '{value}'"
    return throttle(limit_description(limit), condition, value, DAY_MS)


def throttles_operation(throttles: dict[str, dict]) -> dict:
    return {"@type": "upsert", "object": "MtaInboundThrottle", "matchOn": ["description"], "value": throttles}


def throttle(description: str, condition: str, count: int, period: int) -> dict:
    """Keyed on the login. Without ``match`` every unauthenticated session would share one bucket,
    since Stalwart hashes an empty login too."""

    return {
        "enable": True,
        "description": description,
        "key": as_set(["authenticatedAs"]),
        "match": {"match": as_list([{"if": condition, "then": "true"}]), "else": "false"},
        "rate": {"count": count, "period": period},
    }


def usage_webhook_operation(cluster: Document) -> dict:
    """Off until every running node has its Suite Cloud token, as the reload needs it. Telemetry
    being off does not switch it off: it bills."""

    webhook = {
        "enable": server_token.has_every_token(cluster),
        "url": f"{public_url()}{USAGE_METHOD}",
        "level": "info",
        "events": as_set([QUEUED_EVENT]),
        "eventsPolicy": "include",
        "httpAuth": server_token.bearer_auth(),
        "httpHeaders": {HOOK_HEADER: "sending-usage"},
        "signatureKey": {"@type": "None"},
        "throttle": USAGE_THROTTLE_MS,
        "timeout": USAGE_TIMEOUT_MS,
        "discardAfter": USAGE_DISCARD_AFTER_MS,
        "lossy": False,
        "allowInvalidCerts": False,
    }
    return {
        "@type": "upsert",
        "object": "WebHook",
        "matchOn": ["httpHeaders"],
        "value": {"sending-usage": webhook},
    }


# --- after a push --------------------------------------------------------------------------------


def sync_send_limits(cluster: Document) -> None:
    """Leaves only the plan's throttles, one per limit and the submission rate: Stalwart's own
    defaults, the old backstop and those of deleted limits go. Stalwart adds its defaults again only
    on a start that finds no throttle. Removes the old hook, then makes the keys match the accounts.
    Runs after the plan is applied and before the reload."""

    client = cluster.get_client()
    wanted = {limit_description(limit) for limit in get_cluster_limits(cluster.name)} | {
        SUBMISSION_DESCRIPTION
    }
    stale = [
        item["id"]
        for item in client.objects("MtaInboundThrottle").get_all()
        if item.get("description") not in wanted
    ]
    if stale:
        client.objects("MtaInboundThrottle").delete(stale)
    old_hooks = [
        hook["id"]
        for hook in client.objects("MtaHook").get_all()
        if hook.get("httpHeaders") == OLD_HOOK_HEADERS
    ]
    if old_hooks:
        client.objects("MtaHook").delete(old_hooks)
    sync_limit_keys(cluster, client)


def sync_limit_keys(cluster: Document, client: StalwartClient) -> bool:
    """Makes the cluster's keys match its accounts' limits; True when it changed any."""

    keys = get_limit_keys(client)
    default = frappe.db.get_value("Mail Send Limit", {"cluster": cluster.name, "is_default": 1})
    wanted = {
        email: str(cint(daily_limit))
        for email, send_limit, daily_limit in frappe.get_all(
            "Mail Account",
            {"cluster": cluster.name},
            ["email", "send_limit", "daily_send_limit"],
            as_list=True,
        )
        if send_limit != default
    }
    missing = {email: value for email, value in wanted.items() if email not in keys}
    moved = {
        email: value for email, value in wanted.items() if email in keys and keys[email]["value"] != value
    }
    extra = [key["id"] for email, key in keys.items() if email not in wanted]
    service = client.objects(LOOKUP_KEY)
    for email, value in missing.items():
        service.create(limit_key(email, value))
    for email, value in moved.items():
        service.update(keys[email]["id"], {"value": value})
    if extra:
        service.delete(extra)
    if not (missing or moved or extra):
        return False
    client.reload_lookup_stores()
    return True


# --- one account -----------------------------------------------------------------------------------


def ensure_throttle(cluster: Document, limit: Document) -> bool:
    """Creates the limit's throttle when the cluster lacks it, as before a first account takes the
    limit: a key with no throttle of its own would leave the account unlimited. True when it did."""

    client = cluster.get_client()
    description = limit_description(limit)
    if any(item.get("description") == description for item in client.objects("MtaInboundThrottle").get_all()):
        return False
    client.apply([throttles_operation({limit_ref(limit): limit_throttle(limit)})])
    client.reload_settings()
    return True


def set_send_limit(cluster: Document, email: str, limit: int | None) -> None:
    """Writes one account's key; None, for the cluster's default or a deleted account, removes it.
    Stalwart reads keys only when it reloads its lookup stores, which ``ReloadSettings`` does not do."""

    client = cluster.get_client()
    current = get_limit_keys(client).get(email)
    value = str(limit) if limit else None
    service = client.objects(LOOKUP_KEY)
    if current and value is None:
        service.delete(current["id"])
    elif current and current["value"] != value:
        service.update(current["id"], {"value": value})
    elif not current and value:
        service.create(limit_key(email, value))
    else:
        return
    client.reload_lookup_stores()


def get_limit_keys(client: StalwartClient) -> dict[str, dict]:
    """``{address: {id, value}}`` of the namespace's keys."""

    found = client.objects(LOOKUP_KEY).get_all(filter={"namespace": LIMIT_NAMESPACE})
    return {key["key"]: {"id": key["id"], "value": key.get("value")} for key in found}


def limit_key(email: str, value: str) -> dict:
    return {"namespace": LIMIT_NAMESPACE, "key": email, "value": value, "isGlobPattern": False}


def get_cluster_limits(cluster: str) -> list[dict]:
    return frappe.get_all(
        "Mail Send Limit", {"cluster": cluster}, ["daily_limit", "is_default"], order_by="daily_limit"
    )


def limit_description(limit: Document | dict) -> str:
    return f"{LIMIT_PREFIX}{cint(limit.daily_limit)}"


def limit_ref(limit: Document | dict) -> str:
    return f"send-limit-{cint(limit.daily_limit)}"
