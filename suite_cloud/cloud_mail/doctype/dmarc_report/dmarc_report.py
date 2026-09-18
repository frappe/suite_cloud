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
from zoneinfo import ZoneInfo

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, get_datetime, get_system_timezone, now_datetime

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
        domain: DF.Link | None
        errors: DF.SmallText | None
        expires_at: DF.Datetime | None
        extra_contact_info: DF.Data | None
        failed_messages: DF.Int
        org_name: DF.Data
        passed_messages: DF.Int
        percentage: DF.Int
        policy: DF.Data | None
        policy_domain: DF.Data
        received_at: DF.Datetime | None
        records: DF.Table[DMARCReportRecord]
        report: DF.JSON | None
        report_id: DF.Data | None
        reporter_email: DF.Data | None
        site: DF.Link | None
        spf_passed_messages: DF.Int
        stalwart_id: DF.Data
        subdomain_policy: DF.Data | None
        total_messages: DF.Int
    # end: auto-generated types

    @classmethod
    def from_stalwart(cls, cluster_name: str, obj: dict) -> DMARCReport:
        """Builds the document for one ``DmarcExternalReport`` object (nothing is saved)."""

        report = obj.get("report") or {}
        policy_domain = normalize_domain(report.get("policyDomain"))
        owner = frappe.db.get_value("Mail Domain", policy_domain, ["name", "site"], as_dict=True)
        records = [record_row(r) for r in as_list(report.get("records"))]
        doc = frappe.new_doc("DMARC Report")
        doc.update(
            {
                "cluster": cluster_name,
                "stalwart_id": obj["id"],
                "policy_domain": policy_domain,
                "domain": owner.name if owner else None,
                "site": owner.site if owner else None,
                "org_name": report.get("orgName") or sender_address(obj.get("from")) or "unknown",
                "reporter_email": report.get("email") or sender_address(obj.get("from")),
                "extra_contact_info": report.get("extraContactInfo"),
                "report_id": report.get("reportId"),
                "date_range_begin": local_datetime(report.get("dateRangeBegin")),
                "date_range_end": local_datetime(report.get("dateRangeEnd")),
                "received_at": local_datetime(obj.get("receivedAt")),
                "expires_at": local_datetime(obj.get("expiresAt")),
                "policy": report.get("policyDisposition"),
                "subdomain_policy": report.get("policySubdomainDisposition"),
                "percentage": cint(report.get("policyTestingMode")),
                "adkim": report.get("policyAdkim"),
                "aspf": report.get("policyAspf"),
                "errors": "\n".join(str(e) for e in as_list(report.get("errors"))) or None,
                "report": json.dumps(report),
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
    """Hourly: copies the reports each active cluster holds that are not stored yet."""

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


def fetch_reports(cluster: Document) -> int:
    """Stores the cluster's reports that are new here; returns how many were added.

    Ids are the only thing asked of the cluster up front, so an hourly run on a cluster with
    nothing new costs one query. Each report is its own transaction: a malformed one is logged
    and skipped without losing the rest.
    """

    service = get_client(cluster).dmarc_reports
    stored = set(frappe.get_all("DMARC Report", {"cluster": cluster.name}, pluck="stalwart_id"))
    new_ids = [id for id in service.iter_ids() if id not in stored]
    added = 0
    for obj in service.get_many(new_ids):
        try:
            DMARCReport.from_stalwart(cluster.name, obj).insert(ignore_permissions=True)
        except Exception:
            frappe.db.rollback()
            log_exception(f"DMARC report {obj.get('id')} on {cluster.name} could not be stored")
            continue
        added += 1
        if not frappe.in_test:
            frappe.db.commit()
    return added


def prune_expired_reports() -> None:
    """Daily: drops reports whose period ended longer ago than the configured retention."""

    days = cint(get_config("dmarc_report_retention_days")) or 365
    cutoff = add_days(now_datetime(), -days)
    names = frappe.get_all("DMARC Report", {"date_range_end": ["<", cutoff]}, pluck="name")
    delete_reports(names)


def delete_reports_for_domain(domain: str) -> None:
    """Called when a Mail Domain goes: its history must not surface for whoever adds it next."""

    delete_reports(frappe.get_all("DMARC Report", {"domain": domain}, pluck="name"))


def delete_reports(names: list[str]) -> None:
    if not names:
        return
    frappe.db.delete("DMARC Report Record", {"parent": ["in", names], "parenttype": "DMARC Report"})
    frappe.db.delete("DMARC Report", {"name": ["in", names]})


# --- payloads -------------------------------------------------------------------------


def report_payload(row) -> dict:
    return {
        "name": row.name,
        "domain": row.domain,
        "policy_domain": row.policy_domain,
        "reporter": row.org_name,
        "reporter_email": row.reporter_email,
        "report_id": row.report_id,
        "date_range_begin": utc_iso(row.date_range_begin),
        "date_range_end": utc_iso(row.date_range_end),
        "received_at": utc_iso(row.received_at),
        "policy": {
            "p": row.policy,
            "sp": row.subdomain_policy,
            "pct": cint(row.percentage),
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
    "domain",
    "policy_domain",
    "org_name",
    "reporter_email",
    "report_id",
    "date_range_begin",
    "date_range_end",
    "received_at",
    "policy",
    "subdomain_policy",
    "percentage",
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
        f"{r.get('type') or ''}: {r.get('comment') or ''}".strip(": ")
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
    """Stalwart encodes lists as ``{"0": item, "1": item}``; a JSON list is accepted as well."""

    if isinstance(value, dict):
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
