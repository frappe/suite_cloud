"""TLS aggregate reports of the site's domains, as the hourly fetch stored them."""

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, get_datetime

from suite_cloud.api.site import current_site, owned, owned_page, site_api
from suite_cloud.cloud_mail.doctype.tls_report.tls_report import report_payloads
from suite_cloud.cloud_mail.reports import report_window
from suite_cloud.utils import utc_iso

REPORT_PAGE_CAP = 500  # the dashboard offers pages of up to 500; a listing row carries no policies
TOP_FAILURES = 20


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_tls_reports(
    domain: str | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
    start: int = 0,
    limit: int = 50,
    *,
    days: int | None = None,
) -> dict:
    """Newest period first; ``since``/``until`` bound the period a report covers, and ``days``
    keeps the reports whose period ended within the last so many days, as the summary counts;
    without it (or with 0) everything still held is listed.

    A report that counted no sessions says nothing about the domain and is left out, here and
    in the summary; ``get_tls_report`` still answers for it by name.
    """

    filters: dict = {"total_sessions": [">", 0]}
    if domain:
        filters["policy_domain"] = owned("Mail Domain", domain).name
    if since_days := report_window(days)[0]:
        filters["date_range_end"] = [">=", since_days]
    if since:
        filters["date_range_end"] = [">=", get_datetime(since)]
    if until:
        filters["date_range_begin"] = ["<=", get_datetime(until)]
    names, total = owned_page(
        "TLS Report",
        search,
        start,
        limit,
        REPORT_PAGE_CAP,
        ("policy_domain", "org_name", "report_id"),
        filters,
        order_by="date_range_end desc, name asc",
    )
    return {"items": report_payloads(names), "total": total}


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_tls_report(report: str) -> dict:
    name = frappe.db.get_value("TLS Report", {"name": report, "site": current_site().name})
    if not name:
        raise frappe.DoesNotExistError(_("TLS Report {0} not found.").format(report))
    return frappe.get_doc("TLS Report", name).to_api(with_records=True)


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_tls_summary(domain: str | None = None, days: int = 30) -> dict:
    """Session totals over the reports whose period ended in the last ``days``, by domain and
    reporter, and the failed sessions by result type; ``days`` of 0 counts everything still held."""

    since, until = report_window(days)
    scope = ReportScope(current_site().name, owned("Mail Domain", domain).name if domain else None, since)
    return {
        "since": utc_iso(since) if since else None,
        "until": utc_iso(until),
        "totals": scope.totals(),
        "domains": scope.by_domain(),
        "reporters": scope.by_reporter(),
        "failures": scope.by_result_type(),
    }


class ReportScope:
    """The site's reports since a moment: sessions from the reports, failures from their rows."""

    def __init__(self, site: str, domain: str | None, since) -> None:
        self.site = site
        self.domain = domain
        self.since = since
        self.report = frappe.qb.DocType("TLS Report")
        self.failure = frappe.qb.DocType("TLS Report Failure")

    def totals(self) -> dict:
        rows = self._rows(self._sessions(self.report.site))
        return rows[0] if rows else {"reports": 0, "sessions": 0, "successful": 0, "failed": 0}

    def by_domain(self) -> list[dict]:
        return self._rows(self._sessions(self.report.policy_domain, key="domain"))

    def by_reporter(self) -> list[dict]:
        return self._rows(self._sessions(self.report.org_name, key="reporter"))

    def by_result_type(self) -> list[dict]:
        report, failure = self.report, self.failure
        failed = Sum(failure.failed_sessions)
        rows = (
            frappe.qb.from_(report)
            .join(failure)
            .on((failure.parent == report.name) & (failure.parenttype == "TLS Report"))
        )
        query = (
            self._scoped(rows)
            .groupby(failure.result_type)
            .select(
                failure.result_type.as_("result_type"),
                Count(report.name).distinct().as_("reports"),
                failed.as_("failed"),
            )
            .orderby(failed, order=frappe.qb.desc)
            .limit(TOP_FAILURES)
        )
        return [
            {"result_type": row.result_type, "reports": cint(row.reports), "failed": cint(row.failed)}
            for row in query.run(as_dict=True)
        ]

    def _sessions(self, group, key: str | None = None):
        report = self.report
        sessions = Sum(report.total_sessions)
        query = (
            self._scoped(frappe.qb.from_(report))
            .groupby(group)
            .select(
                Count(report.name).as_("reports"),
                sessions.as_("sessions"),
                Sum(report.successful_sessions).as_("successful"),
                Sum(report.failed_sessions).as_("failed"),
            )
            .orderby(sessions, order=frappe.qb.desc)
        )
        return query.select(group.as_(key)) if key else query

    def _scoped(self, query):
        report = self.report
        query = query.where((report.site == self.site) & (report.total_sessions > 0))
        if self.since:
            query = query.where(report.date_range_end >= self.since)
        if self.domain:
            query = query.where(report.policy_domain == self.domain)
        return query

    @staticmethod
    def _rows(query) -> list[dict]:
        rows = query.run(as_dict=True)
        for row in rows:
            for field in ("reports", "sessions", "successful", "failed"):
                row[field] = cint(row[field])
        return rows
