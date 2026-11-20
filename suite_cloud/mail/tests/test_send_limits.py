"""Sending limits: Stalwart enforces each account's Mail Send Limit with a throttle per limit of the
cluster, picked by a lookup key, and Suite Cloud counts the messages Stalwart queued, which a node's
WebHook sends, into Mail Send Usage."""

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import get_test_client

from suite_cloud.api import send_usage as send_usage_api
from suite_cloud.api import tenant as tenant_api
from suite_cloud.api.mail import accounts, domains
from suite_cloud.api.telemetry import authenticate
from suite_cloud.api.tenant import SendLimitsNotAllowedError
from suite_cloud.mail import send_usage
from suite_cloud.mail.cluster import plan, push, quota, server_token
from suite_cloud.mail.doctype.mail_send_usage import mail_send_usage
from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import (
    add_usage,
    prune_usage_job,
    usage_name,
)
from suite_cloud.mail.tests.fixtures import make_cluster, make_node
from suite_cloud.mail.tests.test_tenant_api import TenantApiTestCase
from suite_cloud.patches.v1_0 import create_default_mail_send_limits
from suite_cloud.telemetry import TELEMETRY_USER
from suite_cloud.tests.fixtures import ROOT_DOMAIN, configure_settings, production_logging

USAGE_HOOK = "/api/v2/method/suite_cloud.api.send_usage.record"
DEFAULT_LIMITS = [50, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]  # what a new cluster gets


def frozen_clock(moment: datetime) -> type[datetime]:
    """``datetime`` with ``now()`` fixed at ``moment``, for the usage module."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz)

    return Frozen


def queued(sender: str, recipients: int, queue_id: int, at: datetime) -> dict:
    """One event as Stalwart's WebHook sends it."""

    return {
        "id": f"event-{queue_id}",
        "createdAt": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": send_usage.QUEUED_EVENT,
        "data": {
            "queueId": queue_id,
            "from": sender,
            "to": [f"rcpt{i}@example.net" for i in range(recipients)],
            "size": 1200,
        },
    }


def clear_seen() -> None:
    """The dev site may count real mail under the same prefix, so only the test server's keys go."""

    client = send_usage.get_client()
    for key in client.scan_iter(match=frappe.cache.make_key("send_usage:queued:n*")):
        client.delete(key)


