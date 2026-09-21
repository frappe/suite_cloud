# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""DMARC aggregate reports, copied from the clusters (see ``suite_cloud.cloud_mail.reports``)."""

import json
from uuid import uuid7

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt

from suite_cloud.cloud_mail.reports import (
    ReceivedReports,
    as_list,
    envelope_fields,
    envelope_payload,
    listing_payloads,
    local_datetime,
    lower,
    normalize_domain,
    sender_address,
)

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
        records = [record_row(r) for r in as_list(report.get("records"))]
        doc = frappe.new_doc("DMARC Report")
        doc.update(
            {
                **envelope_fields(cluster_name, obj, normalize_domain(report.get("policyDomain"))),
                "org_name": report.get("orgName") or sender_address(obj.get("from")) or "unknown",
                "reporter_email": report.get("email") or sender_address(obj.get("from")),
                "extra_contact_info": report.get("extraContactInfo"),
                "report_id": report.get("reportId"),
                "report_version": flt(report.get("version")),
                "date_range_begin": local_datetime(report.get("dateRangeBegin")),
                "date_range_end": local_datetime(report.get("dateRangeEnd")),
                "policy": report.get("policyDisposition"),
                "subdomain_policy": report.get("policySubdomainDisposition"),
                "testing_mode": int(bool(report.get("policyTestingMode"))),
                "adkim": report.get("policyAdkim"),
                "aspf": report.get("policyAspf"),
                "errors": "\n".join(str(e) for e in as_list(report.get("errors"))) or None,
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


DMARC_REPORTS = ReceivedReports(
    doctype="DMARC Report",
    child_doctypes=("DMARC Report Record",),
    service="dmarc_reports",
    retention_key="dmarc_report_retention_days",
)


def fetch_all_clusters() -> None:
    """Hourly: copies the reports each active cluster holds that are not stored yet."""

    DMARC_REPORTS.fetch_all_clusters()


def prune_expired_reports() -> None:
    """Daily: drops reports older than the retention, once the cluster has dropped them too."""

    DMARC_REPORTS.prune_expired()


# --- payloads -------------------------------------------------------------------------


def report_payload(row) -> dict:
    return {
        **envelope_payload(row),
        "version": flt(row.report_version),
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

    return listing_payloads("DMARC Report", names, REPORT_FIELDS, report_payload)


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
