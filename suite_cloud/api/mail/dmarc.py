"""DMARC aggregate reports of the site's domains, as the hourly fetch stored them."""

import frappe
from frappe import _
from frappe.query_builder import Case
from frappe.query_builder.functions import Count, Sum
from frappe.utils import add_days, cint, get_datetime, now_datetime

from suite_cloud.api.site import current_site, owned, owned_page, site_api
from suite_cloud.cloud_mail.doctype.dmarc_report.dmarc_report import PASS, report_payloads
from suite_cloud.utils import utc_iso

REPORT_PAGE_CAP = 200
SUMMARY_MAX_DAYS = 400
TOP_SOURCES = 20


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_dmarc_reports(
    domain: str | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
    start: int = 0,
    limit: int = 50,
) -> dict:
    """Newest period first; ``since``/``until`` bound the period a report covers."""

    filters: dict = {}
    if domain:
        filters["policy_domain"] = owned("Mail Domain", domain).name
    if since:
        filters["date_range_end"] = [">=", get_datetime(since)]
    if until:
        filters["date_range_begin"] = ["<=", get_datetime(until)]
    names, total = owned_page(
        "DMARC Report",
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
def get_dmarc_report(report: str) -> dict:
    name = frappe.db.get_value("DMARC Report", {"name": report, "site": current_site().name})
    if not name:
        raise frappe.DoesNotExistError(_("DMARC Report {0} not found.").format(report))
    return frappe.get_doc("DMARC Report", name).to_api(with_records=True)


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_dmarc_summary(domain: str | None = None, days: int = 30) -> dict:
    """Totals over the reports whose period ended in the last ``days``, by domain, source and reporter."""

    days = min(max(cint(days), 1), SUMMARY_MAX_DAYS)
    until = now_datetime()
    since = add_days(until, -days)
    scope = ReportScope(current_site().name, owned("Mail Domain", domain).name if domain else None, since)
    return {
        "since": utc_iso(since),
        "until": utc_iso(until),
        "totals": scope.totals(),
        "domains": scope.by_domain(),
        "sources": scope.by_source(),
        "reporters": scope.by_reporter(),
    }


class ReportScope:
    """The site's reports since a moment, aggregated from the per-source rows."""

    def __init__(self, site: str, domain: str | None, since) -> None:
        self.site = site
        self.domain = domain
        self.since = since
        self.report = frappe.qb.DocType("DMARC Report")
        self.record = frappe.qb.DocType("DMARC Report Record")

    def totals(self) -> dict:
        rows = self._rows(self._query(self.report.site))
        return rows[0] if rows else self._empty()

    def by_domain(self) -> list[dict]:
        return self._rows(self._query(self.report.policy_domain, key="domain"))

    def by_source(self) -> list[dict]:
        query = self._query(self.record.source_ip, key="source_ip")
        return self._rows(
            query.orderby(Sum(self.record.message_count), order=frappe.qb.desc).limit(TOP_SOURCES)
        )

    def by_reporter(self) -> list[dict]:
        return self._rows(self._query(self.report.org_name, key="reporter"))

    def _query(self, group, key: str | None = None):
        record, report = self.record, self.report
        passed = Case().when((record.dkim == PASS) | (record.spf == PASS), record.message_count).else_(0)
        query = (
            frappe.qb.from_(report)
            .join(record)
            .on((record.parent == report.name) & (record.parenttype == "DMARC Report"))
            .where((report.site == self.site) & (report.date_range_end >= self.since))
            .groupby(group)
            .select(
                Count(report.name).distinct().as_("reports"),
                Sum(record.message_count).as_("messages"),
                Sum(passed).as_("passed"),
                Sum(Case().when(record.dkim == PASS, record.message_count).else_(0)).as_("dkim_passed"),
                Sum(Case().when(record.spf == PASS, record.message_count).else_(0)).as_("spf_passed"),
            )
        )
        if key:
            query = query.select(group.as_(key))
        if self.domain:
            query = query.where(report.policy_domain == self.domain)
        return query

    def _rows(self, query) -> list[dict]:
        rows = []
        for row in query.run(as_dict=True):
            for field in ("reports", "messages", "passed", "dkim_passed", "spf_passed"):
                row[field] = cint(row[field])
            row["failed"] = row["messages"] - row["passed"]
            rows.append(row)
        return rows

    @staticmethod
    def _empty() -> dict:
        return {"reports": 0, "messages": 0, "passed": 0, "failed": 0, "dkim_passed": 0, "spf_passed": 0}