class SendingLimitsTestCase(TenantApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        domains.create_domain("acme.com")
        self.verify("acme.com")
        self.tenant.db_set("allow_custom_send_limits", 1)
        self.act_as(self.tenant)
        for name in ("alice", "bob", "carol"):
            accounts.create_account(f"{name}@acme.com", "secret-pw", daily_send_limit=500)
        # One moment for the test and the code, so a run across midnight UTC sees one day.
        self.now = datetime.now(UTC).replace(microsecond=0)
        clock = patch.object(mail_send_usage, "datetime", frozen_clock(self.now))
        clock.start()
        self.addCleanup(clock.stop)
        clear_seen()
        self.addCleanup(clear_seen)
        frappe.set_user("Administrator")
        self.node = make_node(self.cluster)
        self.token = server_token.get_token(self.node)

    def tearDown(self) -> None:
        frappe.local.stalwart_server = None
        frappe.db.delete("Mail Send Usage", {"account": ("like", "%@acme.com")})
        super().tearDown()

    def sign_in(self, token: str | None = None) -> None:
        """A WebHook request as the auth hook sees it."""

        token = self.token if token is None else token
        frappe.local.stalwart_server = None
        frappe.set_user("Guest")
        frappe.utils.set_request(method="POST", path=USAGE_HOOK, headers={"Authorization": f"Bearer {token}"})
        frappe.local.form_dict = frappe._dict()
        authenticate()

    def send_usage(self, *events: dict) -> dict:
        self.sign_in()
        return send_usage_api.record(events=list(events))

    def stored(self, account: str, day: date, field: str = "sent") -> int | None:
        return frappe.db.get_value("Mail Send Usage", {"account": account, "date": day}, field)

    @property
    def today(self) -> date:
        return self.now.date()

    def limit_keys(self) -> dict[str, str]:
        return {
            key["key"]: key["value"]
            for key in self.fake.all(quota.LOOKUP_KEY)
            if key["namespace"] == quota.LIMIT_NAMESPACE
        }

    def actions(self, kind: str) -> int:
        return sum(action["@type"] == kind for action in self.fake.all("Action"))

    def limit(self, value: int) -> str:
        return f"{self.cluster.name}-{value}"

    def set_limit(self, email: str, value: int) -> None:
        account = frappe.get_doc("Mail Account", email)
        account.send_limit = self.limit(value)
        account.save()

    def throttle_descriptions(self) -> list[str]:
        return [item["description"] for item in self.fake.all("MtaInboundThrottle")]


class TestUsageHook(SendingLimitsTestCase):
    def test_each_queued_message_adds_its_recipients_to_its_day(self) -> None:
        day = self.today
        answer = self.send_usage(
            queued("alice@acme.com", 3, 1, self.now),
            queued("Alice@acme.com", 2, 2, self.now),  # the sender in any case
            queued("bob@acme.com", 1, 3, self.now),
        )
        self.assertEqual(answer, {"counted": 3})
        self.assertEqual(self.stored("alice@acme.com", day), 5)
        self.assertEqual(self.stored("bob@acme.com", day), 1)
        self.assertEqual(self.stored("alice@acme.com", day, "send_limit"), 500)
        self.assertEqual(frappe.db.count("Mail Send Usage", {"account": "alice@acme.com"}), 1)

    def test_a_message_counts_on_the_day_it_was_queued(self) -> None:
        late = self.now - timedelta(days=1)
        self.send_usage(queued("alice@acme.com", 4, 1, late))
        self.assertEqual(self.stored("alice@acme.com", late.date()), 4)

    def test_an_alias_counts_for_its_account(self) -> None:
        frappe.set_user("Administrator")
        account = frappe.get_doc("Mail Account", "alice@acme.com")
        account.append("aliases", {"alias_email": "sales@acme.com"})
        account.save()
        self.send_usage(queued("sales@acme.com", 2, 1, self.now))
        self.assertEqual(self.stored("alice@acme.com", self.today), 2)

    def test_a_batch_sent_again_counts_each_message_once(self) -> None:
        day = self.today
        first = queued("alice@acme.com", 3, 1, self.now)
        self.send_usage(first)
        # Stalwart sends a failed batch again with the events that came after it.
        answer = self.send_usage(first, queued("alice@acme.com", 2, 2, self.now))
        self.assertEqual(answer, {"counted": 1})
        self.assertEqual(self.stored("alice@acme.com", day), 5)

    def test_a_failed_write_is_counted_when_the_batch_comes_again(self) -> None:
        event = queued("alice@acme.com", 3, 1, self.now)
        with patch.object(send_usage, "add_usage", side_effect=RuntimeError("db down")):
            self.assertRaises(RuntimeError, self.send_usage, event)
        self.send_usage(event)
        self.assertEqual(self.stored("alice@acme.com", self.today), 3)

    def test_other_clusters_unknown_senders_and_other_events_are_skipped(self) -> None:
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        frappe.db.set_value("Mail Account", "bob@acme.com", "cluster", other.name)
        bounce = queued("", 1, 4, self.now)
        message_queued = {**queued("carol@acme.com", 1, 5, self.now), "type": "queue.message-queued"}
        answer = self.send_usage(
            queued("bob@acme.com", 1, 1, self.now),
            queued("ghost@acme.com", 1, 2, self.now),
            bounce,
            message_queued,
            {"type": send_usage.QUEUED_EVENT, "data": {}},
        )
        self.assertEqual(answer, {"counted": 0})
        self.assertFalse(frappe.db.exists("Mail Send Usage", {"account": ("like", "%@acme.com")}))

    def test_only_a_node_s_own_token_signs_in(self) -> None:
        gateway = frappe.get_doc(
            {"doctype": "Egress Gateway", "cluster": self.cluster.name, "ipv4_address": "203.0.113.50"}
        ).insert()
        key, secret = self.node.suite_cloud_key, self.node.get_password("suite_cloud_secret")
        for token in ("wrong", f"{key}.wrong", f"{key}:{secret}", server_token.get_token(gateway)):
            with self.subTest(token=token[:24]), self.assertRaises(frappe.AuthenticationError) as raised:
                self.sign_in(token)
            self.assertEqual(raised.exception.http_status_code, 401)

        # Billing counts whether telemetry is on or not, and only a signed-in node may call.
        frappe.set_user("Administrator")
        configure_settings(enable_telemetry=0)
        self.sign_in()
        self.assertEqual(
            (frappe.session.user, frappe.local.stalwart_server.cluster), (TELEMETRY_USER, self.cluster.name)
        )
        frappe.set_user("Administrator")
        frappe.local.stalwart_server = None
        self.assertRaises(frappe.PermissionError, send_usage_api.record, events=[])

    def test_stalwart_s_webhook_body_is_counted_over_http(self) -> None:
        db = frappe.local.db
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Frappe-Site-Name": frappe.local.site,
            "Content-Type": "application/json",
        }
        body = {"events": [queued("alice@acme.com", 2, 1, self.now)]}
        with (
            patch("frappe.database.get_db", return_value=db),
            patch.object(db, "commit"),
            patch.object(db, "rollback"),
            patch("frappe.destroy"),
            production_logging(),
        ):
            response = get_test_client().open(
                USAGE_HOOK,
                method="POST",
                headers=headers,
                base_url="http://localhost/",
                data=json.dumps(body),
            )
        self.assertEqual((response.status_code, response.get_json()), (200, {"data": {"counted": 1}}))
        self.assertEqual(self.stored("alice@acme.com", self.today), 2)


