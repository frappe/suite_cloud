"""Sending limits: the Agents pull them per cluster, push what they counted, and the cluster plan
carries the hook and its backstop once the Agents can answer it."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from suite_cloud.api.mail import accounts, domains
from suite_cloud.api.telemetry import quota as quota_api
from suite_cloud.api.tenant import InvalidInputError
from suite_cloud.mail.cluster import plan, quota
from suite_cloud.mail.doctype.mail_send_usage import mail_send_usage
from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import (
    current_periods,
    periods_at,
    prune_usage,
    upsert_usage,
)
from suite_cloud.mail.tests.fixtures import make_cluster, make_node
from suite_cloud.mail.tests.test_tenant_api import TenantApiTestCase
from suite_cloud.patches.v1_0 import fill_quota_hook_tokens
from suite_cloud.telemetry import TELEMETRY_USER
from suite_cloud.tests.fixtures import ROOT_DOMAIN

NODE_IP = "203.0.113.10"


class SendingLimitsTestCase(TenantApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        domains.create_domain("acme.com")
        self.verify("acme.com")
        for name in ("alice", "bob", "carol"):
            accounts.create_account(f"{name}@acme.com", "secret-pw", daily_send_limit=100)
        frappe.set_user("Administrator")
        self.node = make_node(self.cluster, ipv4=NODE_IP)
        self.node.db_set("status", "Active")
        self.act_as(self.tenant)
        settle = patch.object(quota_api, "SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)

    def tearDown(self) -> None:
        frappe.local.agent_cluster = None
        frappe.db.delete("Mail Send Usage", {"account": ("like", "%@acme.com")})
        super().tearDown()

    def as_agent(self, cluster: str | None = None) -> None:
        """As the auth hook leaves a request its cluster's credentials signed in."""

        frappe.set_user(TELEMETRY_USER)
        frappe.local.agent_cluster = cluster or self.cluster.name
        frappe.utils.set_request(method="POST", headers={"User-Agent": "suite-cloud-agent/0.2.0+abcdef12"})
        frappe.local.form_dict = frappe._dict()

    def pull(self, since: str | None = None) -> dict:
        self.as_agent()
        return quota_api.limits(NODE_IP, since)


class TestLimitsApi(SendingLimitsTestCase):
    def test_pages_lose_no_account_that_shares_a_modified_time(self) -> None:
        moment = now_datetime() - timedelta(minutes=1)
        frappe.db.set_value("Mail Account", {"domain": "acme.com"}, "modified", moment, update_modified=False)
        with patch.object(quota_api, "PAGE_SIZE", 2):
            first = self.pull()
            second = self.pull(first["next_version"])
            third = self.pull(second["next_version"])

        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        names = [entry["account"] for entry in first["limits"] + second["limits"]]
        self.assertEqual(names, ["alice@acme.com", "bob@acme.com", "carol@acme.com"])
        self.assertEqual(first["limits"][0]["daily"], 100)
        self.assertEqual(first["limits"][0]["version"], moment.strftime("%Y-%m-%d %H:%M:%S.%f"))
        self.assertEqual((third["limits"], third["next_version"]), ([], second["next_version"]))

        frappe.set_user("Administrator")
        accounts_doc = frappe.get_doc("Mail Account", "bob@acme.com")
        accounts_doc.monthly_send_limit = 500
        accounts_doc.save()
        changed = self.pull(second["next_version"])["limits"]
        self.assertEqual([(e["account"], e["monthly"]) for e in changed], [("bob@acme.com", 500)])

    def test_a_save_is_served_once_it_settled(self) -> None:
        with patch.object(quota_api, "SETTLE_SECONDS", 60):
            self.assertEqual(self.pull()["limits"], [])
        self.assertEqual(len(self.pull()["limits"]), 3)

    def test_only_one_active_node_of_the_cluster_may_pull(self) -> None:
        self.as_agent()
        for host_ip in ("198.51.100.1", "not-an-ip"):
            self.assertRaises((frappe.PermissionError, InvalidInputError), quota_api.limits, host_ip)
        self.assertRaisesRegex(InvalidInputError, "since_version", quota_api.limits, NODE_IP, "2026")

        frappe.set_user("Administrator")
        other = make_cluster("c2", hostname=f"mail.c2.{ROOT_DOMAIN}")
        self.as_agent(other.name)  # another cluster's Agent naming this cluster's node
        self.assertRaisesRegex(frappe.PermissionError, "Agent's cluster", quota_api.limits, NODE_IP)

        frappe.set_user("Administrator")
        self.node.db_set("status", "Provisioned")
        self.assertRaises(frappe.PermissionError, self.pull)

    def test_only_agents_may_call(self) -> None:
        frappe.set_user("Administrator")
        self.assertRaises(frappe.PermissionError, quota_api.limits, NODE_IP)
        self.assertRaises(frappe.PermissionError, quota_api.usage, NODE_IP, [])


