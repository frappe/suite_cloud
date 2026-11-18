# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import external, plan
from suite_cloud.mail.stalwart import forget_sessions, is_external
from suite_cloud.mail.stalwart.connection import ConnectionInfo, JMAPConnection, SessionStore
from suite_cloud.mail.stalwart.errors import StalwartError
from suite_cloud.mail.tests.fake_stalwart import FakeStalwart
from suite_cloud.mail.tests.fixtures import configure_settings, make_cluster, make_node

BASE_URL = "https://stalwart.k8s.test"
JMAP_HOST = "stalwart.mail.svc.test"
RECOVERY_ADMIN = "admin"
RECOVERY_PASSWORD = "recovery-secret"
NODE_LOCAL = {"Tracer", "DnsResolver", "AcmeProvider", "WebHook", "Metrics", "MtaMilter", "MtaHook"}


def make_external_cluster(**fields):
    fields = {"base_url": BASE_URL, "jmap_hostname": JMAP_HOST, **fields}
    return make_cluster("k8s-1", multi_node=False, externally_managed=1, **fields)


class TestExternalCluster(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def fake_server(self) -> FakeStalwart:
        """A running server that only knows its recovery administrator, with the services a
        normal start writes."""

        fake = FakeStalwart(base_url=BASE_URL, admin_user=RECOVERY_ADMIN, admin_password=RECOVERY_PASSWORD)
        fake.singletons["SystemSettings"] = {
            "services": {"jmap": {"hostname": "stalwart-0", "cleartext": False}}
        }
        return fake

    def test_an_external_cluster_keeps_its_url_and_needs_no_ssh(self) -> None:
        cluster = make_external_cluster()
        self.assertTrue(is_external(cluster))
        self.assertEqual(cluster.base_url, BASE_URL)
        self.assertFalse(cluster.ssh_public_key)
        # The recovery administrator is usually "admin"; Suite Cloud's own account differs by default.
        self.assertEqual(cluster.admin_username, external.DEFAULT_ADMIN_USERNAME)

    def test_an_admin_username_set_by_hand_is_kept(self) -> None:
        self.assertEqual(make_external_cluster(admin_username="admin").admin_username, "admin")

    def test_the_base_url(self) -> None:
        self.assertRaises(frappe.ValidationError, make_external_cluster, base_url="stalwart:8080")
        self.assertRaises(frappe.ValidationError, make_external_cluster, base_url=f"{BASE_URL}/jmap")
        # Plain HTTP would carry the administrator's password and the API key in the clear...
        self.assertRaisesRegex(
            frappe.ValidationError, "https", make_external_cluster, base_url="http://stalwart.k8s.test:8080"
        )
        # ...except to a Kubernetes Service, whose traffic stays on the cluster network.
        for url in ("http://stalwart.mail.svc:8080", "http://stalwart.mail.svc.cluster.local:8080"):
            self.assertEqual(make_external_cluster(base_url=url).base_url, url)

    def test_the_plan_leaves_what_the_platform_owns(self) -> None:
        cluster = make_external_cluster()
        rendered = plan.cluster_plan(cluster)
        objects = {op["object"] for op in rendered}
        self.assertFalse(objects & NODE_LOCAL, objects & NODE_LOCAL)
        settings = next(op for op in rendered if op["object"] == "SystemSettings")["value"]
        self.assertEqual(settings["services/jmap"], {"hostname": JMAP_HOST, "cleartext": False})
        domain = next(op for op in rendered if op["object"] == "Domain")["value"]["default"]
        self.assertEqual(domain["certificateManagement"], {"@type": "Manual"})
        # The sending-limit backstop stays: it needs no token on the server.
        backstop = [op for op in rendered if op["object"] == "MtaInboundThrottle"]
        self.assertEqual(len(backstop), 1)

        # A cluster Suite Cloud installs still gets every node-local object.
        installed = make_cluster("blr-2", hostname="mail.blr2.example.test", multi_node=False)
        self.assertFalse(is_external(installed))
        installed_objects = {op["object"] for op in plan.cluster_plan(installed)}
        self.assertTrue(
            {"Tracer", "DnsResolver", "AcmeProvider", "MtaHook", "MtaMilter"} <= installed_objects
        )

    def test_an_external_cluster_takes_no_nodes(self) -> None:
        cluster = make_external_cluster()
        self.assertRaisesRegex(frappe.ValidationError, "externally managed", make_node, cluster)

    def test_a_pinned_connection_calls_the_base_url(self) -> None:
        session = {"apiUrl": "https://stalwart-0/jmap/", "capabilities": {}, "primaryAccounts": {}}
        store = SessionStore(get=lambda: session, set=lambda s: None, clear=lambda: None)
        pinned = JMAPConnection(ConnectionInfo(BASE_URL, token="t", pin_api_url=True), session_store=store)
        self.assertEqual(pinned.api_url, f"{BASE_URL}/jmap/")
        plain = JMAPConnection(ConnectionInfo(BASE_URL, token="t"), session_store=store)
        self.assertEqual(plain.api_url, "https://stalwart-0/jmap/")

    def test_adoption_mints_the_api_key_and_activates(self) -> None:
        cluster = make_external_cluster()
        fake = self.fake_server()
        with fake.install():
            result = external.adopt(cluster, RECOVERY_ADMIN, RECOVERY_PASSWORD)

            cluster.reload()
            self.assertEqual(cluster.status, "Active")
            self.assertFalse(cluster.last_config_sync_error)
            # The permanent administrator exists with its password and minted the key Suite Cloud
            # now signs in with.
            admin = fake.find("Account", name=external.DEFAULT_ADMIN_USERNAME)
            self.assertEqual(admin["credentials"]["0"]["secret"], cluster.get_password("admin_password"))
            key = cluster.get_password("api_key")
            self.assertEqual(fake.tokens[key], admin["id"])
            self.assertEqual([k["secret"] for k in fake.all(f"ApiKey:{admin['id']}")], [key])
            self.assertIn("admin", result["adopted"])  # the administrator's plan reference
            self.assertEqual(fake.singletons["SystemSettings"]["services"]["jmap"]["hostname"], JMAP_HOST)

            # The full push ran after the adoption plan.
            self.assertEqual(cluster.config_version, 2)
            forget_sessions(cluster)
            self.assertEqual(plan.drift_report(cluster)["differences"], [])

    def test_a_failed_adoption_keeps_an_active_cluster_in_service(self) -> None:
        cluster = make_external_cluster()
        cluster.db_set("status", "Active")
        with self.fake_server().install():
            self.assertRaises(StalwartError, external.adopt, cluster, RECOVERY_ADMIN, "mistyped")
        cluster.reload()
        self.assertEqual(cluster.status, "Active")
        self.assertTrue(cluster.last_config_sync_error)

    def test_a_failed_first_adoption_marks_the_cluster_failed(self) -> None:
        cluster = make_external_cluster()
        self.assertEqual(cluster.status, "Pending")
        with self.fake_server().install():
            self.assertRaises(StalwartError, external.adopt, cluster, RECOVERY_ADMIN, "mistyped")
        cluster.reload()
        self.assertEqual(cluster.status, "Failed")
        self.assertTrue(cluster.last_config_sync_error)

    def test_adoption_needs_an_external_cluster(self) -> None:
        installed = make_cluster("blr-3", hostname="mail.blr3.example.test", multi_node=False)
        self.assertRaises(frappe.ValidationError, external.adopt, installed, "admin", "x")


class TestDriftReport(IntegrationTestCase):
    def test_live_value_reads_a_json_pointer(self) -> None:
        live = {
            "defaultHostname": "mail.test",
            "services": {"jmap": {"hostname": JMAP_HOST, "cleartext": False}},
        }
        self.assertEqual(plan.live_value(live, "defaultHostname"), "mail.test")
        self.assertEqual(plan.live_value(live, "services/jmap"), {"hostname": JMAP_HOST, "cleartext": False})
        self.assertIsNone(plan.live_value(live, "services/imap"))
        self.assertIsNone(plan.live_value(live, "defaultHostname/name"))

        wanted = {"services/jmap": {"hostname": JMAP_HOST, "cleartext": False}}
        self.assertEqual(plan._drifted("SystemSettings", live, wanted), [])
        moved = {"services/jmap": {"hostname": "other.test", "cleartext": False}}
        self.assertEqual(
            [d["property"] for d in plan._drifted("SystemSettings", live, moved)], ["services/jmap"]
        )
