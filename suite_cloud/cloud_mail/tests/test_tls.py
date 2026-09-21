from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from suite_cloud.api.mail import domains, tls
from suite_cloud.cloud_mail import reports
from suite_cloud.cloud_mail.doctype.tls_report.tls_report import TLS_REPORTS
from suite_cloud.cloud_mail.tests.test_site_api import SiteApiTestCase

MX = "mail.c1.example.test"


def stalwart_report(
    domain: str, org: str = "Google Inc.", policies: list[dict] | None = None, **fields
) -> dict:
    """A ``TlsExternalReport`` the way Stalwart serialises one (lists as ``{"0": ...}``, sets as
    ``{item: true}``); by default one MTA-STS policy with 8 good sessions and 2 failed ones."""

    if policies is None:
        policies = [policy(domain, 8, [failure("certificateExpired", 1), failure("startTlsNotSupported", 1)])]
    return {
        "report": {
            "organizationName": org,
            "contactInfo": "mailto:smtp-tls-reporting@google.com",
            "reportId": f"2026-09-16T00:00:00Z_{domain}",
            "dateRangeStart": "2026-09-16T00:00:00Z",
            "dateRangeEnd": "2026-09-17T00:00:00Z",
            "policies": {str(i): p for i, p in enumerate(policies)},
            **fields,
        },
        "from": {"email": "noreply-smtp-tls-reporting@google.com"},
        "subject": f"Report Domain: {domain} Submitter: google.com",
        "to": {f"postmaster@{domain}": True},
        "receivedAt": "2026-09-17T06:00:00Z",
        "expiresAt": "2026-10-17T06:00:00Z",
    }


def policy(domain: str, successful: int, failures: list[dict] = (), policy_type: str = "sts") -> dict:
    sts = policy_type == "sts"
    return {
        "policyType": policy_type,
        "policyDomain": domain,
        "policyStrings": {"version: STSv1": True, "mode: enforce": True, f"mx: {MX}": True} if sts else {},
        "mxHosts": {MX: True} if sts else {},
        "totalSuccessfulSessions": successful,
        "totalFailedSessions": sum(f["failedSessionCount"] for f in failures),
        "failureDetails": {str(i): f for i, f in enumerate(failures)},
    }


def failure(result_type: str, count: int) -> dict:
    return {
        "resultType": result_type,
        "sendingMtaIp": "209.85.220.41",
        "receivingMxHostname": MX,
        "receivingIp": "203.0.113.10",
        "failedSessionCount": count,
        "failureReasonCode": "X509_V_ERR_CERT_HAS_EXPIRED" if result_type == "certificateExpired" else None,
    }


# The fixtures cover 16-17 September 2026; the clock is frozen the day after, so the summary
# windows and the retention cutoffs mean the same thing whenever the tests run.
NOW = datetime(2026, 9, 18, 12, 0, 0)


