from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cluster import dns, egress
from suite_cloud.stalwart import forget_sessions
from suite_cloud.tests.fake_stalwart import FakeStalwart
from suite_cloud.tests.fixtures import (
    activate_cluster,
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_node,
    make_site,
)


class TestEgress(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        self.cluster = activate_cluster(make_cluster())
        self.fake = FakeStalwart(
            base_url=self.cluster.base_url, admin_password=self.cluster.get_password("admin_password")
        )
        self.fake.add_token("test-token")
        self.fake.singletons["SystemSettings"] = {
            "mailExchangers": {"0": {"hostname": self.cluster.hostname, "priority": 10}}
        }
        self.fake.singletons["MtaOutboundStrategy"] = {
            "route": {
                "match": {"0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}},
                "else": "'mx'",
            }
        }
        self._install = self.fake.install()
        self._install.__enter__()
        self.addCleanup(self._install.__exit__, None, None, None)
        forget_sessions(self.cluster)
        clear_request_cache()
        self.site = make_site(self.cluster)
        self.gateway = frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "cluster": self.cluster.name,
                "hostname": f"out1.{self.cluster.default_domain}",
                "ipv4_address": "203.0.113.50",
            }
        ).insert()

    def tearDown(self) -> None:
        for doctype in ("Mail Domain",):
            for name in frappe.get_all(doctype, pluck="name"):
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_on_trash=True)
        frappe.db.set_value("Stalwart Cluster", self.cluster.name, "default_egress_pool", None)
        frappe.db.set_value("Suite Site", self.site.name, "egress_pool", None)
        for name in frappe.get_all("Egress IP Pool", pluck="name"):
            frappe.delete_doc(
                "Egress IP Pool", name, force=True, ignore_permissions=True, ignore_on_trash=True
            )
        frappe.db.delete("DNS Record", {"managed_by_doctype": ["in", ["Egress IP Pool", "Egress Gateway"]]})
        frappe.delete_doc(
            "Egress Gateway", self.gateway.name, force=True, ignore_permissions=True, ignore_on_trash=True
        )
        frappe.flags.do_not_enqueue = False

    def make_pool(self, name: str = "ded", ips: tuple[str, ...] = ("203.0.113.51",), **fields):
        pool = frappe.get_doc(
            {
                "doctype": "Egress IP Pool",
                "cluster": self.cluster.name,
                "pool_name": name,
                "addresses": [
                    {
                        "gateway": self.gateway.name,
                        "ip_address": ip,
                        "ehlo_hostname": f"{name}{i}.{self.cluster.default_domain}",
                    }
                    for i, ip in enumerate(ips, start=1)
                ],
                **fields,
            }
        )
        pool.insert()
        return pool

    def test_gateway_defaults_and_dns(self) -> None:
        self.assertEqual(self.gateway.status, "Pending")
        self.assertEqual(self.gateway.base_url, f"https://out1.{self.cluster.default_domain}")
        self.assertEqual(frappe.db.get_value("Stalwart Store", self.gateway.data_store, "type"), "RocksDb")
        self.assertEqual(len(self.gateway.get_password("admin_password")), 32)
        record = frappe.get_all(
            "DNS Record", {"managed_by": self.gateway.name}, ["host", "value", "category"]
        )
        self.assertEqual(
            [(r.host, r.value, r.category) for r in record], [("out1.blr", "203.0.113.50", "Egress")]
        )

    def test_pool_assigns_ports_hostnames_and_records(self) -> None:
        pool = self.make_pool("ded", ("203.0.113.51", "203.0.113.52"))
        second = self.make_pool("shared", ("203.0.113.53",))

        self.assertEqual((pool.relay_port, second.relay_port), (2525, 2526))
        self.assertEqual(pool.hostname, f"ded.out.{self.cluster.default_domain}")
        hosts = sorted(
            (r.host, r.value)
            for r in frappe.get_all("DNS Record", {"managed_by": pool.name}, ["host", "value"])
        )
        self.assertEqual(
            hosts,
            [("ded.out.blr", "203.0.113.50"), ("ded1.blr", "203.0.113.51"), ("ded2.blr", "203.0.113.52")],
        )
        spf = frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value")
        for ip in ("203.0.113.51", "203.0.113.52", "203.0.113.53"):
            self.assertIn(f"ip4:{ip}", spf)

        self.assertRaisesRegex(
            frappe.ValidationError, "already belongs", self.make_pool, "dup", ("203.0.113.51",)
        )
        self.assertRaisesRegex(frappe.ValidationError, "1-8 lowercase", self.make_pool, "TooLongName")

    def test_cluster_routes_follow_pool_assignment(self) -> None:
        pool = self.make_pool("ded")
        domain = frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": "acme.com", "site": self.site.name}
        ).insert()
        frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": "direct.com", "site": self.site.name}
        ).insert()

        # No assignment yet: only the pre-existing rule survives and no relay route exists.
        operations = egress.cluster_operations(self.cluster)
        self.assertEqual([op["object"] for op in operations], ["MtaOutboundStrategy"])
        self.assertEqual(
            operations[0]["value"]["route"]["match"],
            {"0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}},
        )

        domain.egress_pool = pool.name
        domain.save()
        operations = {op["object"]: op for op in egress.cluster_operations(self.cluster)}
        route = operations["MtaRoute"]["value"]["egress-ded"]
        self.assertEqual(
            (route["address"], route["port"], route["authUsername"]), (pool.hostname, 2525, "relay")
        )
        self.assertEqual(route["authSecret"]["secret"], self.cluster.get_password("relay_password"))
        rules = egress.expression_rules(operations["MtaOutboundStrategy"]["value"]["route"])
        self.assertEqual(rules[0], {"if": "sender_domain == 'acme.com'", "then": "'egress-ded'"})
        self.assertEqual(rules[1]["then"], "'local'")  # existing rule kept after ours
        # The save synced the running cluster: the fake now carries the relay route and rules.
        self.assertEqual(self.fake.find("MtaRoute", name="egress-ded")["port"], 2525)
        live_rules = egress.expression_rules(self.fake.singletons["MtaOutboundStrategy"]["route"])
        self.assertEqual([r["then"] for r in live_rules], ["'egress-ded'", "'local'"])

        # Cluster default pool pulls every unassigned domain in; site and domain overrides win.
        self.cluster.db_set("default_egress_pool", pool.name)
        frappe.clear_document_cache("Stalwart Cluster", self.cluster.name)
        grouped = egress.domains_by_pool(frappe.get_doc("Stalwart Cluster", self.cluster.name))
        self.assertEqual(grouped, {pool.name: ["acme.com", "direct.com"]})

        # Re-applying is idempotent: our rules are replaced, not stacked.
        egress.resync_cluster(frappe.get_doc("Stalwart Cluster", self.cluster.name))
        live_rules = egress.expression_rules(self.fake.singletons["MtaOutboundStrategy"]["route"])
        self.assertEqual(len([r for r in live_rules if r["then"].startswith("'egress-")]), 1)

    def test_gateway_plan(self) -> None:
        pool = self.make_pool("ded", ("203.0.113.51",))
        operations = {
            op["object"]: op
            for op in egress.gateway_plan(frappe.get_doc("Egress Gateway", self.gateway.name))
        }

        listener = operations["NetworkListener"]["value"]["relay-ded"]
        self.assertEqual(
            (listener["protocol"], listener["bind"], listener["useTls"]),
            ("smtp", {"0.0.0.0:2525": True}, True),
        )
        strategy = operations["MtaConnectionStrategy"]["value"]["ded"]
        self.assertEqual(
            strategy["sourceIps"],
            {"0": {"sourceIp": "203.0.113.51", "ehloHostname": f"ded1.{self.cluster.default_domain}"}},
        )
        self.assertEqual(
            operations["MtaOutboundStrategy"]["value"]["connection"],
            {"match": {"0": {"if": "received_via_port == 2525", "then": "'ded'"}}, "else": "'default'"},
        )
        role = operations["ClusterRole"]["value"]["gateway-role"]
        self.assertEqual(role["name"], "egress")
        self.assertEqual(role["listeners"], {"@type": "EnableAll"})  # the firewall limits exposure
        domain = operations["Domain"]["value"]["egress"]
        self.assertEqual(
            domain["certificateManagement"]["subjectAlternativeNames"],
            {self.gateway.hostname: True, f"*.out.{self.cluster.default_domain}": True},
        )
        relay = operations["Account"]["value"]["relay"]
        self.assertEqual(relay["credentials"]["0"]["secret"], self.cluster.get_password("relay_password"))
        self.assertEqual(operations["Coordinator"]["value"], {"@type": "Disabled"})

        variables = egress.build_gateway_variables({"gateway": self.gateway.name})
        self.assertEqual(variables["wait_ports"], [443, 2525])
        self.assertIn("STALWART_ROLE=egress", variables["env_normal"])
        self.assertIn('"@type":"RocksDb"', variables["bootstrap_ndjson"])
        self.assertIn(pool.pool_name, variables["cluster_ndjson"])

    def test_verify_ptr_marks_rows_one_or_all(self) -> None:
        pool = self.make_pool("ded", ("203.0.113.51", "203.0.113.52"))
        first, second = pool.addresses
        target = "suite_cloud.suite_cloud.doctype.egress_ip_pool.egress_ip_pool.verify_ptr_record"

        with patch(target, side_effect=lambda ip, host: ip == "203.0.113.51") as check:
            self.assertEqual(pool.verify_ptr(first.name), {"203.0.113.51": True})
            check.assert_called_once_with("203.0.113.51", first.ehlo_hostname)
            self.assertEqual(pool.verify_ptr(), {"203.0.113.51": True, "203.0.113.52": False})
        self.assertRaisesRegex(frappe.ValidationError, "not found", pool.verify_ptr, "missing")

        rows = frappe.get_all(
            "Egress IP Pool Address", {"parent": pool.name}, ["ip_address", "ptr_verified"], order_by="idx"
        )
        self.assertEqual(
            [(r.ip_address, r.ptr_verified) for r in rows], [("203.0.113.51", 1), ("203.0.113.52", 0)]
        )
        self.assertEqual(second.ptr_verified, 0)

    def test_pool_deletion_is_blocked_while_used(self) -> None:
        pool = self.make_pool("ded")
        self.site.db_set("egress_pool", pool.name)
        self.assertRaisesRegex(frappe.ValidationError, "still used", pool.delete)
        self.site.db_set("egress_pool", None)
        pool.delete()
        self.assertFalse(frappe.db.exists("DNS Record", {"managed_by": pool.name}))
        self.assertNotIn("ip4:203.0.113.51", frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value"))

    def test_spf_includes_nodes_and_pools(self) -> None:
        node = make_node(self.cluster, "n1", "203.0.113.10")
        node.db_set("status", "Active")
        self.make_pool("ded", ("203.0.113.51",))
        dns.sync_spf_record(self.cluster)
        self.assertEqual(
            frappe.db.get_value("DNS Record", {"host": "spf.blr"}, "value"),
            "v=spf1 ip4:203.0.113.10 ip4:203.0.113.51 -all",
        )
