# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""TLS aggregate reports (RFC 8460), copied from the clusters (see ``suite_cloud.cloud_mail.reports``).

Servers that deliver mail to a domain report once a day how many of their connections to its MX
hosts negotiated TLS the way the domain's MTA-STS or DANE policy asks for, and why the others
failed. The domain's TLS-RPT record sends the reports to postmaster@, where the cluster keeps them.
"""

from uuid import uuid7

import frappe
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.cloud_mail.reports import (
    ReceivedReports,
    as_list,
    envelope_fields,
    envelope_payload,
    listing_payloads,
    local_datetime,
    normalize_domain,
    sender_address,
)

# Stalwart spells the RFC 8460 values in camelCase; they are stored the way the RFC spells them.
POLICY_TYPES = {"tlsa": "tlsa", "sts": "sts", "noPolicyFound": "no-policy-found", "other": "other"}
RESULT_TYPES = {
    "startTlsNotSupported": "starttls-not-supported",
    "certificateHostMismatch": "certificate-host-mismatch",
    "certificateExpired": "certificate-expired",
    "certificateNotTrusted": "certificate-not-trusted",
    "validationFailure": "validation-failure",
    "tlsaInvalid": "tlsa-invalid",
    "dnssecInvalid": "dnssec-invalid",
    "daneRequired": "dane-required",
    "stsPolicyFetchError": "sts-policy-fetch-error",
    "stsPolicyInvalid": "sts-policy-invalid",
    "stsWebpkiInvalid": "sts-webpki-invalid",
    "other": "other",
}


class TLSReport(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.cloud_mail.doctype.tls_report_failure.tls_report_failure import TLSReportFailure
        from suite_cloud.cloud_mail.doctype.tls_report_policy.tls_report_policy import TLSReportPolicy

        cluster: DF.Link
        contact_info: DF.SmallText | None
        date_range_begin: DF.Datetime | None
        date_range_end: DF.Datetime | None
        expires_at: DF.Datetime | None
        failed_sessions: DF.Int
        failures: DF.Table[TLSReportFailure]
        org_name: DF.Data
        policies: DF.Table[TLSReportPolicy]
        policy_domain: DF.Data
        policy_types: DF.Data | None
        received_at: DF.Datetime | None
        report: DF.JSON | None
        report_id: DF.Data | None
        reporter_email: DF.Data | None
        sent_to: DF.SmallText | None
        site: DF.Link | None
        stalwart_id: DF.Data
        subject: DF.SmallText | None
        successful_sessions: DF.Int
        total_sessions: DF.Int
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    @classmethod
    def from_stalwart(cls, cluster_name: str, obj: dict) -> TLSReport:
        """Builds the document for one ``TlsExternalReport`` object (nothing is saved)."""

        report = obj.get("report") or {}
        policies = as_list(report.get("policies"))
        policy_rows = [policy_row(p) for p in policies]
        domains = policy_domains(policy_rows)
        doc = frappe.new_doc("TLS Report")
        doc.update(
            {
                **envelope_fields(cluster_name, obj, domains[0] if domains else "", len(domains) == 1),
                "org_name": report.get("organizationName") or sender_address(obj.get("from")) or "unknown",
                "reporter_email": sender_address(obj.get("from")),
                "contact_info": report.get("contactInfo"),
                "report_id": report.get("reportId"),
                "date_range_begin": local_datetime(report.get("dateRangeStart")),
                "date_range_end": local_datetime(report.get("dateRangeEnd")),
                "policy_types": ", ".join(
                    dict.fromkeys(r["policy_type"] for r in policy_rows if r["policy_type"])
                )
                or None,
                **totals(policy_rows),
            }
        )
        doc.set("policies", policy_rows)
        doc.set(
            "failures",
            [
                failure_row(detail, row)
                for policy, row in zip(policies, policy_rows, strict=True)
                for detail in as_list(policy.get("failureDetails"))
            ],
        )
        return doc

    def to_api(self, with_records: bool = False) -> dict:
        payload = report_payload(self)
        if with_records:
            payload["policies"] = [policy_payload(r) for r in self.policies]
            payload["failures"] = [failure_payload(r) for r in self.failures]
        return payload


def on_doctype_update() -> None:
    # A report is one object on one cluster; the pair is what the fetch dedups on, and two runs
    # racing each other must not store it twice.
    frappe.db.add_unique("TLS Report", ["cluster", "stalwart_id"])


TLS_REPORTS = ReceivedReports(
    doctype="TLS Report",
    child_doctypes=("TLS Report Policy", "TLS Report Failure"),
    service="tls_reports",
    retention_key="tls_report_retention_days",
)


def fetch_all_clusters() -> None:
    """Hourly: copies the reports each active cluster holds that are not stored yet."""

    TLS_REPORTS.fetch_all_clusters()


def prune_expired_reports() -> None:
    """Daily: drops reports older than the retention, once the cluster has dropped them too."""

    TLS_REPORTS.prune_expired()


# --- payloads -------------------------------------------------------------------------


def report_payload(row) -> dict:
    return {
        **envelope_payload(row),
        "contact_info": row.contact_info,
        "policy_types": row.policy_types.split(", ") if row.policy_types else [],
        "totals": {
            "sessions": cint(row.total_sessions),
            "successful": cint(row.successful_sessions),
            "failed": cint(row.failed_sessions),
        },
    }


REPORT_FIELDS = [
    "name",
    "policy_domain",
    "org_name",
    "reporter_email",
    "contact_info",
    "report_id",
    "subject",
    "sent_to",
    "date_range_begin",
    "date_range_end",
    "received_at",
    "policy_types",
    "total_sessions",
    "successful_sessions",
    "failed_sessions",
]


def report_payloads(names: list[str]) -> list[dict]:
    """The listing shape of many reports in one query, in the order of ``names``."""

    return listing_payloads("TLS Report", names, REPORT_FIELDS, report_payload)


def policy_payload(row) -> dict:
    return {
        "policy_type": row.policy_type,
        "policy_domain": row.policy_domain,
        "mx_hosts": lines(row.mx_hosts),
        "policy_strings": lines(row.policy_strings),
        "successful": cint(row.successful_sessions),
        "failed": cint(row.failed_sessions),
    }


def failure_payload(row) -> dict:
    return {
        "result_type": row.result_type,
        "count": cint(row.failed_sessions),
        "policy_type": row.policy_type,
        "policy_domain": row.policy_domain,
        "sending_mta_ip": row.sending_mta_ip,
        "receiving_mx_hostname": row.receiving_mx_hostname,
        "receiving_mx_helo": row.receiving_mx_helo,
        "receiving_ip": row.receiving_ip,
        "failure_reason_code": row.failure_reason_code,
        "additional_information": row.additional_information,
    }


# --- parsing ----------------------------------------------------------------------------


def policy_domains(policy_rows: list[dict]) -> list[str]:
    """Every domain the report's policies name, the first one first.

    A sender mails a report to the address in one domain's TLS-RPT record, so its policies
    normally name that one domain (several policies of it, such as MTA-STS beside DANE). A report
    naming more is stored under the first but shown to no site: whichever site held that domain
    would otherwise see the other domains' results.
    """

    return list(dict.fromkeys(r["policy_domain"] for r in policy_rows if r["policy_domain"]))


def policy_row(policy: dict) -> dict:
    return {
        "policy_type": rfc_name(POLICY_TYPES, policy.get("policyType")),
        "policy_domain": normalize_domain(policy.get("policyDomain")),
        "successful_sessions": cint(policy.get("totalSuccessfulSessions")),
        "failed_sessions": cint(policy.get("totalFailedSessions")),
        "mx_hosts": "\n".join(as_list(policy.get("mxHosts"))) or None,
        "policy_strings": "\n".join(as_list(policy.get("policyStrings"))) or None,
    }


def failure_row(detail: dict, policy: dict) -> dict:
    return {
        "result_type": rfc_name(RESULT_TYPES, detail.get("resultType")),
        "failed_sessions": cint(detail.get("failedSessionCount")),
        "sending_mta_ip": detail.get("sendingMtaIp"),
        "receiving_mx_hostname": detail.get("receivingMxHostname"),
        "receiving_mx_helo": detail.get("receivingMxHelo"),
        "receiving_ip": detail.get("receivingIp"),
        "failure_reason_code": detail.get("failureReasonCode"),
        "additional_information": detail.get("additionalInformation"),
        "policy_type": policy["policy_type"],
        "policy_domain": policy["policy_domain"],
    }


def totals(policy_rows: list[dict]) -> dict:
    successful = sum(r["successful_sessions"] for r in policy_rows)
    failed = sum(r["failed_sessions"] for r in policy_rows)
    return {
        "total_sessions": successful + failed,
        "successful_sessions": successful,
        "failed_sessions": failed,
    }


def rfc_name(names: dict, value) -> str | None:
    """A value Stalwart does not know yet is kept as it came."""

    return names.get(value, value) if value else None


def lines(value) -> list[str]:
    return value.split("\n") if value else []