class TestTlsReports(SiteApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        frozen = patch("suite_cloud.cloud_mail.reports.now_datetime", return_value=NOW)
        frozen.start()
        self.addCleanup(frozen.stop)
        domains.create_domain("acme.com")
        self.act_as(self.other)
        domains.create_domain("other.com")
        self.act_as(self.site)

    def tearDown(self) -> None:
        TLS_REPORTS.delete(self.report_names())
        super().tearDown()

    def add(self, report: dict) -> str:
        return self.fake._add("TlsExternalReport", report)

    def fetch(self) -> int:
        return TLS_REPORTS.fetch(self.cluster)

    def report_names(self) -> list[str]:
        return frappe.get_all("TLS Report", {"cluster": self.cluster.name}, pluck="name")

    def stored(self, stalwart_id: str) -> str:
        return frappe.db.get_value("TLS Report", {"cluster": self.cluster.name, "stalwart_id": stalwart_id})

    def test_fetch_stores_new_reports_once_and_attributes_them(self) -> None:
        acme = self.add(stalwart_report("Acme.com."))
        self.add(stalwart_report("other.com", org="Microsoft Corporation"))
        self.add(stalwart_report("nobody.example"))
        self.assertEqual(self.fetch(), 3)
        self.assertEqual(self.fetch(), 0)  # already stored: nothing is asked for again

        doc = frappe.get_doc("TLS Report", self.stored(acme))
        self.assertEqual(
            (doc.site, doc.policy_domain, doc.org_name), (self.site.name, "acme.com", "Google Inc.")
        )
        self.assertEqual((doc.total_sessions, doc.successful_sessions, doc.failed_sessions), (10, 8, 2))
        self.assertEqual(
            (doc.policy_types, doc.contact_info, doc.reporter_email, doc.sent_to),
            (
                "sts",
                "mailto:smtp-tls-reporting@google.com",
                "noreply-smtp-tls-reporting@google.com",
                "postmaster@Acme.com.",
            ),
        )
        self.assertEqual(frappe.parse_json(doc.report)["id"], acme)  # the whole object, envelope included
        [sts] = doc.policies
        self.assertEqual((sts.policy_type, sts.policy_domain, sts.mx_hosts), ("sts", "acme.com", MX))
        self.assertIn("mode: enforce", sts.policy_strings.split("\n"))
        # Result types are stored the way RFC 8460 spells them, with the policy they were counted under.
        self.assertEqual(
            [(f.result_type, f.failed_sessions, f.policy_type) for f in doc.failures],
            [("certificate-expired", 1, "sts"), ("starttls-not-supported", 1, "sts")],
        )
        self.assertEqual(
            (doc.failures[0].receiving_mx_hostname, doc.failures[0].failure_reason_code),
            (MX, "X509_V_ERR_CERT_HAS_EXPIRED"),
        )
        # A report about a domain no site holds is kept for operators, attributed to nobody.
        self.assertIsNone(frappe.db.get_value("TLS Report", {"policy_domain": "nobody.example"}, "site"))

    def test_every_policy_counts_and_unknown_values_are_kept(self) -> None:
        both = self.add(
            stalwart_report(
                "acme.com",
                policies=[
                    policy("acme.com", 5, [failure("certificateExpired", 2)]),
                    policy("acme.com", 3, [failure("someFutureType", 1)], policy_type="tlsa"),
                ],
                organizationName=None,
            )
        )
        self.fetch()
        doc = frappe.get_doc("TLS Report", self.stored(both))
        self.assertEqual((doc.total_sessions, doc.successful_sessions, doc.failed_sessions), (11, 8, 3))
        self.assertEqual(doc.policy_types, "sts, tlsa")
        self.assertEqual([f.result_type for f in doc.failures], ["certificate-expired", "someFutureType"])
        self.assertEqual(doc.failures[1].policy_type, "tlsa")
        # Without an organisation name the report is credited to the address that sent it.
        self.assertEqual(doc.org_name, "noreply-smtp-tls-reporting@google.com")

    def test_a_malformed_report_is_skipped_without_losing_the_rest(self) -> None:
        self.add(stalwart_report("acme.com"))
        broken = self.add({"report": {"policies": {"0": 1}}})
        self.add(stalwart_report("acme.com", org="Microsoft Corporation"))
        errors_before = frappe.db.count("Error Log")
        self.assertEqual(self.fetch(), 2)
        self.assertEqual(len(self.report_names()), 2)
        self.assertEqual(frappe.db.count("Error Log"), errors_before + 1)
        self.assertIn(
            broken, frappe.db.get_value("Error Log", {"name": ["!=", ""]}, "method", order_by="creation desc")
        )

    def test_site_api_shows_only_the_site_s_reports(self) -> None:
        self.add(stalwart_report("acme.com"))
        self.add(
            stalwart_report(
                "acme.com",
                org="Microsoft Corporation",
                policies=[policy("acme.com", 20, policy_type="noPolicyFound")],
                dateRangeEnd="2026-09-18T00:00:00Z",
            )
        )
        self.add(stalwart_report("other.com"))
        # Counted nothing: stored for the record, but neither listed nor summed for the site.
        empty = self.add(stalwart_report("acme.com", org="empty.org", policies=[policy("acme.com", 0)]))
        self.fetch()

        listing = tls.list_tls_reports()
        self.assertEqual(listing["total"], 2)
        self.assertEqual([r["reporter"] for r in listing["items"]], ["Microsoft Corporation", "Google Inc."])
        self.assertEqual(tls.get_tls_summary(days=30)["totals"]["reports"], 2)
        self.assertEqual(tls.get_tls_report(self.stored(empty))["totals"]["sessions"], 0)
        google = listing["items"][1]
        self.assertEqual(google["totals"], {"sessions": 10, "successful": 8, "failed": 2})
        self.assertEqual(
            (google["policy_domain"], google["policy_types"], google["date_range_end"]),
            ("acme.com", ["sts"], "2026-09-17T00:00:00Z"),
        )
        self.assertEqual(listing["items"][0]["policy_types"], ["no-policy-found"])
        self.assertEqual(tls.list_tls_reports(domain="acme.com", search="microsoft")["total"], 1)
        self.assertEqual(tls.list_tls_reports(since="2026-09-19")["total"], 0)
        self.assertRaises(frappe.DoesNotExistError, tls.list_tls_reports, domain="other.com")

        detail = tls.get_tls_report(google["name"])
        self.assertEqual(detail["policies"][0]["mx_hosts"], [MX])
        self.assertEqual((detail["policies"][0]["successful"], detail["policies"][0]["failed"]), (8, 2))
        self.assertEqual(
            [(f["result_type"], f["count"], f["receiving_ip"]) for f in detail["failures"]],
            [("certificate-expired", 1, "203.0.113.10"), ("starttls-not-supported", 1, "203.0.113.10")],
        )
        other_report = frappe.db.get_value("TLS Report", {"policy_domain": "other.com"})
        self.assertRaises(frappe.DoesNotExistError, tls.get_tls_report, other_report)

    def test_summary_totals_sessions_and_ranks_failures(self) -> None:
        self.add(stalwart_report("acme.com"))
        older = self.add(
            stalwart_report(
                "acme.com",
                org="Microsoft Corporation",
                policies=[policy("acme.com", 20, [failure("certificateExpired", 3)])],
            )
        )
        self.add(stalwart_report("other.com"))
        self.fetch()
        frappe.db.set_value("TLS Report", self.stored(older), "date_range_end", add_days(NOW, -60))

        summary = tls.get_tls_summary(days=30)
        self.assertEqual(summary["totals"], {"reports": 1, "sessions": 10, "successful": 8, "failed": 2})
        self.assertEqual(
            summary["domains"],
            [{"domain": "acme.com", "reports": 1, "sessions": 10, "successful": 8, "failed": 2}],
        )
        self.assertEqual(
            sorted((f["result_type"], f["reports"], f["failed"]) for f in summary["failures"]),
            [("certificate-expired", 1, 1), ("starttls-not-supported", 1, 1)],
        )
        # The longer window takes the older report in: failures rank by failed sessions, and
        # reporters by the sessions they saw. The other site's report never counts.
        wider = tls.get_tls_summary(days=90)
        self.assertEqual(wider["totals"], {"reports": 2, "sessions": 33, "successful": 28, "failed": 5})
        self.assertEqual(
            wider["failures"][0], {"result_type": "certificate-expired", "reports": 2, "failed": 4}
        )
        self.assertEqual(
            [r["reporter"] for r in wider["reporters"]], ["Microsoft Corporation", "Google Inc."]
        )
        # The listing takes the same window; no window at all is whatever the retention still holds.
        self.assertEqual(tls.list_tls_reports(days=30)["total"], 1)
        self.assertEqual(tls.list_tls_reports(days=0)["total"], 2)
        everything = tls.get_tls_summary(days=0)
        self.assertEqual((everything["since"], everything["totals"]["sessions"]), (None, 33))

    def test_a_deleted_domain_s_reports_stay_stored_but_unattributed(self) -> None:
        acme = self.add(stalwart_report("acme.com"))
        self.fetch()
        domains.delete_domain("acme.com")
        self.assertIsNone(frappe.db.get_value("TLS Report", self.stored(acme), "site"))
        self.assertEqual(tls.list_tls_reports()["total"], 0)
        # The cluster still lists the report; registering the domain elsewhere must not revive
        # the previous holder's history for the new one.
        self.act_as(self.other)
        domains.create_domain("acme.com")
        self.assertEqual(self.fetch(), 0)
        self.assertEqual(tls.list_tls_reports(domain="acme.com")["total"], 0)

    def test_prune_follows_its_own_retention_and_waits_for_the_cluster(self) -> None:
        kept = self.add(stalwart_report("acme.com"))
        gone = self.add(stalwart_report("other.com"))
        self.fetch()
        for stalwart_id in (kept, gone):
            frappe.db.set_value("TLS Report", self.stored(stalwart_id), "date_range_end", add_days(NOW, -100))
        gone_name = self.stored(gone)
        frappe.db.set_value("TLS Report", gone_name, "expires_at", add_days(NOW, -1))

        def retention(days: int):
            config = {"tls_report_retention_days": days}
            return patch("suite_cloud.cloud_mail.reports.get_config", side_effect=config.get)

        with retention(365):
            TLS_REPORTS.prune_expired()
        self.assertEqual(len(self.report_names()), 2)
        with retention(90):
            TLS_REPORTS.prune_expired()
        self.assertEqual(
            frappe.get_all("TLS Report", {"cluster": self.cluster.name}, pluck="stalwart_id"), [kept]
        )
        for child in ("TLS Report Policy", "TLS Report Failure"):
            self.assertEqual(frappe.db.count(child, {"parent": gone_name}), 0)
        self.assertEqual(frappe.db.count("TLS Report Failure", {"parent": self.stored(kept)}), 2)
        del self.fake.objects["TlsExternalReport"][gone]  # as Stalwart did on expiry
        self.assertEqual(self.fetch(), 0)

    def test_the_retention_setting_refuses_less_than_a_day(self) -> None:
        settings = frappe.get_doc("Suite Cloud Settings")
        settings.tls_report_retention_days = 0
        self.assertRaises(frappe.ValidationError, settings.save, ignore_permissions=True)
        settings = frappe.get_doc("Suite Cloud Settings")
        settings.tls_report_retention_days = None
        settings.save(ignore_permissions=True)
        self.assertEqual(settings.tls_report_retention_days, reports.DEFAULT_RETENTION_DAYS)
