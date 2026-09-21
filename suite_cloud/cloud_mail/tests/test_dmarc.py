from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from suite_cloud.api.mail import dmarc, domains
from suite_cloud.cloud_mail import reports
from suite_cloud.cloud_mail.doctype.dmarc_report import dmarc_report
from suite_cloud.cloud_mail.tenancy import sync
from suite_cloud.cloud_mail.tests.test_site_api import SiteApiTestCase


def stalwart_report(
    domain: str, org: str = "google.com", records: list[dict] | None = None, **fields
) -> dict:
    """A ``DmarcExternalReport`` the way Stalwart serialises one (lists as ``{"0": ...}``)."""

    records = (
        records
        if records is not None
        else [record("203.0.113.5", 3), record("198.51.100.9", 2, spf="fail", dkim="fail")]
    )
    return {
        "report": {
            "version": 1,
            "orgName": org,
            "email": f"noreply-dmarc@{org}",
            "reportId": f"{org}-{domain}-1",
            "dateRangeBegin": "2026-09-16T00:00:00Z",
            "dateRangeEnd": "2026-09-17T00:00:00Z",
            "policyDomain": domain,
            "policyAdkim": "relaxed",
            "policyAspf": "relaxed",
            "policyDisposition": "reject",
            "policySubdomainDisposition": "reject",
            "policyTestingMode": False,
            "records": {str(i): r for i, r in enumerate(records)},
            **fields,
        },
        "from": {"email": f"noreply-dmarc@{org}"},
        "subject": f"Report domain: {domain}",
        "to": {f"postmaster@{domain}": True},
        "receivedAt": "2026-09-17T06:00:00Z",
        "expiresAt": "2026-10-17T06:00:00Z",
    }


def record(source_ip: str, count: int, dkim: str = "pass", spf: str = "pass") -> dict:
    return {
        "sourceIp": source_ip,
        "count": count,
        "evaluatedDisposition": "none" if dkim == "pass" or spf == "pass" else "reject",
        "evaluatedDkim": dkim,
        "evaluatedSpf": spf,
        "headerFrom": "acme.com",
        "envelopeFrom": "acme.com",
        "dkimResults": {"0": {"domain": "acme.com", "selector": "frappemail-rsa", "result": dkim}},
        "spfResults": {"0": {"domain": "acme.com", "scope": "mfrom", "result": spf}},
        "policyOverrideReasons": {}
        if dkim == "pass"
        else {"0": {"overrideType": "LocalPolicy", "comment": "allowlisted"}},
    }


# The fixtures cover 16-17 September 2026; the clock is frozen the day after, so the summary
# windows and the retention cutoffs mean the same thing whenever the tests run.
NOW = datetime(2026, 9, 18, 12, 0, 0)
CLOCK = "suite_cloud.cloud_mail.reports.now_datetime"


