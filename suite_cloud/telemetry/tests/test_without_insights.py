"""A site without Insights: no warehouse loads or prunes the spool, so the telemetry API keeps
nothing, and the warehouse jobs, the alerts and the data source setup are off."""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from suite_cloud import install
from suite_cloud.api.telemetry import otlp, outbound_scans, webhook
from suite_cloud.telemetry import METRICS, OUTBOUND_SCAN_SYMBOLS, OUTBOUND_SCANS, alerts, warehouse
from suite_cloud.telemetry.status import EVENTS as EVENTS_STREAM
from suite_cloud.telemetry.status import forget_contacts, get_seen_at
from suite_cloud.telemetry.tests.test_outbound_scans import SCANS, scan_record
from suite_cloud.telemetry.tests.test_push import OTLP, WEBHOOK, PushTestCase, metrics_request, webhook_event


def without_insights(module: str):
    return patch(f"{module}.insights_installed", return_value=False)


class TestPushWithoutInsights(PushTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = without_insights("suite_cloud.api.telemetry")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_events_are_answered_and_not_staged(self) -> None:
        forget_contacts()
        self.request(self.token, f"{WEBHOOK}?stream=events")
        self.assertEqual(webhook(events=[webhook_event("1")]), {"accepted": 1, "skipped": 0})
        self.assertEqual(self.staged(), [])
        # The status on the node form still sees the server.
        self.assertIsNotNone(get_seen_at(self.node.ipv4_address, EVENTS_STREAM))

    def test_metrics_are_answered_and_not_staged(self) -> None:
        self.request(self.token, OTLP)
        with (
            patch("frappe.request.get_data", return_value=metrics_request()),
            patch("suite_cloud.api.telemetry.log_error"),
        ):
            self.assertEqual(otlp().status_code, 200)
        self.assertEqual(self.staged(METRICS), [])

    def test_outbound_scans_are_answered_and_not_staged(self) -> None:
        self.request(self.token, SCANS)
        self.assertEqual(outbound_scans(scans=[scan_record()]), {"accepted": 1, "skipped": 0})
        self.assertEqual(self.staged(OUTBOUND_SCANS), [])
        self.assertEqual(self.staged(OUTBOUND_SCAN_SYMBOLS), [])


class TestJobsWithoutInsights(IntegrationTestCase):
    def test_the_warehouse_jobs_and_alerts_do_nothing(self) -> None:
        with (
            without_insights("suite_cloud.telemetry.warehouse"),
            patch.object(warehouse, "Warehouse") as warehouse_class,
        ):
            warehouse.load_staged_batches()
            warehouse.prune_expired_rows()
        warehouse_class.assert_not_called()

        with without_insights("suite_cloud.telemetry.alerts"), patch.object(alerts, "Alerts") as alerts_class:
            alerts.check_server_alerts()
        alerts_class.assert_not_called()

    def test_setup_leaves_out_the_data_source(self) -> None:
        with without_insights("suite_cloud.install"), patch.object(install, "Warehouse") as warehouse_class:
            install.setup()
        warehouse_class.assert_not_called()
