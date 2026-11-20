"""The outbound spam scan on the cluster's side: the MtaMilter that sends each signed-in sender's
message to the node's Rspamd at DATA, and the Memory Lookup Keys that name the trusted accounts it
skips. Mail Account owns which accounts are trusted; the keys are a copy."""

from typing import TYPE_CHECKING

import frappe

from suite_cloud.mail.cluster import rspamd
from suite_cloud.mail.cluster.lookup_keys import create_key, delete_key
from suite_cloud.mail.cluster.plan import as_list, as_set
from suite_cloud.mail.cluster.quota import NEVER
from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

    from suite_cloud.mail.stalwart.client import StalwartClient

EXEMPT_NAMESPACE = "suite-cloud-outbound-scan-exempt"
LOOKUP_KEY = "MemoryLookupKey"
SCANNED = {
    "match": as_list(
        [
            {
                "if": f"!is_empty(authenticated_as) && !key_exists('{EXEMPT_NAMESPACE}', authenticated_as)",
                "then": "true",
            }
        ]
    ),
    "else": "false",
}


def cluster_operations(cluster: Document) -> list[dict]:
    return [milter_operation(cluster)]


def milter_operation(cluster: Document) -> dict:
    """Stalwart defers the message with a 451 when Rspamd fails or is slow, so no signed-in sender's
    mail leaves without the scan. Off until every running node has its scanner. MtaMilter has no
    description, so the plan matches it on its address."""

    milter = {
        "enable": SCANNED if is_scan_enabled(cluster) else NEVER,
        "hostname": rspamd.MILTER_HOST,
        "port": rspamd.MILTER_PORT,
        "stages": as_set(["data"]),
        "tempFailOnError": True,
        "timeoutConnect": 1000,
        "timeoutCommand": 5000,
        "timeoutData": 12000,  # longer than Rspamd's task_timeout, so Rspamd sends its own reply
        "protocolVersion": "v6",
        "useTls": False,
        "maxResponseSize": 52428800,
    }
    return {
        "@type": "upsert",
        "object": "MtaMilter",
        "matchOn": ["hostname", "port"],
        "value": {"outbound-scan": milter},
    }


def is_scan_enabled(cluster: Document) -> bool:
    return get_config("outbound_scan_mode") != "Off" and rspamd.has_every_scanner(cluster)


# --- trusted accounts ------------------------------------------------------------------------


def set_exempt(cluster: Document, email: str, is_exempt: bool, key_id: str | None) -> str | None:
    """Adds or removes one account's key, and returns its id. Adding finds a key already there from
    Stalwart's refusal; removing goes by the id, and reads the namespace only for an account from
    before ids were kept. Stalwart reads keys only when it reloads its lookup stores, which
    ``ReloadSettings`` does not do."""

    client = cluster.get_client()
    service = client.objects(LOOKUP_KEY)
    if is_exempt:
        key_id, _is_new = create_key(service, lookup_key(email))
    else:
        if not (key_id := key_id or get_exempt_keys(client).get(email)):
            return None
        delete_key(service, key_id)
        key_id = None
    client.reload_lookup_stores()
    return key_id


def sync_exempt_accounts(cluster: Document) -> bool:
    """Makes the cluster's keys match the cluster's trusted accounts, and each account keep its
    key's id; True when it changed any key."""

    client = cluster.get_client()
    keys = get_exempt_keys(client)
    accounts = frappe.get_all(
        "Mail Account",
        {"cluster": cluster.name},
        ["name", "email", "skip_outbound_scan", "outbound_scan_key"],
    )
    wanted = {row.email for row in accounts if row.skip_outbound_scan}
    missing = sorted(wanted - set(keys))
    extra = [id for email, id in keys.items() if email not in wanted]
    service = client.objects(LOOKUP_KEY)
    ids = {email: keys[email] for email in wanted if email in keys}
    for email in missing:
        ids[email] = service.create(lookup_key(email))["id"]
    if extra:
        service.delete(extra)
    for row in accounts:
        if (row.outbound_scan_key or None) != ids.get(row.email):
            frappe.db.set_value(
                "Mail Account", row.name, "outbound_scan_key", ids.get(row.email), update_modified=False
            )
    if not (missing or extra):
        return False
    client.reload_lookup_stores()
    return True


def get_exempt_keys(client: StalwartClient) -> dict[str, str]:
    """``{address: key id}`` of the namespace's keys."""

    found = client.objects(LOOKUP_KEY).get_all(filter={"namespace": EXEMPT_NAMESPACE})
    return {key["key"]: key["id"] for key in found}


def lookup_key(email: str) -> dict:
    return {"namespace": EXEMPT_NAMESPACE, "key": email, "isGlobPattern": False}