class TestLimitKeys(SendingLimitsTestCase):
    def test_an_account_s_key_follows_its_limit(self) -> None:
        self.assertEqual(
            self.limit_keys(), dict.fromkeys(("alice@acme.com", "bob@acme.com", "carol@acme.com"), "500")
        )
        reloads = self.actions("ReloadLookupStores")

        self.set_limit("alice@acme.com", 1000)
        self.assertEqual(
            frappe.db.get_value("Mail Account", "alice@acme.com", ["send_limit", "daily_send_limit"]),
            (self.limit(1000), 1000),
        )
        self.assertEqual(self.limit_keys()["alice@acme.com"], "1000")
        # The default's throttle takes every account without a key.
        self.set_limit("alice@acme.com", 50)
        self.assertNotIn("alice@acme.com", self.limit_keys())
        self.assertEqual(self.actions("ReloadLookupStores"), reloads + 2)

        frappe.get_doc({"doctype": "Mail Account", "email": "dave@acme.com"}).insert()
        self.assertEqual(frappe.db.get_value("Mail Account", "dave@acme.com", "send_limit"), self.limit(50))
        self.assertNotIn("dave@acme.com", self.limit_keys())
        frappe.delete_doc("Mail Account", "bob@acme.com")
        self.assertNotIn("bob@acme.com", self.limit_keys())

    def test_an_account_s_first_use_of_a_limit_creates_its_throttle(self) -> None:
        frappe.get_doc(
            {"doctype": "Mail Send Limit", "cluster": self.cluster.name, "daily_limit": 750}
        ).insert()
        self.assertNotIn("suite-cloud-send-limit-750", self.throttle_descriptions())
        reloads = self.actions("ReloadSettings")

        self.set_limit("bob@acme.com", 750)
        self.set_limit("carol@acme.com", 750)
        self.assertEqual(self.throttle_descriptions().count("suite-cloud-send-limit-750"), 1)
        throttle = self.fake.find("MtaInboundThrottle", description="suite-cloud-send-limit-750")
        self.assertEqual(throttle["rate"], {"count": 750, "period": 86_400_000})
        self.assertEqual(self.actions("ReloadSettings"), reloads + 1)
        self.assertEqual(self.limit_keys()["bob@acme.com"], "750")

    def test_an_account_takes_only_a_limit_of_its_cluster(self) -> None:
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        account = frappe.get_doc("Mail Account", "bob@acme.com")
        account.send_limit = f"{other.name}-500"
        self.assertRaisesRegex(frappe.ValidationError, "belongs to another cluster", account.save)
        self.assertEqual(self.limit_keys()["bob@acme.com"], "500")

    def key_id(self, email: str) -> str | None:
        return frappe.db.get_value("Mail Account", email, "send_limit_key")

    def test_an_account_writes_its_key_by_the_id_it_keeps(self) -> None:
        self.assertEqual(
            self.key_id("alice@acme.com"), self.fake.find(quota.LOOKUP_KEY, key="alice@acme.com")["id"]
        )
        # Stalwart cannot find a key by its name, so a save must not read the namespace.
        with patch.object(quota, "get_limit_keys", side_effect=AssertionError("read the namespace")):
            self.set_limit("alice@acme.com", 1000)
            self.assertEqual(self.limit_keys()["alice@acme.com"], "1000")
            self.set_limit("alice@acme.com", 50)  # the default: no key
            self.assertNotIn("alice@acme.com", self.limit_keys())
            self.assertIsNone(self.key_id("alice@acme.com"))
            self.set_limit("alice@acme.com", 2500)
            frappe.delete_doc("Mail Account", "bob@acme.com")
        self.assertEqual(
            self.key_id("alice@acme.com"), self.fake.find(quota.LOOKUP_KEY, key="alice@acme.com")["id"]
        )
        self.assertEqual(set(self.limit_keys()), {"alice@acme.com", "carol@acme.com"})

    def test_a_stale_or_lost_key_id_is_found_again(self) -> None:
        service = self.cluster.get_client().objects(quota.LOOKUP_KEY)
        with patch.object(quota, "get_limit_keys", side_effect=AssertionError("read the namespace")):
            service.delete([self.key_id("alice@acme.com")])  # gone from Stalwart: created again
            self.set_limit("alice@acme.com", 1000)
            # The account lost the id but the key is there: Stalwart's refusal names it.
            kept = self.key_id("bob@acme.com")
            frappe.db.set_value("Mail Account", "bob@acme.com", "send_limit_key", None)
            self.set_limit("bob@acme.com", 2500)
        self.assertEqual(
            self.limit_keys(), {"alice@acme.com": "1000", "bob@acme.com": "2500", "carol@acme.com": "500"}
        )
        self.assertEqual(self.key_id("bob@acme.com"), kept)
        self.assertEqual(
            self.key_id("alice@acme.com"), self.fake.find(quota.LOOKUP_KEY, key="alice@acme.com")["id"]
        )

        # Removing a key the account has no id for reads the namespace once, as before ids were kept.
        frappe.db.set_value("Mail Account", "carol@acme.com", "send_limit_key", None)
        frappe.delete_doc("Mail Account", "carol@acme.com")
        self.assertNotIn("carol@acme.com", self.limit_keys())

    def test_a_sync_repairs_the_keys(self) -> None:
        service = self.cluster.get_client().objects(quota.LOOKUP_KEY)
        service.delete(self.fake.find(quota.LOOKUP_KEY, key="alice@acme.com")["id"])
        service.update(self.fake.find(quota.LOOKUP_KEY, key="bob@acme.com")["id"], {"value": "2000"})
        service.create(quota.limit_key("stray@acme.com", "1000"))
        other = service.create({"namespace": "another", "key": "kept", "value": "1", "isGlobPattern": False})
        client = self.cluster.get_client()

        self.assertTrue(quota.sync_limit_keys(self.cluster, client))
        self.assertEqual(
            self.limit_keys(), dict.fromkeys(("alice@acme.com", "bob@acme.com", "carol@acme.com"), "500")
        )
        self.assertIsNotNone(self.fake.get(quota.LOOKUP_KEY, other["id"]))
        for email in ("alice@acme.com", "bob@acme.com", "carol@acme.com"):  # each account keeps its id
            self.assertEqual(self.key_id(email), self.fake.find(quota.LOOKUP_KEY, key=email)["id"])
        reloads = self.actions("ReloadLookupStores")
        self.assertFalse(quota.sync_limit_keys(self.cluster, client))
        self.assertEqual(self.actions("ReloadLookupStores"), reloads)