class TestUsageApi(SendingLimitsTestCase):
    def push(self, *records: tuple) -> dict:
        """``(account, period, sent)`` or ``(account, period, sent, refused)`` each."""

        self.as_agent()
        keys = ("account", "period", "sent", "refused")
        return quota_api.usage(NODE_IP, [dict(zip(keys, record, strict=False)) for record in records])

    def sent(self, account: str, period: str) -> int | None:
        return frappe.db.get_value("Mail Send Usage", f"{account}:{period}", "sent")

    def test_the_higher_count_of_two_agents_is_kept(self) -> None:
        day, month = current_periods()
        self.assertEqual(self.push(("alice@acme.com", day, 5), ("alice@acme.com", month, 40))["accepted"], 2)
        self.push(("alice@acme.com", day, 3))  # another node read the counter earlier
        self.assertEqual(self.sent("alice@acme.com", day), 5)
        self.push(("alice@acme.com", day, 7))
        self.assertEqual((self.sent("alice@acme.com", day), self.sent("alice@acme.com", month)), (7, 40))

        # Refusals are kept the same way, and an Agent that sends none leaves them as they are.
        self.push(("alice@acme.com", day, 7, 2))
        self.push(("alice@acme.com", day, 7, 1), ("alice@acme.com", day, 7))
        self.assertEqual(frappe.db.get_value("Mail Send Usage", f"alice@acme.com:{day}", "refused"), 2)

    def test_unknown_accounts_and_old_periods_are_dropped(self) -> None:
        day, _month = current_periods()
        old_day, _old_month = periods_at(datetime.now(UTC) - timedelta(days=3))
        answer = self.push(
            ("ALICE@acme.com", day, 1), ("ghost@acme.com", day, 1), ("bob@acme.com", old_day, 1)
        )
        self.assertEqual(answer["accepted"], 1)
        self.assertEqual(self.sent("alice@acme.com", day), 1)
        self.assertIsNone(self.sent("bob@acme.com", old_day))
        self.assertRaises(InvalidInputError, self.push, ("bob@acme.com", day, -1))
        self.assertRaises(InvalidInputError, self.push, ("bob@acme.com", day, 1, -1))

    def test_form_and_api_show_the_usage_and_delete_takes_it(self) -> None:
        day, month = current_periods()
        self.push(("alice@acme.com", day, 80, 3), ("alice@acme.com", month, 900, 5))
        usage = {"day": 80, "month": 900, "refused_day": 3, "refused_month": 5}
        frappe.set_user("Administrator")
        doc = frappe.get_doc("Mail Account", "alice@acme.com")
        doc.run_method("onload")
        self.assertEqual(doc.get_onload().send_usage, usage)

        self.act_as(self.tenant)
        payload = accounts.get_account("alice@acme.com")
        self.assertEqual(
            (payload["daily_send_limit"], payload["monthly_send_limit"], payload["send_usage"]),
            (100, 0, usage),
        )
        listed = {item["email"]: item for item in accounts.list_accounts()["items"]}
        self.assertEqual(listed["alice@acme.com"]["send_usage"], usage)
        self.assertEqual(listed["bob@acme.com"]["send_usage"], dict.fromkeys(usage, 0))

        accounts.delete_account("alice@acme.com")
        self.assertFalse(frappe.db.exists("Mail Send Usage", {"account": "alice@acme.com"}))

    def test_prune_keeps_the_days_and_months_of_the_retention(self) -> None:
        now = datetime.now(UTC)
        inside, outside, old = (now - timedelta(days=days) for days in (29, 31, 70))
        rows = [*current_periods(), *periods_at(inside), *periods_at(outside), *periods_at(old)]
        upsert_usage(
            [{"account": "alice@acme.com", "period": period, "sent": 1, "refused": 0} for period in set(rows)]
        )
        with patch(f"{mail_send_usage.__name__}.get_config", return_value=30):
            prune_usage()
        kept = set(frappe.get_all("Mail Send Usage", {"account": "alice@acme.com"}, pluck="period"))
        # The month of the 31st day back is kept while the retention still reaches into it.
        month_at_cutoff = periods_at(now - timedelta(days=30))[1]
        self.assertEqual(kept, {*current_periods(), *periods_at(inside), month_at_cutoff})

    def test_a_retention_under_a_day_falls_back_to_the_default(self) -> None:
        for days in (0, -5):
            with patch(f"{mail_send_usage.__name__}.get_config", return_value=days):
                self.assertEqual(mail_send_usage.retention_days(), mail_send_usage.DEFAULT_RETENTION_DAYS)


