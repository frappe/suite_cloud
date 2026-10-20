"""The telemetry API: the secret signs the agent in, a batch is checked whole and staged once."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import frappe
import pyarrow.parquet as parquet
from frappe.tests import IntegrationTestCase
from frappe.utils import get_test_client

from suite_cloud.api.telemetry import MAX_RECORDS, authenticate, events, metrics
from suite_cloud.api.tenant import InvalidInputError
from suite_cloud.telemetry import EVENTS, METRICS, TELEMETRY_USER
from suite_cloud.telemetry.spool import Spool
from suite_cloud.tests.fixtures import configure_settings

SECRET = "agent-secret-for-tests"
PATH = "/api/method/suite_cloud.api.telemetry.events"
EVENT = {
    "timestamp": "2026-10-20T09:59:00Z",
    "level": "info",
    "event": "delivery.delivered",
    "span_id": 42,
    "data": {"remoteIp": "198.51.100.7"},
}
METRIC = {"timestamp": "2026-10-20T09:59:00+02:00", "name": "server.memory", "type": "gauge", "value": 1024}
HOST = {"source": "stalwart", "host_ip": "203.0.113.5", "hostname": " n1.c1.example.test "}


class TelemetryApiTestCase(IntegrationTestCase):
    def setUp(self) -> None:
        configure_settings(telemetry_secret=SECRET)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = Spool(Path(self.directory.name))
        patcher = patch("suite_cloud.api.telemetry.Spool", return_value=self.spool)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request(SECRET)

    def tearDown(self) -> None:
        frappe.local.request = None
        frappe.set_user("Administrator")

    def request(self, secret: str | None, path: str = PATH, scheme: str = "Bearer") -> None:
        """A request as the auth hook sees it: signed in only when the bearer is the secret."""

        headers = {"Authorization": f"{scheme} {secret}"} if secret is not None else {}
        frappe.set_user("Guest")
        frappe.utils.set_request(method="POST", path=path, headers=headers)
        frappe.local.form_dict = frappe._dict()
        authenticate()

    def staged(self, table=EVENTS) -> list[Path]:
        return self.spool.get_pending(table, 10)


class TestAuthentication(TelemetryApiTestCase):
    def test_the_secret_signs_the_telemetry_user_in(self) -> None:
        self.assertEqual(frappe.session.user, TELEMETRY_USER)

    def test_other_requests_pass_through_untouched(self) -> None:
        for secret, path, scheme in (
            (None, PATH, "Bearer"),
            (SECRET, PATH, "Tenant"),
            (SECRET, "/api/method/frappe.ping", "Bearer"),  # Frappe's own OAuth bearer tokens
        ):
            with self.subTest(f"{scheme} on {path}"):
                self.request(secret, path, scheme)
                self.assertEqual(frappe.session.user, "Guest")

    def test_a_wrong_secret_is_refused(self) -> None:
        for secret in ("", "not-the-secret", SECRET.upper()):
            with self.subTest(secret), self.assertRaisesRegex(frappe.AuthenticationError, "failed"):
                self.request(secret)

    def test_an_unset_secret_refuses_everyone(self) -> None:
        frappe.set_user("Administrator")
        configure_settings(telemetry_secret="")

        with self.assertRaisesRegex(frappe.AuthenticationError, "failed"):
            self.request(SECRET)

    def test_anyone_else_may_not_call_the_methods(self) -> None:
        frappe.set_user("Administrator")

        self.assertRaisesRegex(frappe.PermissionError, "telemetry agents", events, records=[EVENT], **HOST)
        self.assertEqual(self.staged(), [])


class TestValidation(TelemetryApiTestCase):
    def test_a_bad_batch_is_a_400_naming_the_problem_and_stages_nothing(self) -> None:
        cases = (
            ({"host_ip": "999.1.1.1"}, "host_ip"),
            ({"source": "Stalwart!"}, "source"),
            ({"hostname": "x" * 254}, "hostname"),
            ({"batch_id": "nope"}, "batch_id"),
            ({"records": []}, "at least one"),
            ({"records": [EVENT] * (MAX_RECORDS + 1)}, "at most"),
            ({"records": ["x"]}, r"records\[0\] must be an object"),
            ({"records": [{**EVENT, "timestamp": "yesterday"}]}, r"records\[0\].timestamp"),
            ({"records": [{**EVENT, "timestamp": "2026-10-20T09:59:00"}]}, "UTC offset"),
            ({"records": [EVENT, {**EVENT, "level": None}]}, r"records\[1\].level is required"),
            ({"records": [{**EVENT, "event": 7.5}]}, "must be a string"),
            ({"records": [{**EVENT, "data": "x"}]}, r"records\[0\].data must be an object"),
        )
        for overrides, message in cases:
            with self.subTest(message), self.assertRaisesRegex(InvalidInputError, message):
                events(**{**HOST, "records": [EVENT], **overrides})
        self.assertEqual(self.staged(), [])

    def test_a_bad_metric_is_a_400_too(self) -> None:
        cases = (
            ({"type": "rate"}, "must be one of"),
            ({"value": "12"}, "must be a number"),
            ({"value": True}, "must be a number"),
            ({"value": float("nan")}, "must be a finite number"),
            ({"value": 10**400}, "must be a finite number"),
            ({"sum": float("inf")}, "must be a finite number"),
            ({"count": 1.5}, "must fit a 64-bit integer"),
            ({"count": float("nan")}, "must be a finite number"),
            ({"count": 2**63}, "must fit a 64-bit integer"),
            ({"name": ""}, "name is required"),
        )
        for overrides, message in cases:
            with self.subTest(message), self.assertRaisesRegex(InvalidInputError, message):
                metrics(**HOST, records=[{**METRIC, **overrides}])
        self.assertEqual(self.staged(METRICS), [])

    def test_a_missing_argument_is_a_400(self) -> None:
        with self.assertRaisesRegex(InvalidInputError, "source field is required"):
            events(host_ip="203.0.113.5", records=[EVENT])


class TestStaging(TelemetryApiTestCase):
    def test_an_accepted_batch_is_one_file_carrying_the_envelope(self) -> None:
        before = datetime.now(UTC).replace(tzinfo=None)
        response = events(**HOST, records=[EVENT, {**EVENT, "span_id": None, "data": None}])

        self.assertEqual(response["accepted"], 2)
        path = self.spool.get_directory(EVENTS) / f"{response['batch_id']}.parquet"
        self.assertEqual(self.staged(), [path])
        table = parquet.read_table(path)
        self.assertEqual(table.schema, EVENTS.schema)
        rows = table.to_pylist()
        self.assertEqual(
            {key: rows[0][key] for key in ("batch_id", "source", "host_ip", "hostname")},
            {
                "batch_id": response["batch_id"],
                "source": "stalwart",
                "host_ip": "203.0.113.5",
                "hostname": "n1.c1.example.test",
            },
        )
        self.assertEqual(rows[0]["timestamp"], datetime(2026, 10, 20, 9, 59))
        self.assertEqual((rows[0]["span_id"], rows[0]["data"]), ("42", '{"remoteIp":"198.51.100.7"}'))
        self.assertEqual((rows[1]["span_id"], rows[1]["data"]), (None, None))
        self.assertTrue(before <= rows[0]["received_at"] <= datetime.now(UTC).replace(tzinfo=None))
        UUID(response["batch_id"])

    def test_a_metric_batch_keeps_its_numbers_and_converts_the_offset(self) -> None:
        response = metrics(
            source="sfu",
            host_ip="2001:DB8::1",
            records=[METRIC, {**METRIC, "type": "histogram", "count": 3, "sum": 9.5, "labels": {"q": "a"}}],
        )

        rows = parquet.read_table(self.staged(METRICS)[0]).to_pylist()
        self.assertEqual(rows[0]["host_ip"], "2001:db8::1")
        self.assertIsNone(rows[0]["hostname"])
        self.assertEqual(rows[0]["timestamp"], datetime(2026, 10, 20, 7, 59))
        self.assertEqual(
            (rows[0]["value"], rows[0]["count"], rows[0]["sum"], rows[0]["labels"]),
            (1024.0, None, None, None),
        )
        self.assertEqual((rows[1]["count"], rows[1]["sum"], rows[1]["labels"]), (3, 9.5, '{"q":"a"}'))
        self.assertEqual(response["accepted"], 2)

    def test_a_retried_batch_id_replaces_the_file(self) -> None:
        batch_id = "0199F7E0-0000-7000-8000-000000000001"
        first = events(**HOST, records=[EVENT], batch_id=batch_id)
        second = events(**HOST, records=[EVENT, EVENT], batch_id=batch_id.lower())

        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["batch_id"], batch_id.lower())
        self.assertEqual(len(self.staged()), 1)
        self.assertEqual(parquet.read_table(self.staged()[0]).num_rows, 2)

    def test_a_batch_that_cannot_be_written_is_not_accepted(self) -> None:
        with patch.object(self.spool, "write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            events(**HOST, records=[EVENT])
        self.assertEqual(self.staged(), [])


class TestHttpContract(TelemetryApiTestCase):
    def http(self, verb: str, secret: str, **params) -> tuple[int, dict | None]:
        """One request through the WSGI app on this test's own connection (see test_tenant_api_http)."""

        db = frappe.local.db
        headers = {"Authorization": f"Bearer {secret}", "X-Frappe-Site-Name": frappe.local.site}
        with (
            patch("frappe.database.get_db", return_value=db),
            patch.object(db, "commit"),
            patch.object(db, "rollback"),
            patch("frappe.destroy"),
        ):
            response = get_test_client().open(
                "/api/v2/method/suite_cloud.api.telemetry.events",
                method=verb,
                headers=headers,
                base_url="http://localhost/",
                json=params,
            )
        self.request(SECRET)  # the request re-initialised the thread-local
        return response.status_code, response.get_json(silent=True)

    def test_the_status_codes_an_agent_relies_on(self) -> None:
        status, body = self.http("POST", "wrong", **HOST, records=[EVENT])
        self.assertEqual((status, body["errors"][0]["type"]), (401, "AuthenticationError"))

        status, body = self.http("GET", SECRET, **HOST, records=[EVENT])
        self.assertEqual((status, body["errors"][0]["type"]), (403, "PermissionError"))

        status, body = self.http("POST", SECRET, **HOST, records=[{**EVENT, "level": ""}])
        self.assertEqual((status, body["errors"][0]["type"]), (400, "InvalidInputError"))
        self.assertEqual(body["errors"][0]["message"], "records[0].level is required.")

        status, body = self.http("POST", SECRET, **HOST, records=[EVENT])
        self.assertEqual((status, body["data"]["accepted"]), (200, 1))
        self.assertEqual(len(self.staged()), 1)