class TestMailSendLimit(SendingLimitsTestCase):
    def limits(self, cluster: str | None = None) -> list[tuple[int, int]]:
        return list(
            frappe.get_all(
                "Mail Send Limit",
                {"cluster": cluster or self.cluster.name},
                ["daily_limit", "is_default"],
                order_by="daily_limit",
                as_list=True,
            )
        )

    def test_a_new_cluster_starts_with_the_default_limits_the_lowest_its_default(self) -> None:
        expected = [(value, int(value == 50)) for value in DEFAULT_LIMITS]
        self.assertEqual(self.limits(), expected)
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        self.assertEqual(self.limits(other.name), expected)
        self.assertEqual(
            frappe.db.get_value("Mail Send Limit", f"{other.name}-2500", "title"), "2,500 per day"
        )
        # More than the defaults is the admin's to add.
        frappe.get_doc({"doctype": "Mail Send Limit", "cluster": other.name, "daily_limit": 300_000}).insert()
        self.assertEqual(self.limits(other.name)[-1], (300_000, 0))

    def test_a_cluster_has_one_default(self) -> None:
        limit = frappe.get_doc("Mail Send Limit", self.limit(500))
        limit.is_default = 1
        with patch.object(push, "resync_cluster_after_commit") as resync:
            limit.save()
        resync.assert_called_once_with(self.cluster.name)
        self.assertEqual([value for value, is_default in self.limits() if is_default], [500])

        limit.is_default = 0
        self.assertRaisesRegex(frappe.ValidationError, "Mark another limit", limit.save)
        limit.reload()
        self.assertRaisesRegex(frappe.ValidationError, "cannot be deleted", limit.delete)

    def test_a_limit_is_at_least_one(self) -> None:
        doc = frappe.get_doc({"doctype": "Mail Send Limit", "cluster": self.cluster.name, "daily_limit": 0})
        self.assertRaisesRegex(frappe.ValidationError, "Daily Limit must be at least 1", doc.insert)

    def test_only_an_unused_limit_is_deleted_and_its_throttle_goes_with_it(self) -> None:
        self.assertRaises(frappe.LinkExistsError, frappe.delete_doc, "Mail Send Limit", self.limit(500))
        self.cluster.push_config()
        self.assertIn("suite-cloud-send-limit-1000", self.throttle_descriptions())
        frappe.delete_doc("Mail Send Limit", self.limit(1000))  # tests push at once
        self.assertNotIn("suite-cloud-send-limit-1000", self.throttle_descriptions())

    def test_sync_throttle_creates_a_missing_throttle(self) -> None:
        limit = frappe.get_doc("Mail Send Limit", self.limit(2500))
        self.assertTrue(limit.sync_throttle())
        self.assertFalse(limit.sync_throttle())
        self.assertEqual(self.throttle_descriptions().count("suite-cloud-send-limit-2500"), 1)

    def test_a_deleted_cluster_takes_its_limits(self) -> None:
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        frappe.delete_doc("Stalwart Cluster", other.name, ignore_permissions=True)
        self.assertEqual(self.limits(other.name), [])


