"""Reports other mail systems send about the clusters' domains, copied into Suite Cloud.

Stalwart intercepts the DMARC and TLS aggregate reports mailed to ``postmaster@<domain>``,
parses them and keeps them for a month. The hourly fetch copies the ones it has not seen into a
DocType per kind, attributed to the site that holds the domain, so a site keeps a history longer
than the cluster does and reads it through the site API without ever touching the cluster.
"""

import json
from dataclasses import dataclass
from datetime import UTC
from zoneinfo import ZoneInfo

import frappe
from frappe.model.base_document import get_controller
from frappe.model.document import Document
from frappe.utils import add_days, cint, get_datetime, get_system_timezone, now_datetime

from suite_cloud.cloud_mail.stalwart import get_client, has_credentials
from suite_cloud.utils import get_config, log_exception, utc_iso

DEFAULT_RETENTION_DAYS = 90


@dataclass(frozen=True)
class ReceivedReports:
    """How one kind of report is copied from the clusters, kept and let go.

    The DocType builds a document from one Stalwart object with ``from_stalwart(cluster, obj)``.
    """

    doctype: str
    child_doctypes: tuple[str, ...]
    service: str  # the StalwartClient attribute that lists the objects
    retention_key: str  # the Suite Cloud config key that holds the retention in days

    def fetch_all_clusters(self) -> None:
        """Copies the reports each active cluster holds that are not stored yet.

        One transaction per cluster: a cluster that fails rolls its own work back and is logged,
        while the ones already fetched stay committed.
        """

        clusters = frappe.get_all("Stalwart Cluster", {"enabled": 1, "status": "Active"}, pluck="name")
        for name in clusters:
            cluster = frappe.get_cached_doc("Stalwart Cluster", name)
            if not has_credentials(cluster):
                continue
            try:
                self.fetch(cluster)
            except Exception:
                frappe.db.rollback()
                log_exception(f"{self.doctype} fetch failed for cluster {name}")
                continue
            if not frappe.in_test:
                frappe.db.commit()

    def fetch(self, cluster: Document) -> int:
        """Stores the cluster's reports that are new here; returns how many were added.

        Ids are the only thing asked of the cluster up front, so an hourly run on a cluster with
        nothing new costs one query. The caller owns the transaction; a malformed report is rolled
        back to its savepoint, logged and skipped without losing the rest.
        """

        service = getattr(get_client(cluster), self.service)
        stored = set(frappe.get_all(self.doctype, {"cluster": cluster.name}, pluck="stalwart_id"))
        # Deduplicated: an id the cluster lists twice must be stored once, not logged as a failure.
        new_ids = list(dict.fromkeys(id for id in service.iter_ids() if id not in stored))
        controller = get_controller(self.doctype)
        savepoint = frappe.scrub(self.doctype)
        added = 0
        for obj in service.get_many(new_ids):
            frappe.db.savepoint(savepoint)
            try:
                controller.from_stalwart(cluster.name, obj).insert(ignore_permissions=True)
            except Exception:
                frappe.db.rollback(save_point=savepoint)
                log_exception(f"{self.doctype} {obj.get('id')} on {cluster.name} could not be stored")
                continue
            added += 1
        return added

    def prune_expired(self) -> None:
        """Drops reports whose period ended longer ago than the configured retention.

        Only once the cluster has dropped its copy too: a report deleted here while Stalwart still
        lists it would look new to the next fetch and come straight back.
        """

        expired = {"date_range_end": ["<", add_days(now_datetime(), -self.retention_days())]}
        names = frappe.get_all(self.doctype, {**expired, "expires_at": ["<", now_datetime()]}, pluck="name")
        names += frappe.get_all(self.doctype, {**expired, "expires_at": ["is", "not set"]}, pluck="name")
        self.delete(names)

    def retention_days(self) -> int:
        """The configured retention; a missing or negative value falls back to the default.

        Settings refuse a value under one day, but site_config is not validated, and a negative
        number would move the cutoff into the future and delete the whole history.
        """

        days = cint(get_config(self.retention_key))
        return days if days > 0 else DEFAULT_RETENTION_DAYS

    def detach_domain(self, domain: str) -> None:
        """Called when a Mail Domain goes: its history must not surface for whoever adds it next.

        The reports stay, unattributed, rather than being deleted: the cluster may still list them,
        and a deleted report would be fetched again and attributed to the domain's next holder.
        """

        frappe.db.set_value(self.doctype, {"policy_domain": domain, "site": ["is", "set"]}, "site", None)

    def delete(self, names: list[str]) -> None:
        if not names:
            return
        for child in self.child_doctypes:
            frappe.db.delete(child, {"parent": ["in", names], "parenttype": self.doctype})
        frappe.db.delete(self.doctype, {"name": ["in", names]})


def envelope_fields(cluster_name: str, obj: dict, policy_domain: str) -> dict:
    """The fields every received report shares: where it came from, whose domain it is about,
    the message that carried it and the whole object as the cluster answered it."""

    return {
        "cluster": cluster_name,
        "stalwart_id": obj["id"],
        "policy_domain": policy_domain,
        # A Mail Domain is named by its domain, so the report's own domain says who holds it.
        "site": frappe.db.get_value("Mail Domain", policy_domain, "site") if policy_domain else None,
        "subject": obj.get("subject"),
        "sent_to": "\n".join(as_list(obj.get("to"))) or None,
        "received_at": local_datetime(obj.get("receivedAt")),
        "expires_at": local_datetime(obj.get("expiresAt")),
        "report": json.dumps(obj),
    }


def envelope_payload(row) -> dict:
    """The API fields every received report shares; ``row`` is a document or a listing row."""

    return {
        "name": row.name,
        "policy_domain": row.policy_domain,
        "reporter": row.org_name,
        "reporter_email": row.reporter_email,
        "report_id": row.report_id,
        "subject": row.subject,
        "to": row.sent_to.split("\n") if row.sent_to else [],
        "date_range_begin": utc_iso(row.date_range_begin),
        "date_range_end": utc_iso(row.date_range_end),
        "received_at": utc_iso(row.received_at),
    }


def listing_payloads(doctype: str, names: list[str], fields: list[str], build) -> list[dict]:
    """The listing shape of many reports in one query, in the order of ``names``."""

    if not names:
        return []
    rows = frappe.get_all(doctype, filters={"name": ["in", names]}, fields=fields)
    by_name = {row.name: row for row in rows}
    return [build(by_name[n]) for n in names if n in by_name]


def report_window(days) -> tuple:
    """``(since, until)``: the last ``days`` days up to now; ``since`` is None for 0 or unset,
    which means everything the retention still holds."""

    until = now_datetime()
    days = cint(days)
    return (add_days(until, -days) if days > 0 else None), until


# --- parsing ----------------------------------------------------------------------------


def as_list(value) -> list:
    """Stalwart encodes lists as ``{"0": item, "1": item}`` and sets as ``{item: true}``; a JSON
    list is accepted as well."""

    if isinstance(value, dict):
        if all(v is True for v in value.values()):
            return list(value)
        return [value[k] for k in sorted(value, key=lambda k: cint(k))]
    return list(value or [])


def sender_address(value) -> str | None:
    if isinstance(value, dict):
        return value.get("email") or value.get("name")
    return value or None


def normalize_domain(value) -> str:
    return (value or "").strip().lower().rstrip(".")


def lower(value) -> str | None:
    return str(value).lower() if value not in (None, "") else None


def local_datetime(value):
    """A UTC timestamp from Stalwart as the naive system-time value Frappe stores."""

    if not value:
        return None
    moment = get_datetime(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
