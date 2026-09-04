import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cluster import dns, plan
from suite_cloud.tests.fixtures import (
    ROOT_DOMAIN,
    configure_settings,
    make_cluster,
    make_node,
    make_store,
    make_zone,
    no_dns_provider,
)


class TestStalwartCluster(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_cluster_derives_zone_url_and_coordinator(self) -> None:
        cluster = make_cluster()

        self.assertEqual(cluster.default_domain, f"blr.{ROOT_DOMAIN}")
        self.assertEqual(cluster.base_url, f"https://mail.blr.{ROOT_DOMAIN}")
        self.assertEqual(cluster.coordinator, "Default")
        self.assertEqual(cluster.status, "Pending")
        self.assertTrue(cluster.ssh_public_key.startswith("ssh-ed25519 "))
        self.assertEqual(len(cluster.get_password("admin_password")), 32)
        self.assertEqual(
            cluster.stalwart_version, frappe.db.get_single_value("Suite Cloud Settings", "stalwart_version")
        )

    def test_hostname_must_be_two_labels_under_root(self) -> None:
        store = make_store("Data", "PostgreSql", host="db", auth_secret="x")
        cluster = frappe.get_doc(
            {
                "doctype": "Stalwart Cluster",
                "cluster_name": "bad",
                "hostname": f"mail.{ROOT_DOMAIN}",
                "data_store": store.name,
            }
        )
        self.assertRaisesRegex(frappe.ValidationError, "two labels", cluster.insert)

    def test_store_kind_is_enforced(self) -> None:
        blob = make_store("Blob", "S3", region="r", bucket="b", access_key="a", secret_key="s")
        cluster = frappe.get_doc(
            {
                "doctype": "Stalwart Cluster",
                "cluster_name": "kind",
                "hostname": f"mail.kind.{ROOT_DOMAIN}",
                "data_store": blob.name,
            }
        )
        self.assertRaisesRegex(frappe.ValidationError, "Data store", cluster.insert)

    def test_embedded_stores_pin_the_cluster_to_one_full_node(self) -> None:
        cluster = make_cluster(name="solo", hostname=f"mail.solo.{ROOT_DOMAIN}", multi_node=False)
        self.assertEqual(cluster.single_node, 1)
        self.assertEqual(cluster.coordinator, "Disabled")
        self.assertRaisesRegex(
            frappe.ValidationError, "full role", make_node, cluster, "n1", "203.0.113.1", role="frontend"
        )
        make_node(cluster, "n1", "203.0.113.1")

        self.assertRaisesRegex(frappe.ValidationError, "one node", make_node, cluster, "n2", "203.0.113.2")

        # Redis on a single node is allowed but coordinates nothing.
        redis = make_store("In-Memory", "Redis", title="solo redis", url="redis://redis.example.test:6379")
        cluster.reload()  # the node insert re-saved it
        cluster.in_memory_store = redis.name
        cluster.save()
        self.assertEqual(cluster.coordinator, "Disabled")

    def test_node_hostname_must_sit_under_the_zone(self) -> None:
        cluster = make_cluster()
        node = frappe.get_doc(
            {
                "doctype": "Stalwart Node",
                "cluster": cluster.name,
                "hostname": f"n1.other.{ROOT_DOMAIN}",
                "ipv4_address": "203.0.113.1",
            }
        )
        self.assertRaisesRegex(frappe.ValidationError, "single label", node.insert)

    def test_node_creates_its_dns_records_and_spf_tracks_ips(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, "n1", "203.0.113.10", ipv6_address="2001:db8::10")

        records = frappe.get_all(
            "DNS Record", {"managed_by": node.name}, ["host", "type", "value", "category"], order_by="type"
        )
        self.assertEqual(
            [(r.host, r.type, r.value, r.category) for r in records],
            [("n1.blr", "A", "203.0.113.10", "Node"), ("n1.blr", "AAAA", "2001:db8::10", "Node")],
        )
        # Not yet in ingress: the cluster hostname does not point at a pending node.
        self.assertFalse(frappe.db.exists("DNS Record", {"host": "mail.blr", "managed_by": node.name}))

        node.db_set("status", "Active")
        dns.sync_node_records(node, include_ingress=True)
        dns.sync_spf_record(cluster)
        ingress = frappe.get_all("DNS Record", {"host": "mail.blr"}, pluck="value", order_by="type")
        self.assertEqual(ingress, ["203.0.113.10", "2001:db8::10"])
        spf = frappe.db.get_value("DNS Record", {"host": "spf.blr", "type": "TXT"}, "value")
        self.assertEqual(spf, "v=spf1 ip4:203.0.113.10 ip6:2001:db8::10 -all")

        dns.sync_node_records(node, include_ingress=False)
        self.assertFalse(frappe.db.exists("DNS Record", {"host": "mail.blr"}))
        self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "in_ingress_dns"), 0)

    def test_removing_a_node_updates_spf_and_frees_a_failed_bootstrap(self) -> None:
        from suite_cloud.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "n1", "203.0.113.10")
        node.db_set({"status": "Active", "is_bootstrap_node": 1})
        dns.sync_spf_record(cluster)
        self.assertIn("ip4:203.0.113.10", frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value"))

        bootstrap.drain_node(node)  # Draining nodes still send
        self.assertIn("ip4:203.0.113.10", frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value"))
        node.db_set("status", "Failed")
        cluster.db_set({"status": "Failed", "bootstrap_node": node.name})

        frappe.get_doc("Stalwart Node", node.name).delete()

        self.assertEqual(frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value"), "v=spf1 -all")
        self.assertEqual(
            frappe.db.get_value("Stalwart Cluster", cluster.name, ["status", "bootstrap_node"]),
            ("Pending", None),
        )

    def test_active_nodes_are_not_failed_by_transient_checks(self) -> None:
        from suite_cloud.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "n1", "203.0.113.10")
        node.db_set({"status": "Active", "provisioned_at": frappe.utils.add_to_date(None, hours=-5)})
        self.assertFalse(bootstrap._not_ready(node, "registry hiccup"))
        self.assertEqual(
            frappe.db.get_value("Stalwart Node", node.name, ["status", "last_error"]),
            ("Active", "registry hiccup"),
        )

        node.db_set("status", "Provisioned")
        self.assertFalse(bootstrap._not_ready(node, "still down"))
        self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "status"), "Failed")

    def test_dns_records_may_be_wanted_by_two_owners(self) -> None:
        from suite_cloud.suite_cloud.doctype.dns_record.dns_record import reconcile_managed_records

        cluster = make_cluster()
        node = make_node(cluster, "n1", "203.0.113.10")
        wanted = [
            {
                "dns_zone": ROOT_DOMAIN,
                "host": "n1.blr",
                "type": "A",
                "value": "203.0.113.10",
                "category": "Egress",
            }
        ]
        reconcile_managed_records("Stalwart Cluster", cluster.name, wanted)
        wanted_filters = {"dns_zone": ROOT_DOMAIN, "host": "n1.blr", "type": "A"}
        self.assertEqual(frappe.db.count("DNS Record", wanted_filters), 2)

        with patch("suite_cloud.suite_cloud.doctype.dns_record.dns_record.get_dns_provider") as provider:
            provider.return_value.delete_dns_record.return_value = True
            reconcile_managed_records("Stalwart Cluster", cluster.name, [])
            provider.return_value.delete_dns_record.assert_not_called()  # the node still wants it
            frappe.get_doc("Stalwart Node", node.name).delete()  # Pending nodes delete normally
        self.assertFalse(frappe.db.exists("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "n1.blr"}))

    def test_finish_bootstrap_and_key_rotation_through_the_fake(self) -> None:
        from suite_cloud.cluster import bootstrap
        from suite_cloud.stalwart import forget_sessions
        from suite_cloud.tests.fake_stalwart import FakeStalwart
        from suite_cloud.tests.fixtures import clear_request_cache

        cluster = make_cluster()
        node = make_node(cluster, "n1", "203.0.113.10")
        fake = FakeStalwart(base_url=cluster.base_url, admin_password=cluster.get_password("admin_password"))
        fake.objects["Certificate"]["cert1"] = {
            "id": "cert1",
            "subjectAlternativeNames": {cluster.hostname: True},
        }
        with fake.install():
            forget_sessions(cluster)
            clear_request_cache()
            job = frappe.get_doc(
                {
                    "doctype": "Server Job",
                    "title": "x",
                    "server_doctype": "Stalwart Node",
                    "server": node.name,
                    "playbook": "run-commands.yml",
                }
            )
            with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.enqueue"):
                job.insert()
            bootstrap.provision_node(node)
            node.reload()

            # No registry lease yet: a Redis-coordinated cluster keeps waiting (nothing fails).
            bootstrap.after_provision(node, job)
            self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Bootstrapping")

            fake.add_cluster_node(node.hostname, node_id=7)
            self.assertTrue(bootstrap.finish_bootstrap(frappe.get_doc("Stalwart Cluster", cluster.name)))

            cluster.reload()
            node.reload()
            self.assertEqual((cluster.status, node.status, node.node_id), ("Active", "Active", 7))
            self.assertTrue(frappe.db.exists("DNS Record", {"host": "mail.blr", "managed_by": node.name}))
            self.assertIn(cluster.get_password("api_key"), fake.tokens)
            self.assertEqual(fake.singletons["SystemSettings"]["defaultCertificateId"], "cert1")
            self.assertEqual(len(fake.all("ApiKey:" + fake.admin_id)), 1)

            # The minted key works for management calls and rotation revokes the old one.
            old_key = cluster.get_password("api_key")
            clear_request_cache()
            self.assertEqual(cluster.get_client().cluster_nodes.find_by_hostname(node.hostname)["nodeId"], 7)
            cluster.rotate_api_key()
            self.assertNotEqual(cluster.get_password("api_key"), old_key)
            self.assertEqual(len(fake.all("ApiKey:" + fake.admin_id)), 1)

    def test_retried_bootstrap_recovers_a_failed_cluster(self) -> None:
        from suite_cloud.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "n1", "203.0.113.10")
        node.db_set({"is_bootstrap_node": 1, "status": "Provisioning"})
        cluster.db_set({"status": "Failed", "bootstrap_node": node.name})
        job = frappe._dict(retries=1, max_retries=1, error_log="boom")

        node.after_provision_failed(job)  # first failure keeps the cluster retryable
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Failed")

        with (
            patch("suite_cloud.cluster.bootstrap.check_node", return_value=False),
            patch("suite_cloud.cluster.dns.sync_node_records"),
            patch("suite_cloud.cluster.dns.sync_spf_record"),
        ):
            bootstrap.after_provision(node, job)
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Bootstrapping")

        node.after_provision_failed(frappe._dict(retries=2, max_retries=1, error_log="boom"))
        self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Failed")

    def test_outbound_nodes_stay_out_of_ingress(self) -> None:
        from suite_cloud.cluster import bootstrap

        cluster = make_cluster()
        node = make_node(cluster, "mta1", "203.0.113.20", role="outbound")
        self.assertFalse(bootstrap.serves_clients(node))
        self.assertEqual(bootstrap.build_node_variables({"node": node.name})["wait_ports"], [])
        bootstrap.activate_node(node)
        self.assertFalse(frappe.db.exists("DNS Record", {"host": "mail.blr", "managed_by": node.name}))
        self.assertIn("ip4:203.0.113.20", frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value"))
        self.assertRaisesRegex(frappe.ValidationError, "must serve clients", bootstrap.provision_node, node)

    def test_bootstrap_and_cluster_plans(self) -> None:
        cluster = make_cluster()

        bootstrap = plan.bootstrap_plan(cluster)[0]
        self.assertEqual(bootstrap["object"], "Bootstrap")
        value = bootstrap["value"]
        self.assertEqual(value["serverHostname"], f"mail.blr.{ROOT_DOMAIN}")
        self.assertEqual(value["defaultDomain"], f"blr.{ROOT_DOMAIN}")
        self.assertEqual(value["dataStore"]["@type"], "PostgreSql")
        self.assertEqual(value["dataStore"]["authSecret"], {"@type": "Value", "secret": "pg-secret"})
        self.assertEqual(value["blobStore"]["@type"], "S3")
        self.assertEqual(value["inMemoryStore"]["@type"], "Redis")
        self.assertEqual(value["searchStore"], {"@type": "Default"})
        self.assertFalse(value["requestTlsCertificate"])

        operations = {op["object"]: op for op in plan.cluster_plan(cluster)}
        self.assertEqual(operations["Coordinator"]["value"], {"@type": "Default"})
        self.assertEqual(set(operations["ClusterRole"]["value"]), {"full", "frontend", "outbound"})
        self.assertNotIn("DnsServer", operations)  # no DNS provider configured in tests
        self.assertEqual(
            operations["ClusterRole"]["value"]["frontend"]["tasks"]["taskTypes"], {"outboundMta": True}
        )
        self.assertEqual(operations["AcmeProvider"]["matchOn"], ["directory"])
        self.assertNotIn("description", operations["AcmeProvider"]["value"]["acme"])
        domain = operations["Domain"]["value"]["default"]
        self.assertEqual(
            domain["certificateManagement"]["subjectAlternativeNames"],
            {cluster.hostname: True, f"*.{cluster.default_domain}": True},
        )
        self.assertEqual(domain["dnsManagement"], {"@type": "Manual"})
        self.assertEqual(
            operations["SystemSettings"]["value"]["mailExchangers"],
            {"0": {"hostname": cluster.hostname, "priority": 10}},
        )
        self.assertEqual(operations["AcmeProvider"]["value"]["acme"]["contact"], {"ops@example.test": True})
        self.assertEqual(
            operations["Role"]["value"]["disabled"]["enabledPermissions"], {"emailReceive": True}
        )

        redacted = plan.redacted(plan.bootstrap_plan(cluster))
        self.assertNotIn("pg-secret", redacted)
        self.assertIn(plan.SECRET_MARKER, redacted)
        self.assertEqual(
            plan.to_ndjson(plan.cluster_plan(cluster)).count("\n"), len(plan.cluster_plan(cluster))
        )

    def test_node_env_and_config(self) -> None:
        cluster = make_cluster()
        node = make_node(cluster, "n1", role="frontend")

        self.assertEqual(
            plan.node_env(node),
            {
                "STALWART_HOSTNAME": node.hostname,
                "STALWART_PUBLIC_URL": cluster.base_url,
                "STALWART_ROLE": "frontend",
            },
        )
        recovery = plan.node_env(node, "recovery")
        self.assertEqual(recovery["STALWART_RECOVERY_MODE"], "1")
        self.assertTrue(recovery["STALWART_RECOVERY_ADMIN"].startswith("admin:"))
        self.assertNotIn("STALWART_RECOVERY_MODE", plan.node_env(node, "bootstrap"))
        self.assertEqual(json.loads(frappe.as_json(plan.node_config(cluster)))["@type"], "PostgreSql")
        self.assertIn("EnvironmentFile=/etc/stalwart/stalwart.env", plan.systemd_unit())

    def test_dns_server_object_maps_the_zone_provider(self) -> None:
        make_zone(dns_provider="Cloudflare", dns_provider_token="cf-token")
        with no_dns_provider():
            cluster = make_cluster()
        self.assertEqual(cluster.dns_zone, ROOT_DOMAIN)
        self.assertEqual(plan.dns_server_object(cluster)["@type"], "Cloudflare")
        self.assertEqual(plan.dns_server_object(cluster)["secret"], "cf-token")

        domain = {op["object"]: op for op in plan.cluster_plan(cluster)}["Domain"]["value"]["default"]
        self.assertEqual(domain["dnsManagement"]["@type"], "Automatic")
        self.assertEqual(domain["dnsManagement"]["origin"], ROOT_DOMAIN)