class TestQuotaPlan(SendingLimitsTestCase):
    def throttles(self) -> dict[str, dict]:
        operation = quota.throttle_operation(self.cluster)
        return {item["description"]: item for item in operation["value"].values()}

    def test_the_plan_has_a_throttle_per_limit(self) -> None:
        throttles = self.throttles()
        self.assertEqual(
            sorted(throttles),
            sorted(
                [
                    *(f"suite-cloud-send-limit-{value}" for value in DEFAULT_LIMITS),
                    "suite-cloud-submission-rate",
                ]
            ),
        )
        default, top = throttles["suite-cloud-send-limit-50"], throttles["suite-cloud-send-limit-100000"]
        self.assertEqual(
            default["match"]["match"]["0"]["if"],
            "!is_empty(authenticated_as) && !is_empty(rcpt) && !key_exists('suite-cloud-send-limit', authenticated_as)",
        )
        # Reading rcpt makes Stalwart check the throttle once per recipient.
        self.assertEqual(
            top["match"]["match"]["0"]["if"],
            "!is_empty(rcpt) && key_get('suite-cloud-send-limit', authenticated_as) == '100000'",
        )
        self.assertEqual(
            (top["enable"], top["key"], top["rate"]),
            (True, {"authenticatedAs": True}, {"count": 100_000, "period": 86_400_000}),
        )
        submission = throttles["suite-cloud-submission-rate"]
        self.assertEqual(submission["match"]["match"]["0"]["if"], "!is_empty(authenticated_as)")
        self.assertEqual(submission["rate"], {"count": 90, "period": 60_000})  # the cluster's default

        # Stalwart adds its default throttles only when none exist, so recovery leaves ours out.
        self.assertNotIn("MtaInboundThrottle", {op["object"] for op in plan.recovery_plan(self.cluster)})

    def test_the_cluster_sets_the_submission_rate(self) -> None:
        cluster = frappe.get_doc("Stalwart Cluster", self.cluster.name)
        cluster.submission_rate = 0
        self.assertRaisesRegex(frappe.ValidationError, "at least 1", cluster.save, ignore_permissions=True)
        cluster.reload()
        cluster.submission_rate = 120
        with patch.object(push, "resync_cluster_after_commit") as resync:
            cluster.save(ignore_permissions=True)
        resync.assert_called_once_with(cluster.name)
        self.cluster.reload()
        self.assertEqual(self.throttles()["suite-cloud-submission-rate"]["rate"]["count"], 120)

    def test_a_cluster_without_a_default_limit_is_not_pushed(self) -> None:
        frappe.db.set_value("Mail Send Limit", {"cluster": self.cluster.name}, "is_default", 0)
        self.assertRaisesRegex(
            frappe.ValidationError, "no default Mail Send Limit", quota.throttle_operation, self.cluster
        )

    def test_the_usage_webhook_waits_for_every_node_s_token(self) -> None:
        configure_settings(enable_telemetry=0, public_url="https://cloud.example.test")
        operation = next(
            op
            for op in plan.cluster_plan(self.cluster)
            if op["object"] == "WebHook" and "sending-usage" in op["value"]
        )
        webhook = operation["value"]["sending-usage"]
        self.assertEqual(webhook["url"], f"https://cloud.example.test{quota.USAGE_METHOD}")
        self.assertEqual(quota.USAGE_METHOD, USAGE_HOOK)
        self.assertEqual(
            (webhook["events"], webhook["eventsPolicy"]),
            ({"queue.authenticated-message-queued": True}, "include"),
        )
        self.assertEqual(webhook["httpHeaders"], {"X-Suite-Cloud-Hook": "sending-usage"})
        self.assertEqual(webhook["discardAfter"], 24 * 3_600_000)
        self.assertEqual(
            webhook["httpAuth"]["bearerToken"],
            {"@type": "EnvironmentVariable", "variableName": "SUITE_CLOUD_TOKEN"},
        )
        self.assertTrue(webhook["enable"])  # the node is not running yet, and telemetry is off

        self.node.db_set("status", "Active")
        self.assertFalse(quota.usage_webhook_operation(self.cluster)["value"]["sending-usage"]["enable"])
        self.node.db_set("suite_cloud_token_installed", 1)
        self.assertTrue(quota.usage_webhook_operation(self.cluster)["value"]["sending-usage"]["enable"])

    def test_a_push_leaves_only_its_own_throttles(self) -> None:
        for description in ("Sender IP throttle", "Sender address to recipient throttle"):  # Stalwart's
            self.fake._add("MtaInboundThrottle", {"description": description})
        self.fake._add("MtaInboundThrottle", {"description": "suite-cloud-quota-backstop"})  # the old one
        self.fake._add("MtaInboundThrottle", {"description": "suite-cloud-send-limit-33333"})  # no such limit
        self.fake._add("MtaHook", {"httpHeaders": quota.OLD_HOOK_HEADERS})

        self.cluster.push_config()
        self.cluster.push_config()  # a second push finds every throttle by its description
        self.assertEqual(sorted(self.throttle_descriptions()), sorted(self.throttles()))
        self.assertEqual(len(self.throttles()), len(DEFAULT_LIMITS) + 1)  # one per limit, and the rate
        self.assertEqual(self.fake.all("MtaHook"), [])
        self.assertEqual(len(self.limit_keys()), 3)