class TestDmarcReports(SiteApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        frozen = patch(CLOCK, return_value=NOW)
        frozen.start()
        self.addCleanup(frozen.stop)
        domains.create_domain("acme.com")
        self.act_as(self.other)
        domains.create_domain("other.com")
        self.act_as(self.site)

    def tearDown(self) -> None:
        dmarc_report.DMARC_REPORTS.delete(self.report_names())
        super().tearDown()

    def fetch(self) -> int:
        return dmarc_report.DMARC_REPORTS.fetch(self.cluster)

    def report_names(self) -> list[str]:
        return frappe.get_all("DMARC Report", {"cluster": self.cluster.name}, pluck="name")

    def record_count(self) -> int:
        names = self.report_names()
        return frappe.db.count("DMARC Report Record", {"parent": ["in", names]}) if names else 0

    def test_fetch_stores_new_reports_once_and_attributes_them(self) -> None:
        acme = self.fake._add("DmarcExternalReport", stalwart_report("Acme.com."))
        self.fake._add("DmarcExternalReport", stalwart_report("other.com", org="yahoo.com"))
        self.fake._add("DmarcExternalReport", stalwart_report("nobody.example", org="yahoo.com"))
        self.assertEqual(self.fetch(), 3)
        self.assertEqual(self.fetch(), 0)  # already stored: nothing is asked for again

        doc = frappe.get_doc("DMARC Report", {"cluster": self.cluster.name, "stalwart_id": acme})
        self.assertEqual((doc.site, doc.policy_domain), (self.site.name, "acme.com"))
        self.assertEqual((doc.total_messages, doc.passed_messages, doc.failed_messages), (5, 3, 2))
        self.assertEqual((doc.dkim_passed_messages, doc.spf_passed_messages), (3, 3))
        self.assertEqual((doc.policy, doc.testing_mode, doc.adkim), ("reject", 0, "relaxed"))
        self.assertEqual(
            (doc.subject, doc.sent_to, doc.report_version),
            ("Report domain: Acme.com.", "postmaster@Acme.com.", 1.0),
        )
        self.assertEqual(frappe.parse_json(doc.report)["id"], acme)  # the whole object, envelope included
        self.assertEqual(len(doc.records), 2)
        self.assertEqual(doc.records[1].override_reasons, "LocalPolicy: allowlisted")
        # A report about a domain no site holds is kept for operators, attributed to nobody.
        self.assertIsNone(frappe.db.get_value("DMARC Report", {"policy_domain": "nobody.example"}, "site"))

    def test_a_report_addressed_to_another_site_s_domain_reaches_neither(self) -> None:
        stored = self.fake._add(
            "DmarcExternalReport", {**stalwart_report("acme.com"), "to": {"postmaster@other.com": True}}
        )
        self.assertEqual(self.fetch(), 1)
        self.assertIsNone(frappe.db.get_value("DMARC Report", {"stalwart_id": stored}, "site"))
        self.assertEqual(dmarc.list_dmarc_reports()["total"], 0)
        self.act_as(self.other)
        self.assertEqual(dmarc.list_dmarc_reports()["total"], 0)

    def test_fetch_pages_the_cluster_s_ids_in_a_stable_order(self) -> None:
        ids = {self.fake._add("DmarcExternalReport", stalwart_report("acme.com")) for _ in range(7)}
        service = sync.client_for(frappe.get_doc("Mail Domain", "acme.com")).dmarc_reports
        paged = list(service.iter_ids(page_size=3))
        self.assertEqual((len(paged), set(paged)), (7, ids))
        self.assertEqual(paged, sorted(paged))
        # A cluster that lists an id twice (overlapping pages) stores it once and logs nothing.
        errors_before = frappe.db.count("Error Log")
        with patch.object(type(service), "iter_ids", return_value=iter([*sorted(ids), *sorted(ids)])):
            self.assertEqual(self.fetch(), 7)
        self.assertEqual((len(self.report_names()), frappe.db.count("Error Log")), (7, errors_before))

    def test_a_page_holds_up_to_five_hundred_reports(self) -> None:
        self.seed_reports(501)
        page = dmarc.list_dmarc_reports(limit=500)
        self.assertEqual((len(page["items"]), page["total"]), (500, 501))
        self.assertEqual(len(dmarc.list_dmarc_reports(limit=501)["items"]), 500)  # the cap, not the ask
        self.assertEqual(len(dmarc.list_dmarc_reports(start=500, limit=500)["items"]), 1)

    def seed_reports(self, count: int) -> None:
        """Bare stored reports, written in one statement: what the listing pages, minus the fetch."""

        stamp = NOW.strftime("%Y-%m-%d %H:%M:%S")
        fields = [
            "name",
            "creation",
            "modified",
            "owner",
            "modified_by",
            "docstatus",
            "idx",
            "cluster",
            "stalwart_id",
            "policy_domain",
            "site",
            "org_name",
            "total_messages",
            "date_range_end",
        ]
        rows = [
            [
                f"seed-{i:04d}",
                stamp,
                stamp,
                "Administrator",
                "Administrator",
                0,
                0,
                self.cluster.name,
                f"seed{i}",
                "acme.com",
                self.site.name,
                "seed.test",
                1,
                stamp,
            ]
            for i in range(count)
        ]
        frappe.db.bulk_insert("DMARC Report", fields=fields, values=rows)

    def test_a_long_subject_does_not_fail_the_report(self) -> None:
        subject = "Report Domain: acme.com Submitter: " + "x" * 200
        stored = self.fake._add("DmarcExternalReport", {**stalwart_report("acme.com"), "subject": subject})
        self.assertEqual(self.fetch(), 1)
        self.assertEqual(frappe.db.get_value("DMARC Report", {"stalwart_id": stored}, "subject"), subject)

    def test_a_malformed_report_is_skipped_without_losing_the_rest(self) -> None:
        self.fake._add("DmarcExternalReport", stalwart_report("acme.com"))
        broken = self.fake._add(
            "DmarcExternalReport", {"report": {"policyDomain": "acme.com", "records": {"0": 1}}}
        )
        self.fake._add("DmarcExternalReport", stalwart_report("acme.com", org="yahoo.com"))
        errors_before = frappe.db.count("Error Log")
        self.assertEqual(self.fetch(), 2)
        self.assertEqual(len(self.report_names()), 2)
        self.assertEqual(frappe.db.count("Error Log"), errors_before + 1)
        self.assertIn(
            broken, frappe.db.get_value("Error Log", {"name": ["!=", ""]}, "method", order_by="creation desc")
        )

    def test_site_api_shows_only_the_site_s_reports(self) -> None:
        self.fake._add("DmarcExternalReport", stalwart_report("acme.com"))
        self.fake._add(
            "DmarcExternalReport",
            stalwart_report("acme.com", org="yahoo.com", records=[record("203.0.113.5", 10)]),
        )
        self.fake._add("DmarcExternalReport", stalwart_report("other.com"))
        # Counted nothing: stored for the record, but neither listed nor summed for the site.
        empty = self.fake._add(
            "DmarcExternalReport", stalwart_report("acme.com", org="empty.org", records=[])
        )
        self.fetch()

        listing = dmarc.list_dmarc_reports()
        self.assertEqual(listing["total"], 2)
        self.assertNotIn("empty.org", [r["reporter"] for r in listing["items"]])
        self.assertEqual(dmarc.get_dmarc_summary(days=30)["totals"]["reports"], 2)
        stored = frappe.db.get_value("DMARC Report", {"cluster": self.cluster.name, "stalwart_id": empty})
        self.assertEqual(dmarc.get_dmarc_report(stored)["totals"]["messages"], 0)
        self.assertEqual({r["policy_domain"] for r in listing["items"]}, {"acme.com"})
        self.assertEqual(
            listing["items"][0]["totals"],
            {"messages": 5, "passed": 3, "failed": 2, "dkim_passed": 3, "spf_passed": 3},
        )
        self.assertEqual(listing["items"][0]["date_range_end"], "2026-09-17T00:00:00Z")
        self.assertEqual(dmarc.list_dmarc_reports(domain="acme.com", search="yahoo")["total"], 1)
        self.assertEqual(dmarc.list_dmarc_reports(since="2026-09-18")["total"], 0)
        self.assertRaises(frappe.DoesNotExistError, dmarc.list_dmarc_reports, domain="other.com")

        detail = dmarc.get_dmarc_report(listing["items"][0]["name"])
        self.assertEqual(detail["records"][0]["dkim_results"][0]["selector"], "frappemail-rsa")
        other_report = frappe.db.get_value("DMARC Report", {"policy_domain": "other.com"})
        self.assertRaises(frappe.DoesNotExistError, dmarc.get_dmarc_report, other_report)

    def test_summary_aggregates_the_source_rows(self) -> None:
        self.fake._add("DmarcExternalReport", stalwart_report("acme.com"))
        self.fake._add(
            "DmarcExternalReport",
            stalwart_report("acme.com", org="yahoo.com", records=[record("203.0.113.5", 10)]),
        )
        self.fake._add("DmarcExternalReport", stalwart_report("other.com"))
        self.fetch()
        frappe.db.set_value(
            "DMARC Report",
            {"policy_domain": "acme.com", "org_name": "yahoo.com"},
            "date_range_end",
            add_days(NOW, -60),
        )

        summary = dmarc.get_dmarc_summary(days=30)
        self.assertEqual(
            summary["totals"],
            {"reports": 1, "messages": 5, "passed": 3, "failed": 2, "dkim_passed": 3, "spf_passed": 3},
        )
        self.assertEqual(summary["domains"][0]["domain"], "acme.com")
        self.assertEqual(
            [(s["source_ip"], s["messages"]) for s in summary["sources"]],
            [("203.0.113.5", 3), ("198.51.100.9", 2)],
        )
        self.assertEqual(
            summary["reporters"],
            [
                {
                    "reporter": "google.com",
                    "reports": 1,
                    "messages": 5,
                    "passed": 3,
                    "failed": 2,
                    "dkim_passed": 3,
                    "spf_passed": 3,
                }
            ],
        )
        # The listing takes the same window, so a page and its summary describe the same reports.
        self.assertEqual(dmarc.list_dmarc_reports(days=30)["total"], 1)
        self.assertEqual(dmarc.list_dmarc_reports(days=90)["total"], 2)
        # No window at all: whatever the retention still holds, for a retention longer than any period.
        self.assertEqual(dmarc.list_dmarc_reports(days=0)["total"], 2)
        everything = dmarc.get_dmarc_summary(days=0)
        self.assertEqual((everything["since"], everything["totals"]["messages"]), (None, 15))
        # The whole year takes the older report in; the other site's report never counts.
        self.assertEqual(dmarc.get_dmarc_summary(days=90)["totals"]["messages"], 15)
        self.assertEqual(
            dmarc.get_dmarc_summary(days=400)["sources"][0],
            {
                "source_ip": "203.0.113.5",
                "reports": 2,
                "messages": 13,
                "passed": 13,
                "failed": 0,
                "dkim_passed": 13,
                "spf_passed": 13,
            },
        )

    def test_a_deleted_domain_s_reports_stay_stored_but_unattributed(self) -> None:
        acme = self.fake._add("DmarcExternalReport", stalwart_report("acme.com"))
        self.fake._add("DmarcExternalReport", stalwart_report("other.com"))
        self.fetch()
        domains.delete_domain("acme.com")
        self.assertIsNone(frappe.db.get_value("DMARC Report", {"stalwart_id": acme}, "site"))
        self.assertEqual(dmarc.list_dmarc_reports()["total"], 0)
        # The cluster still lists the report; registering the domain elsewhere must not revive
        # the previous holder's history for the new one.
        self.act_as(self.other)
        domains.create_domain("acme.com")
        self.assertEqual(self.fetch(), 0)
        self.assertEqual(dmarc.list_dmarc_reports(domain="acme.com")["total"], 0)

    def test_prune_waits_for_the_cluster_to_drop_its_copy(self) -> None:
        kept = self.fake._add("DmarcExternalReport", stalwart_report("acme.com"))
        gone = self.fake._add("DmarcExternalReport", stalwart_report("other.com"))
        self.fetch()
        old = add_days(NOW, -366)
        for stalwart_id in (kept, gone):
            frappe.db.set_value("DMARC Report", {"stalwart_id": stalwart_id}, "date_range_end", old)
        frappe.db.set_value("DMARC Report", {"stalwart_id": gone}, "expires_at", add_days(NOW, -1))
        dmarc_report.prune_expired_reports()
        self.assertEqual(
            frappe.get_all("DMARC Report", {"cluster": self.cluster.name}, pluck="stalwart_id"), [kept]
        )
        self.assertEqual(self.record_count(), 2)
        del self.fake.objects["DmarcExternalReport"][gone]  # as Stalwart did on expiry
        self.assertEqual(self.fetch(), 0)

    def test_a_negative_retention_never_prunes_the_future(self) -> None:
        self.fake._add("DmarcExternalReport", stalwart_report("acme.com"))
        self.fetch()
        frappe.db.set_value("DMARC Report", {"cluster": self.cluster.name}, "expires_at", add_days(NOW, -1))
        with patch("suite_cloud.cloud_mail.reports.get_config", return_value=-30):
            dmarc_report.prune_expired_reports()
        self.assertEqual(len(self.report_names()), 1)
        settings = frappe.get_doc("Suite Cloud Settings")
        settings.dmarc_report_retention_days = 0
        self.assertRaises(frappe.ValidationError, settings.save, ignore_permissions=True)
        # A site from before the field existed saves with the default filled in, not a refusal.
        settings = frappe.get_doc("Suite Cloud Settings")
        settings.dmarc_report_retention_days = None
        settings.save(ignore_permissions=True)
        self.assertEqual(settings.dmarc_report_retention_days, reports.DEFAULT_RETENTION_DAYS)