class TestTenantSendLimits(SendingLimitsTestCase):
    def test_tenants_set_and_lift_the_limits(self) -> None:
        updated = accounts.update_account("bob@acme.com", monthly_send_limit=2000)
        self.assertEqual((updated["daily_send_limit"], updated["monthly_send_limit"]), (100, 2000))
        updated = accounts.update_account("bob@acme.com", daily_send_limit=0)
        self.assertEqual((updated["daily_send_limit"], updated["monthly_send_limit"]), (0, 2000))
        self.assertRaises(
            frappe.ValidationError, accounts.update_account, "bob@acme.com", daily_send_limit=-1
        )


class TestQuotaPlan(SendingLimitsTestCase):
    def test_the_plan_carries_the_hook_once_the_agents_answer_it(self) -> None:
        objects = lambda operations: {op["object"] for op in operations}  # noqa: E731
        with patch("suite_cloud.telemetry.agent.deploy.is_quota_enabled", return_value=False):
            self.assertFalse({"MtaHook", "MtaInboundThrottle"} & objects(plan.cluster_plan(self.cluster)))

        with patch("suite_cloud.telemetry.agent.deploy.is_quota_enabled", return_value=True):
            operations = plan.cluster_plan(self.cluster)
            recovery = plan.recovery_plan(self.cluster)
        hook = next(op for op in operations if op["object"] == "MtaHook")["value"]["quota"]
        self.assertEqual(hook["url"], "http://127.0.0.1:8095/hooks/quota")
        self.assertEqual(
            (hook["stages"], hook["timeout"], hook["tempFailOnError"]), ({"data": True}, 2000, False)
        )
        self.assertEqual(
            hook["httpAuth"]["bearerToken"]["secret"], self.cluster.get_password("quota_hook_token")
        )
        self.assertIn(self.cluster.get_password("quota_hook_token"), plan.secret_strings(operations))

        throttle = next(op for op in operations if op["object"] == "MtaInboundThrottle")["value"][
            "quota-backstop"
        ]
        # Only signed-in sessions: unauthenticated ones would share one bucket otherwise.
        self.assertEqual(throttle["match"], quota.SIGNED_IN)
        self.assertEqual(
            (throttle["enable"], throttle["key"], throttle["rate"]),
            (True, {"authenticatedAs": True}, {"count": 5000, "period": 86_400_000}),
        )

        # The cap is the cluster's; 0 turns the throttle off rather than leaving it behind.
        self.cluster.db_set("quota_backstop_per_day", 0)
        disabled = quota.backstop_operation(self.cluster)["value"]["quota-backstop"]
        self.assertEqual((disabled["enable"], disabled["rate"]["count"]), (False, 1))
        self.cluster.db_set("quota_backstop_per_day", 20000)
        self.assertEqual(
            quota.backstop_operation(self.cluster)["value"]["quota-backstop"]["rate"]["count"], 20000
        )

        # Stalwart adds its default throttles only when none exist, so recovery leaves ours out.
        self.assertIn("MtaHook", objects(recovery))
        self.assertNotIn("MtaInboundThrottle", objects(recovery))

    def test_the_patch_gives_older_clusters_a_token_and_keeps_existing_ones(self) -> None:
        frappe.set_user("Administrator")
        token = self.cluster.get_password("quota_hook_token")
        self.assertEqual(len(token), 32)
        fill_quota_hook_tokens.execute()
        self.assertEqual(self.cluster.get_password("quota_hook_token"), token)

        frappe.db.delete(
            "__Auth",
            {"doctype": "Stalwart Cluster", "name": self.cluster.name, "fieldname": "quota_hook_token"},
        )
        fill_quota_hook_tokens.execute()
        self.assertNotIn(self.cluster.get_password("quota_hook_token", raise_exception=False), (None, token))
