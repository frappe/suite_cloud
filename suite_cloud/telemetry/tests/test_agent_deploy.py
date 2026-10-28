import json
from unittest.mock import patch

import frappe

from suite_cloud.mail.cluster import bootstrap, egress
from suite_cloud.mail.tests.fixtures import configure_settings, error_log_count, make_cluster, make_node
from suite_cloud.provisioning.ansible import playbook_task_names
from suite_cloud.telemetry.agent import build, deploy, status
from suite_cloud.telemetry.agent.build import AgentBuild
from suite_cloud.telemetry.tests.test_agent import AgentBuildFixture
from suite_cloud.utils import clear_config_cache


class TestAgentDeploy(AgentBuildFixture):
    def setUp(self) -> None:
        super().setUp()
        frappe.flags.do_not_enqueue = True
        self.addCleanup(lambda: setattr(frappe.flags, "do_not_enqueue", False))
        configure_settings()
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 1
        settings.public_url = "https://cloud.example.test"
        settings.save()  # queues a roll-out at most, which runs after a commit a test never makes
        clear_config_cache()
        self.cluster = make_cluster()
        self.node = make_node(self.cluster)
        self.gateway = frappe.get_doc(
            {
                "doctype": "Egress Gateway",
                "cluster": self.cluster.name,
                "ipv4_address": "203.0.113.50",
                "ssh_host_keys": "203.0.113.50 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTests",
            }
        ).insert()
        self.addCleanup(
            lambda: frappe.delete_doc(
                "Egress Gateway", self.gateway.name, force=True, ignore_permissions=True, ignore_on_trash=True
            )
        )
        fixture = self

        class FixtureBuild(AgentBuild):
            def __init__(self, version: str | None = None) -> None:
                super().__init__(root=fixture.root, source=fixture.source, version=version)

        self.with_build = patch.object(deploy, "AgentBuild", FixtureBuild)
        self.with_build.start()
        self.addCleanup(self.with_build.stop)

    def test_agent_config_for_a_node_a_gateway_and_an_outbound_node(self) -> None:
        config = deploy.agent_config(self.node)
        self.assertEqual(config["suite_cloud"]["url"], "https://cloud.example.test")
        self.assertEqual(config["suite_cloud"]["secret"], self.cluster.agent_token)
        self.assertRegex(config["suite_cloud"]["secret"], r"^[0-9a-f]{20}:[0-9a-f]{40}$")
        self.assertEqual(
            (config["suite_cloud"]["requests_per_minute"], config["suite_cloud"]["records_per_request"]),
            (300, 5000),
        )
        self.assertEqual(config["suite_cloud"]["max_attempts"], 8)
        self.assertEqual(
            config["node"],
            {"source": "stalwart", "host_ip": self.node.ipv4_address, "hostname": self.node.hostname},
        )
        self.assertEqual(config["logs"][0]["path"], "/var/log/stalwart/stalwart.*")
        self.assertEqual(
            (config["logs"][0]["ship_existing"], config["logs"][0]["delete_after_days"]), (False, 0)
        )
        self.assertEqual(config["metrics"]["interval_seconds"], 60)
        stalwart = config["metrics"]["stalwart"]
        self.assertEqual(stalwart["url"], "https://127.0.0.1:443/metrics/prometheus")
        self.assertEqual(
            (stalwart["username"], stalwart["tls_server_name"]), ("telemetry", self.node.hostname)
        )
        self.assertEqual(stalwart["password"], self.cluster.get_password("metrics_password"))
        self.assertTrue(stalwart["verify_tls"])

        gateway_config = deploy.agent_config(self.gateway)
        self.assertEqual(gateway_config["node"]["host_ip"], "203.0.113.50")
        self.assertEqual(
            gateway_config["metrics"]["stalwart"]["password"], self.gateway.get_password("metrics_password")
        )

        outbound = make_node(self.cluster, ipv4="203.0.113.11", role="outbound")
        self.assertNotIn("stalwart", deploy.agent_config(outbound)["metrics"])

        # The cluster's switch turns the scrape off for its nodes and gateways alike.
        self.cluster.db_set("enable_metrics", 0)
        try:
            for server in (self.node, self.gateway):
                self.assertNotIn(
                    "stalwart", deploy.agent_config(frappe.get_doc(server.doctype, server.name))["metrics"]
                )
        finally:
            self.cluster.db_set("enable_metrics", 1)

    def test_agent_variables_follow_the_settings_and_the_build(self) -> None:
        self.assertFalse(deploy.agent_variables(self.node)["agent_enabled"])  # no build yet
        version = self.build().run()

        variables = deploy.agent_variables(self.node)
        self.assertTrue(variables["agent_enabled"])
        self.assertEqual(variables["agent_version"], version)
        self.assertEqual(
            variables["agent_binaries"],
            {arch: str(self.build().binary_path(arch)) for arch in ("x86_64", "aarch64")},
        )
        self.assertIn('"host_ip": "203.0.113.10"', variables["agent_config_json"])
        self.assertIn("User=stalwart", variables["agent_systemd_unit"])
        self.assertFalse(variables["agent_restart"])
        self.assertTrue(deploy.agent_variables(self.node, restart=True)["agent_restart"])
        self.assertIn(self.cluster.agent_token, variables["__secret_values__"])
        self.assertIn(self.cluster.get_password("metrics_password"), variables["__secret_values__"])

        # Sources change before the next build finishes: the nodes keep getting the last build.
        (self.source / "cmd" / "main.go").write_text("package main\n\nfunc main() {}\n")
        self.assertNotEqual(self.build().version, version)
        self.assertEqual(deploy.agent_variables(self.node)["agent_version"], version)
        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            self.assertIn(version, deploy.install_agent(self.node).title)

        # Off: the Agents stop, and each active cluster gets a push that switches the hook off.
        self.cluster.db_set("status", "Active")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        settings.save()
        clear_config_cache()
        self.assertFalse(deploy.agent_variables(self.node)["agent_enabled"])
        with (
            patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"),
            patch.object(deploy.push, "resync_cluster_after_commit") as resync,
        ):
            deploy.roll_out_agents()
        resync.assert_any_call(self.cluster.name)

    def test_the_quota_section_waits_for_a_build_that_answers_the_hook(self) -> None:
        self.build().run()  # 0.1.0 refuses a config with a quota section
        self.assertFalse(build.is_quota_enabled())
        self.assertNotIn("quota", deploy.agent_config(self.node))

        (self.source / "VERSION").write_text("0.2.0\n")
        self.build().run()
        self.assertTrue(build.is_quota_enabled())
        section = deploy.agent_config(self.node)["quota"]
        # The intervals are the agent's own defaults, so config.json leaves them out.
        self.assertEqual(set(section), {"hook_port", "hook_token", "store"})
        self.assertEqual(section["hook_port"], 8095)
        self.assertEqual(section["hook_token"], self.cluster.get_password("quota_hook_token"))
        self.assertEqual(section["store"], {"type": "redis", "urls": ["redis://redis.example.test:6379"]})
        self.assertNotIn("quota", deploy.agent_config(self.gateway))  # relays only, like an outbound node
        outbound = make_node(self.cluster, ipv4="203.0.113.11", role="outbound")
        self.assertNotIn("quota", deploy.agent_config(outbound))

        secrets = deploy.agent_variables(self.node)["__secret_values__"]
        self.assertIn(section["hook_token"], secrets)
        self.assertIn("redis://redis.example.test:6379", secrets)

    def test_the_quota_store_follows_the_in_memory_store(self) -> None:
        from suite_cloud.mail.cluster import quota
        from suite_cloud.mail.tests.fixtures import make_store

        single = frappe._dict(get_store=lambda field: None)
        self.assertEqual(quota.store_config(single), {"type": "memory"})

        sentinel = make_store(
            "In-Memory",
            "RedisSentinel",
            urls="redis://s1:26379\nredis://s2:26379",
            auth_username="stalwart",
            auth_secret="redis-pw",
            sentinel_secret="sentinel-pw",
        )
        config = quota.store_config(frappe._dict(get_store=lambda field: sentinel))
        self.assertEqual(
            config,
            {
                "type": "redis_sentinel",
                "urls": ["redis://s1:26379", "redis://s2:26379"],
                "username": "stalwart",
                "password": "redis-pw",
                "service_name": "mymaster",
                "sentinel_username": "",
                "sentinel_password": "sentinel-pw",
            },
        )
        self.assertEqual(
            set(quota.store_secrets(config)),
            {"redis://s1:26379", "redis://s2:26379", "redis-pw", "sentinel-pw"},
        )

    def test_nodes_and_gateways_show_their_agent_config_with_every_secret_masked(self) -> None:
        (self.source / "VERSION").write_text("0.2.0\n")
        self.build().run()
        secrets = deploy.agent_secrets(deploy.agent_config(self.node))
        self.assertEqual(len(secrets), 4)  # token, metrics password, hook token, Redis URL

        shown = self.node.show_agent_config()
        text = json.dumps(shown["config"])
        for secret in secrets:
            self.assertNotIn(secret, text)
        config = shown["config"]
        self.assertEqual(config["suite_cloud"]["secret"], "***")
        self.assertEqual(config["quota"]["hook_token"], "***")
        self.assertEqual(config["node"]["host_ip"], self.node.ipv4_address)
        self.assertEqual(config["quota"]["hook_port"], 8095)
        self.assertTrue(shown["telemetry_enabled"])

        # A gateway's config has no quota section, so its secrets are the token and its metrics password.
        gateway = self.gateway.show_agent_config()["config"]
        self.assertNotIn("quota", gateway)
        self.assertEqual(
            (gateway["suite_cloud"]["secret"], gateway["metrics"]["stalwart"]["password"]), ("***", "***")
        )
        self.assertNotIn(self.gateway.get_password("metrics_password"), json.dumps(gateway))

        self.assertEqual(
            deploy.mask_config({"url": "redis://:pw@host"}, ["pw"]), {"url": "redis://:***@host"}
        )
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, "Administrator")
        self.assertRaises(frappe.PermissionError, self.node.show_agent_config)
        self.assertRaises(frappe.PermissionError, self.gateway.show_agent_config)

    def test_node_and_gateway_jobs_carry_the_agent_variables_as_secrets(self) -> None:
        self.build().run()
        for variables in (
            bootstrap.build_node_variables({"node": self.node.name}),
            egress.build_gateway_variables({"gateway": self.gateway.name}),
        ):
            self.assertIn("agent_config_json", variables["__secret_keys__"])
            self.assertIn(self.cluster.agent_token, variables["__secret_values__"])
            self.assertTrue(variables["agent_enabled"])
            self.assertIn(self.cluster.agent_token, variables["agent_config_json"])

        names = playbook_task_names("configure-node.yml")
        self.assertLess(names.index("Install the Stalwart binary"), names.index("Install the agent binary"))
        self.assertIn("Stop the agent while telemetry is off", playbook_task_names("upgrade-stalwart.yml"))

    def test_install_agent_jobs(self) -> None:
        self.assertRaisesRegex(frappe.ValidationError, "Build Agent", deploy.install_agent, self.node)
        version = self.build().run()

        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            job = deploy.install_agent(self.node)
            self.assertEqual(
                (job.playbook, job.callback, job.server), ("install-agent.yml", None, self.node.name)
            )
            self.assertIn(version, job.title)
            self.assertEqual(frappe.db.get_value("Stalwart Node", self.node.name, "status"), self.node.status)
            # A reinstall rewrites what differs and restarts the agent whatever its version.
            self.assertTrue(bootstrap.build_node_variables(json.loads(job.context))["agent_restart"])
            self.assertFalse(bootstrap.build_node_variables({"node": self.node.name})["agent_restart"])

            self.node.db_set("status", "Active")
            self.gateway.db_set("status", "Active")
            jobs = deploy.upgrade_agents(self.cluster)
            servers = {frappe.db.get_value("Server Job", name, "server") for name in jobs}
            self.assertEqual(servers, {self.node.name, self.gateway.name})

    def test_a_settings_change_is_rolled_out_once(self) -> None:
        self.node.db_set("status", "Active")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        settings.save()
        with (
            patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"),
            patch.object(deploy.push, "resync_cluster_after_commit"),
        ):
            self.assertTrue(deploy.roll_out_agents())
            # A second job queued for the same change, say by a quick second turn, finds it done.
            self.assertEqual(deploy.roll_out_agents(), [])

    def test_the_settings_save_leaves_the_roll_out_to_a_background_job(self) -> None:
        # Even with do_not_enqueue on, as in setUp: the roll-out reaches every server of the site.
        before = frappe.get_all("Server Job", pluck="name")
        settings = frappe.get_single("Suite Cloud Settings")
        settings.enable_telemetry = 0
        with patch("frappe.enqueue") as enqueue:
            settings.save()
        enqueue.assert_called_once_with(deploy.roll_out_agents, queue="long", enqueue_after_commit=True)
        self.assertEqual(frappe.get_all("Server Job", {"name": ["not in", before]}), [])

    def test_turning_telemetry_on_or_off_rolls_the_agents_out(self) -> None:
        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        draining = make_node(self.cluster, ipv4="203.0.113.12")
        draining.db_set("status", "Draining")
        make_node(self.cluster, ipv4="203.0.113.13").db_set("status", "Provisioned")
        servers = (self.node, draining, self.gateway)
        settings = frappe.get_single("Suite Cloud Settings")

        # Only this cluster's servers: the site may hold clusters of its own.
        ours = [
            name
            for doctype in ("Stalwart Node", "Egress Gateway")
            for name in frappe.get_all(doctype, {"cluster": self.cluster.name}, pluck="name")
        ]

        def jobs_after(enabled: bool) -> list[dict]:
            before = frappe.get_all("Server Job", pluck="name")
            settings.reload()
            settings.enable_telemetry = int(enabled)
            with patch("frappe.enqueue") as enqueue:
                settings.save()
                # The save only queues the roll-out; it runs here as a worker would, without playbooks.
                if any(call.args[:1] == (deploy.roll_out_agents,) for call in enqueue.call_args_list):
                    with (
                        patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"),
                        patch.object(deploy.push, "resync_cluster_after_commit"),
                    ):
                        deploy.roll_out_agents()
            filters = {"name": ["not in", before], "server": ["in", ours]}
            return frappe.get_all("Server Job", filters, ["server", "title"])

        # Off: the agents are stopped, build or not.
        jobs = jobs_after(False)
        self.assertEqual({job.title for job in jobs}, {f"Stop agent on {s.hostname}" for s in servers})

        # On without a build: an Error Log instead of jobs the play would refuse.
        errors = error_log_count()
        self.assertEqual(jobs_after(True), [])
        self.assertEqual(error_log_count(), errors + 1)

        version = self.build().run()
        jobs_after(False)
        jobs = jobs_after(True)
        self.assertEqual({job.server for job in jobs}, {s.name for s in servers})
        self.assertTrue(all(job.title.startswith(f"Install agent {version}") for job in jobs))

        # A save that leaves the switch as it is changes nothing.
        self.assertEqual(jobs_after(True), [])

    def test_each_cluster_has_its_own_agent_credentials(self) -> None:
        from frappe.utils.password import get_decrypted_password, set_encrypted_password

        from suite_cloud.patches.v1_0 import move_agent_auth_to_clusters

        self.assertEqual(len(self.cluster.agent_key), 20)
        self.assertEqual(self.gateway.get_cluster().agent_token, self.cluster.agent_token)

        # Older sites: the shared secret turns telemetry on and goes; clusters get credentials.
        settings = "Suite Cloud Settings"
        frappe.db.set_single_value(settings, "enable_telemetry", 0)
        set_encrypted_password(settings, settings, "shared-secret", "telemetry_secret")
        self.cluster.db_set("agent_key", None)
        frappe.db.delete(
            "__Auth", {"doctype": "Stalwart Cluster", "name": self.cluster.name, "fieldname": "agent_secret"}
        )
        move_agent_auth_to_clusters.execute()

        self.assertEqual(frappe.db.get_single_value(settings, "enable_telemetry"), 1)
        self.assertIsNone(
            get_decrypted_password(settings, settings, "telemetry_secret", raise_exception=False)
        )
        cluster = frappe.get_doc("Stalwart Cluster", self.cluster.name)
        self.assertRegex(cluster.agent_token, r"^[0-9a-f]{20}:[0-9a-f]{40}$")
        token = cluster.agent_token
        move_agent_auth_to_clusters.execute()  # a second run keeps them
        self.assertEqual(frappe.get_doc("Stalwart Cluster", self.cluster.name).agent_token, token)

    def test_agent_status_follows_the_last_batch_and_the_build(self) -> None:
        status.forget_contacts()
        self.addCleanup(status.forget_contacts)
        self.assertEqual(status.agent_status(self.node)["state"], "unknown")

        version = self.build().run()
        status.record_contact(self.node.ipv4_address, f"suite-cloud-agent/{version}")
        current = status.agent_status(self.node)
        self.assertEqual(
            (current["state"], current["version"], current["built_version"]), ("ok", version, version)
        )
        self.assertIn(version, current["message"])

        status.record_contact(self.gateway.ipv4_address, "suite-cloud-agent/0.0.9+00000000")
        self.assertEqual(status.agent_status(self.gateway)["state"], "outdated")

        stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2).isoformat()
        frappe.cache.hset(status.CACHE_KEY, self.node.ipv4_address, {"seen_at": stale, "version": version})
        self.assertEqual(status.agent_status(self.node)["state"], "silent")

        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        summary = status.agent_summary(self.cluster)
        self.assertEqual(summary["state"], "silent")
        self.assertEqual(
            summary["message"],
            f"Agents: 1 outdated ({self.gateway.hostname}), 1 silent ({self.node.hostname}).",
        )
        self.assertEqual(status.get_agent_status("Stalwart Node", self.node.name)["state"], "silent")
        self.assertEqual(status.get_agent_summary(self.cluster.name)["state"], "silent")
        self.assertRaises(
            frappe.ValidationError, status.get_agent_status, "Stalwart Cluster", self.cluster.name
        )
