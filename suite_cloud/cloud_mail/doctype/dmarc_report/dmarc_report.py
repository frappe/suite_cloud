# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""DMARC aggregate reports, copied from the clusters.

Stalwart intercepts the reports other receivers mail to ``postmaster@<domain>``, parses them and
keeps them for a month. The hourly fetch copies the ones it has not seen into these documents,
attributed to the site that holds the domain, so a site keeps a history longer than the cluster
does and reads it through the site API without ever touching the cluster.
"""

import json
from datetime import UTC
from uuid import uuid7
from zoneinfo import ZoneInfo

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, get_datetime, get_system_timezone, now_datetime

from suite_cloud.cloud_mail.stalwart import get_client, has_credentials
from suite_cloud.utils import get_config, log_exception, utc_iso

PASS = "pass"


class DMARCReport(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.cloud_mail.doctype.dmarc_report_record.dmarc_report_record import (
            DMARCReportRecord,
        )

        adkim: DF.Data | None
        aspf: DF.Data | None
        cluster: DF.Link
        date_range_begin: DF.Datetime | None
        date_range_end: DF.Datetime | None
        dkim_passed_messages: DF.Int
        errors: DF.SmallText | None
        expires_at: DF.Datetime | None
        extra_contact_info: DF.SmallText | None
        failed_messages: DF.Int
        org_name: DF.Data
        passed_messages: DF.Int
        policy: DF.Data | None
        policy_domain: DF.Data
        received_at: DF.Datetime | None
        records: DF.Table[DMARCReportRecord]
        report: DF.JSON | None
        report_id: DF.Data | None
        report_version: DF.Float
        reporter_email: DF.Data | None
        sent_to: DF.SmallText | None
        site: DF.Link | None
        spf_passed_messages: DF.Int
        stalwart_id: DF.Data
        subdomain_policy: DF.Data | None
        subject: DF.SmallText | None
        testing_mode: DF.Check
        total_messages: DF.Int
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    @classmethod
    def from_stalwart(cls, cluster_name: str, obj: dict) -> DMARCReport:
        """Builds the document for one ``DmarcExternalReport`` object (nothing is saved)."""

        report = obj.get("report") or {}
        policy_domain = normalize_domain(report.get("policyDomain"))
        # A Mail Domain is named by its domain, so the report's own domain says who holds it.
        site = frappe.db.get_value("Mail Domain", policy_domain, "site") if policy_domain else None
        records = [record_row(r) for r in as_list(report.get("records"))]
        doc = frappe.new_doc("DMARC Report")
        doc.update(
            {
                "cluster": cluster_name,
                "stalwart_id": obj["id"],
                "policy_domain": policy_domain,
                "site": site,
                "org_name": report.get("orgName") or sender_address(obj.get("from")) or "unknown",
                "reporter_email": report.get("email") or sender_address(obj.get("from")),
                "extra_contact_info": report.get("extraContactInfo"),
                "report_id": report.get("reportId"),
                "report_version": flt(report.get("version")),
                "subject": obj.get("subject"),
                "sent_to": "\n".join(as_list(obj.get("to"))) or None,
                "date_range_begin": local_datetime(report.get("dateRangeBegin")),
                "date_range_end": local_datetime(report.get("dateRangeEnd")),
                "received_at": local_datetime(obj.get("receivedAt")),
                "expires_at": local_datetime(obj.get("expiresAt")),
                "policy": report.get("policyDisposition"),
                "subdomain_policy": report.get("policySubdomainDisposition"),
                "testing_mode": int(bool(report.get("policyTestingMode"))),
                "adkim": report.get("policyAdkim"),
                "aspf": report.get("policyAspf"),
                "errors": "\n".join(str(e) for e in as_list(report.get("errors"))) or None,
                "report": json.dumps(obj),
                **totals(records),
            }
        )
        doc.set("records", records)
        return doc

    def to_api(self, with_records: bool = False) -> dict:
        payload = report_payload(self)
        if with_records:
            payload["records"] = [record_payload(r) for r in self.records]
        return payload


def on_doctype_update() -> None:
    # A report is one object on one cluster; the pair is what the fetch dedups on, and two runs
    # racing each other must not store it twice.
    frappe.db.add_unique("DMARC Report", ["cluster", "stalwart_id"])


# --- scheduled ------------------------------------------------------------------------


def fetch_all_clusters() -> None:
    """Hourly: copies the reports each active cluster holds that are not stored yet.

    One transaction per cluster: a cluster that fails rolls its own work back and is logged,
    while the ones already fetched stay committed.
    """

    clusters = frappe.get_all("Stalwart Cluster", {"enabled": 1, "status": "Active"}, pluck="name")
    for name in clusters:
        cluster = frappe.get_cached_doc("Stalwart Cluster", name)
        if not has_credentials(cluster):
            continue
        try:
            fetch_reports(cluster)
        except Exception:
            frappe.db.rollback()
            log_exception(f"DMARC report fetch failed for cluster {name}")
            continue
        if not frappe.in_test:
            frappe.db.commit()


def fetch_reports(cluster: Document) -> int:
    """Stores the cluster's reports that are new here; returns how many were added.

    Ids are the only thing asked of the cluster up front, so an hourly run on a cluster with
    nothing new costs one query. The caller owns the transaction; a malformed report is rolled
    back to its savepoint, logged and skipped without losing the rest.
    """

    service = get_client(cluster).dmarc_reports
    stored = set(frappe.get_all("DMARC Report", {"cluster": cluster.name}, pluck="stalwart_id"))
    # Deduplicated: an id the cluster lists twice must be stored once, not logged as a failure.
    new_ids = list(dict.fromkeys(id for id in service.iter_ids() if id not in stored))
    added = 0
    for obj in service.get_many(new_ids):
        frappe.db.savepoint(SAVEPOINT)
        try:
            DMARCReport.from_stalwart(cluster.name, obj).insert(ignore_permissions=True)
        except Exception:
            frappe.db.rollback(save_point=SAVEPOINT)
            log_exception(f"DMARC report {obj.get('id')} on {cluster.name} could not be stored")
            continue
        added += 1
    return added


SAVEPOINT = "dmarc_report"


def prune_expired_reports() -> None:
    """Daily: drops reports whose period ended longer ago than the configured retention.

    Only once the cluster has dropped its copy too: a report deleted here while Stalwart still
    lists it would look new to the next fetch and come straight back.
    """

    days = retention_days()
    expired = {"date_range_end": ["<", add_days(now_datetime(), -days)]}
    names = frappe.get_all("DMARC Report", {**expired, "expires_at": ["<", now_datetime()]}, pluck="name")
    names += frappe.get_all("DMARC Report", {**expired, "expires_at": ["is", "not set"]}, pluck="name")
    delete_reports(names)


DEFAULT_RETENTION_DAYS = 90


def retention_days() -> int:
    """The configured retention; a missing or negative value falls back to the default.

    Settings refuse a value under one day, but site_config is not validated, and a negative
    number would move the cutoff into the future and delete the whole history.
    """

    days = cint(get_config("dmarc_report_retention_days"))
    return days if days > 0 else DEFAULT_RETENTION_DAYS


def detach_reports_for_domain(domain: str) -> None:
    """Called when a Mail Domain goes: its history must not surface for whoever adds it next.

    The reports stay, unattributed, rather than being deleted: the cluster may still list them,
    and a deleted report would be fetched again and attributed to the domain's next holder.
    """

    frappe.db.set_value("DMARC Report", {"policy_domain": domain, "site": ["is", "set"]}, "site", None)


def delete_reports(names: list[str]) -> None:
    if not names:
        return
    frappe.db.delete("DMARC Report Record", {"parent": ["in", names], "parenttype": "DMARC Report"})
    frappe.db.delete("DMARC Report", {"name": ["in", names]})


# --- payloads -------------------------------------------------------------------------


def report_payload(row) -> dict:
    return {
        "name": row.name,
        "policy_domain": row.policy_domain,
        "reporter": row.org_name,
        "reporter_email": row.reporter_email,
        "report_id": row.report_id,
        "version": flt(row.report_version),
        "subject": row.subject,
        "to": (row.sent_to or "").split("\n") if row.sent_to else [],
        "date_range_begin": utc_iso(row.date_range_begin),
        "date_range_end": utc_iso(row.date_range_end),
        "received_at": utc_iso(row.received_at),
        "policy": {
            "p": row.policy,
            "sp": row.subdomain_policy,
            "testing_mode": bool(row.testing_mode),
            "adkim": row.adkim,
            "aspf": row.aspf,
        },
        "totals": {
            "messages": cint(row.total_messages),
            "passed": cint(row.passed_messages),
            "failed": cint(row.failed_messages),
            "dkim_passed": cint(row.dkim_passed_messages),
            "spf_passed": cint(row.spf_passed_messages),
        },
        "errors": row.errors,
    }


REPORT_FIELDS = [
    "name",
    "policy_domain",
    "org_name",
    "reporter_email",
    "report_id",
    "report_version",
    "subject",
    "sent_to",
    "date_range_begin",
    "date_range_end",
    "received_at",
    "policy",
    "subdomain_policy",
    "testing_mode",
    "adkim",
    "aspf",
    "total_messages",
    "passed_messages",
    "failed_messages",
    "dkim_passed_messages",
    "spf_passed_messages",
    "errors",
]


def report_payloads(names: list[str]) -> list[dict]:
    """The listing shape of many reports in one query, in the order of ``names``."""

    if not names:
        return []
    rows = frappe.get_all("DMARC Report", filters={"name": ["in", names]}, fields=REPORT_FIELDS)
    by_name = {row.name: row for row in rows}
    return [report_payload(by_name[n]) for n in names if n in by_name]


def record_payload(row) -> dict:
    return {
        "source_ip": row.source_ip,
        "count": cint(row.message_count),
        "disposition": row.disposition,
        "dkim": row.dkim,
        "spf": row.spf,
        "header_from": row.header_from,
        "envelope_from": row.envelope_from,
        "envelope_to": row.envelope_to,
        "override_reasons": row.override_reasons,
        "dkim_results": frappe.parse_json(row.dkim_results) or [],
        "spf_results": frappe.parse_json(row.spf_results) or [],
    }


# --- parsing ----------------------------------------------------------------------------


def record_row(record: dict) -> dict:
    reasons = [
        f"{r.get('overrideType') or ''}: {r.get('comment') or ''}".strip(": ")
        for r in as_list(record.get("policyOverrideReasons"))
    ]
    return {
        "source_ip": record.get("sourceIp"),
        "message_count": cint(record.get("count")),
        "disposition": lower(record.get("evaluatedDisposition")),
        "dkim": lower(record.get("evaluatedDkim")),
        "spf": lower(record.get("evaluatedSpf")),
        "header_from": record.get("headerFrom"),
        "envelope_from": record.get("envelopeFrom"),
        "envelope_to": record.get("envelopeTo"),
        "override_reasons": "\n".join(r for r in reasons if r) or None,
        "dkim_results": json.dumps(as_list(record.get("dkimResults"))),
        "spf_results": json.dumps(as_list(record.get("spfResults"))),
    }


def totals(records: list[dict]) -> dict:
    """A message passes DMARC when either aligned check does; the counts are per source row."""

    counts = {"total_messages": 0, "passed_messages": 0, "dkim_passed_messages": 0, "spf_passed_messages": 0}
    for record in records:
        count = record["message_count"]
        dkim, spf = record["dkim"] == PASS, record["spf"] == PASS
        counts["total_messages"] += count
        counts["passed_messages"] += count if (dkim or spf) else 0
        counts["dkim_passed_messages"] += count if dkim else 0
        counts["spf_passed_messages"] += count if spf else 0
    counts["failed_messages"] = counts["total_messages"] - counts["passed_messages"]
    return counts


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