class TestTenantSendLimits(SendingLimitsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.act_as(self.tenant)

    def test_a_new_account_gets_its_tenant_s_default_unless_the_tenant_sets_one(self) -> None:
        self.assertEqual(accounts.create_account("dave@acme.com", "secret-pw")["daily_send_limit"], 50)
        self.tenant.db_set("default_send_limit", self.limit(1000))
        self.assertEqual(accounts.create_account("erin@acme.com", "secret-pw")["daily_send_limit"], 1000)
        created = accounts.create_account("fay@acme.com", "secret-pw", daily_send_limit=2500)
        self.assertEqual(created["daily_send_limit"], 2500)
        self.assertEqual(self.limit_keys()["fay@acme.com"], "2500")
        self.assertRaisesRegex(
            frappe.ValidationError,
            "Daily Send Limit must be one of 50, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, not 260\\.",
            accounts.create_account,
            "gus@acme.com",
            "secret-pw",
            daily_send_limit=260,
        )
        self.assertFalse(frappe.db.exists("Mail Account", "gus@acme.com"))

    def test_a_tenant_passes_a_limit_only_once_an_operator_allowed_it(self) -> None:
        self.tenant.db_set("allow_custom_send_limits", 0)
        self.act_as(self.tenant)
        for call in (
            lambda: accounts.create_account("dave@acme.com", "secret-pw", daily_send_limit=250),
            lambda: accounts.update_account("bob@acme.com", daily_send_limit=250),
        ):
            self.assertRaisesRegex(SendLimitsNotAllowedError, "may not set send limits", call)
        self.assertFalse(frappe.db.exists("Mail Account", "dave@acme.com"))
        self.assertEqual(accounts.update_account("bob@acme.com", display_name="Bob")["daily_send_limit"], 500)
        self.tenant.db_set("allow_custom_send_limits", 1)
        self.act_as(self.tenant)
        self.assertEqual(
            accounts.update_account("bob@acme.com", daily_send_limit=1000)["daily_send_limit"], 1000
        )
        self.assertEqual(self.limit_keys()["bob@acme.com"], "1000")

    def test_a_tenant_s_default_is_a_limit_of_its_cluster_and_its_limits_list_them(self) -> None:
        frappe.set_user("Administrator")
        tenant = frappe.get_doc("Suite Tenant", self.tenant.name)
        self.assertEqual(tenant.default_send_limit, self.limit(50))  # the cluster's default
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        tenant.default_send_limit = f"{other.name}-500"
        self.assertRaisesRegex(frappe.ValidationError, "belongs to another cluster", tenant.save)
        tenant.reload()
        tenant.default_send_limit = None
        tenant.save()
        self.assertEqual(tenant.default_send_limit, self.limit(50))  # never left empty
        limits = frappe.get_doc("Suite Tenant", self.tenant.name).to_api()["limits"]
        self.assertEqual((limits["send_limits"], limits["default_daily_send_limit"]), (DEFAULT_LIMITS, 50))
        self.tenant.db_set("default_send_limit", self.limit(1000))
        limits = frappe.get_doc("Suite Tenant", self.tenant.name).to_api()["limits"]
        self.assertEqual(limits["default_daily_send_limit"], 1000)

    def test_a_tenant_reads_its_usage_per_month(self) -> None:
        last_month = self.today.replace(day=1) - timedelta(days=1)
        add_usage({("alice@acme.com", self.today): 3, ("bob@acme.com", self.today): 4})
        add_usage(
            {("alice@acme.com", last_month): 10, ("alice@acme.com", last_month - timedelta(days=40)): 99}
        )
        this, previous = f"{self.today:%Y-%m}", f"{last_month:%Y-%m}"

        self.assertEqual(
            tenant_api.get_send_usage(months=2),
            {"months": [{"month": this, "sent": 7}, {"month": previous, "sent": 10}]},
        )
        self.assertEqual(tenant_api.get_send_usage(months=1), {"months": [{"month": this, "sent": 7}]})
        self.act_as(self.other)
        self.assertEqual(tenant_api.get_send_usage(), {"months": []})

    def test_tenants_read_an_account_s_daily_history(self) -> None:
        now = self.now
        # The window's first day is six days back, which some dates put in the previous month.
        today, two_days_ago, first_day, long_ago = (now - timedelta(days=days) for days in (0, 2, 6, 40))
        future = now + timedelta(days=40)  # no batch stores one, and it stays out of the answer too
        start_of_month = first_day.replace(day=1)  # before the window, but in its first month
        days = {
            moment.date() for moment in (today, two_days_ago, first_day, long_ago, future, start_of_month)
        }
        add_usage({("alice@acme.com", day): 9 for day in days})

        history = accounts.get_send_usage("alice@acme.com", days=7)
        self.assertEqual(
            [day["date"] for day in history["days"]],
            [f"{moment:%Y-%m-%d}" for moment in (today, two_days_ago, first_day)],
        )
        self.assertEqual(history["days"][0], {"date": f"{today:%Y-%m-%d}", "sent": 9, "limit": 500})
        # A month sums every day stored for it, in the window or not.
        months = {}
        for day in {today.date(), two_days_ago.date(), first_day.date(), start_of_month.date()}:
            months[f"{day:%Y-%m}"] = months.get(f"{day:%Y-%m}", 0) + 9
        self.assertEqual({month["month"]: month["sent"] for month in history["months"]}, months)
        self.assertNotIn("limit", history["months"][0])

        self.act_as(self.other)
        self.assertRaises(frappe.DoesNotExistError, accounts.get_send_usage, "alice@acme.com")


class TestSendLimitsPatch(SendingLimitsTestCase):
    def test_each_limit_in_use_becomes_a_mail_send_limit(self) -> None:
        for email, value in (("alice@acme.com", 260), ("bob@acme.com", 2000), ("carol@acme.com", 5000)):
            frappe.db.set_value(
                "Mail Account", email, {"send_limit": None, "daily_send_limit": value}, update_modified=False
            )
        frappe.get_doc({"doctype": "Mail Account", "email": "dave@acme.com"}).insert()
        frappe.db.set_value("Mail Account", "dave@acme.com", {"send_limit": None, "daily_send_limit": 0})
        frappe.db.delete("Mail Send Limit", {"cluster": self.cluster.name})
        for tenant in (self.tenant, self.other):  # the field is new: no tenant holds one yet
            tenant.db_set("default_send_limit", None)
        old_defaults = {self.tenant.name: 300}

        with (
            patch.object(
                create_default_mail_send_limits,
                "get_old_tenant_defaults",
                side_effect=lambda cluster: old_defaults if cluster == self.cluster.name else {},
            ),
            patch.object(frappe.db, "has_column", return_value=True),
            patch.object(frappe.db, "sql_ddl") as ddl,
            patch.object(push, "resync_cluster_after_commit") as resync,
        ):
            create_default_mail_send_limits.execute()
            create_default_mail_send_limits.execute()  # a second run finds every limit made and linked

        values = frappe.get_all(
            "Mail Send Limit", {"cluster": self.cluster.name}, pluck="daily_limit", order_by="daily_limit"
        )
        self.assertEqual(values, sorted([*DEFAULT_LIMITS, 260, 300, 2000]))
        linked = frappe.get_all(
            "Mail Account",
            {"tenant": self.tenant.name},
            ["name", "send_limit", "daily_send_limit"],
            as_list=True,
        )
        self.assertEqual(
            sorted(linked),
            [
                ("alice@acme.com", self.limit(260), 260),
                ("bob@acme.com", self.limit(2000), 2000),
                ("carol@acme.com", self.limit(5000), 5000),
                ("dave@acme.com", self.limit(50), 50),  # the default
            ],
        )
        defaults = [
            frappe.db.get_value("Suite Tenant", tenant.name, "default_send_limit")
            for tenant in (self.tenant, self.other)
        ]
        self.assertEqual(defaults, [self.limit(300), self.limit(50)])  # its old default, else the cluster's
        ddl.assert_called_with("alter table `tabMail Send Usage` drop column `refused`")
        resync.assert_not_called()


class TestUsage(SendingLimitsTestCase):
    def test_rows_are_written_in_one_lock_order(self) -> None:
        today, yesterday = self.today, self.today - timedelta(days=1)
        totals = {("bob@acme.com", today): 1, ("alice@acme.com", today): 1, ("alice@acme.com", yesterday): 1}
        with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
            add_usage(totals)
        insert = next(call for call in sql.call_args_list if "Mail Send Usage" in str(call.args[0]))
        # Each row's name stands for its account and day, in the order of the statement.
        expected = [
            usage_name("alice@acme.com", yesterday),
            usage_name("alice@acme.com", today),
            usage_name("bob@acme.com", today),
        ]
        names = [value for value in insert.args[1].values() if value in expected]
        self.assertEqual(names, expected)

    def test_counts_add_up_and_a_past_day_keeps_its_limit(self) -> None:
        day, yesterday = self.today, self.today - timedelta(days=1)
        add_usage({("alice@acme.com", day): 5, ("alice@acme.com", yesterday): 90})
        frappe.db.set_value("Mail Account", "alice@acme.com", "daily_send_limit", 250)
        add_usage({("alice@acme.com", day): 2, ("alice@acme.com", yesterday): 1})
        self.assertEqual(
            (self.stored("alice@acme.com", day), self.stored("alice@acme.com", day, "send_limit")), (7, 250)
        )
        self.assertEqual(
            (
                self.stored("alice@acme.com", yesterday),
                self.stored("alice@acme.com", yesterday, "send_limit"),
            ),
            (91, 500),
        )

    def test_the_row_name_fits_any_account(self) -> None:
        long_account = f"{'a' * 64}@{'b' * 75}.com"  # 140 characters, the longest name
        self.assertEqual(len(usage_name(long_account, date(2026, 10, 28))), 40)
        self.assertNotEqual(
            usage_name("alice@acme.com", date(2026, 10, 28)), usage_name("alice@acme.com", date(2026, 10, 29))
        )

    def test_an_account_deleted_meanwhile_does_not_fail_the_batch(self) -> None:
        add_usage({("gone@acme.com", self.today): 3, ("alice@acme.com", self.today): 2})
        self.assertEqual(self.stored("alice@acme.com", self.today), 2)
        self.assertIsNone(self.stored("gone@acme.com", self.today))

    def test_form_and_api_show_today_and_the_month_s_sum(self) -> None:
        earlier = [self.today - timedelta(days=days) for days in range(1, 4)]
        this_month = [day for day in earlier if day.month == self.today.month]
        last_month = self.today.replace(day=1) - timedelta(days=1)
        add_usage({("alice@acme.com", self.today): 80, ("alice@acme.com", last_month): 1000})
        add_usage({("alice@acme.com", day): 100 for day in earlier})
        usage = {"day": 80, "month": 80 + 100 * len(this_month)}
        doc = frappe.get_doc("Mail Account", "alice@acme.com")
        doc.run_method("onload")
        self.assertEqual(doc.get_onload().send_usage, usage)

        self.act_as(self.tenant)
        payload = accounts.get_account("alice@acme.com")
        self.assertEqual((payload["daily_send_limit"], payload["send_usage"]), (500, usage))
        self.assertNotIn("monthly_send_limit", payload)
        listed = {item["email"]: item for item in accounts.list_accounts()["items"]}
        self.assertEqual(listed["alice@acme.com"]["send_usage"], usage)
        self.assertEqual(listed["bob@acme.com"]["send_usage"], {"day": 0, "month": 0})

        accounts.delete_account("alice@acme.com")
        # Billing outlives the account: its rows stay under its tenant, and still add up.
        self.assertTrue(
            frappe.db.exists("Mail Send Usage", {"account": "alice@acme.com", "tenant": self.tenant.name})
        )
        this_month = tenant_api.get_send_usage(months=1)["months"]
        self.assertEqual(this_month, [{"month": f"{self.today:%Y-%m}", "sent": usage["month"]}])

    def test_prune_keeps_the_days_of_the_retention(self) -> None:
        days = [self.today - timedelta(days=days) for days in (0, 29, 30, 31, 70)]
        add_usage({("alice@acme.com", day): 1 for day in days})
        with (
            patch(f"{mail_send_usage.__name__}.get_config", return_value=30),
            patch.object(frappe.db, "delete", wraps=frappe.db.delete) as delete,
        ):
            prune_usage_job()
        # One day per statement, so a shortened retention never deletes months of rows at once.
        self.assertEqual(len(delete.call_args_list), 2)
        self.assertTrue(all(set(call.args[1]) == {"date"} for call in delete.call_args_list))
        kept = set(frappe.get_all("Mail Send Usage", {"account": "alice@acme.com"}, pluck="date"))
        self.assertEqual(kept, set(days[:3]))

    def test_a_retention_under_a_day_falls_back_to_the_default(self) -> None:
        for days in (0, -5):
            with patch(f"{mail_send_usage.__name__}.get_config", return_value=days):
                self.assertEqual(mail_send_usage.retention_days(), mail_send_usage.DEFAULT_RETENTION_DAYS)
