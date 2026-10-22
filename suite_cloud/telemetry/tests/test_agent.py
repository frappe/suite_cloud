import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.cluster import bootstrap, egress
from suite_cloud.mail.tests.fixtures import configure_settings, error_log_count, make_cluster, make_node
from suite_cloud.provisioning.ansible import playbook_task_names
from suite_cloud.telemetry.agent import build, deploy, status
from suite_cloud.telemetry.agent.build import ARCHITECTURES, AgentBuild, AgentBuildError
from suite_cloud.utils import clear_config_cache

# Stands in for `go build`: records the environment and arguments in the output file, or fails.
FAKE_GO = """#!/bin/sh
if [ -n "$FAKE_GO_FAIL" ]; then echo "boom: $GOARCH" >&2; exit 1; fi
args="$*"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
echo "GOOS=$GOOS GOARCH=$GOARCH CGO_ENABLED=$CGO_ENABLED $args" > "$out"
"""


class AgentBuildFixture(IntegrationTestCase):
    """A fake Go toolchain on PATH and a small source tree, so no test needs Go."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-build-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "go").write_text(FAKE_GO)
        (bin_dir / "go").chmod(stat.S_IRWXU)
        self.source = self.tmp / "agent"
        (self.source / "cmd").mkdir(parents=True)
        (self.source / "VERSION").write_text("0.1.0\n")
        (self.source / "go.mod").write_text("module example\n")
        (self.source / "cmd" / "main.go").write_text("package main\n")
        self.root = self.tmp / "private"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(clear_config_cache)
        self.built_before = frappe.db.get_single_value("Suite Cloud Settings", "agent_built_version")

    def build(self, **kwargs) -> AgentBuild:
        return AgentBuild(root=self.root, source=self.source, **kwargs)


class TestAgentBuild(AgentBuildFixture):
    def test_build_writes_both_binaries_with_the_version_baked_in(self) -> None:
        build = self.build()
        version = build.run()

        self.assertRegex(version, r"^0\.1\.0\+[0-9a-f]{8}$")
        self.assertTrue(build.is_built)
        for architecture, goarch in ARCHITECTURES.items():
            content = build.binary_path(architecture).read_text()
            self.assertIn(f"GOOS=linux GOARCH={goarch} CGO_ENABLED=0", content)
            self.assertIn(f"-X main.version={version}", content)
        self.assertEqual(frappe.db.get_single_value("Suite Cloud Settings", "agent_built_version"), version)
        self.assertFalse(frappe.db.get_single_value("Suite Cloud Settings", "agent_build_error"))
        self.assertEqual([p.name for p in self.root.iterdir() if not p.name.startswith(".")], [version])

    def test_a_failed_build_leaves_no_partial_directory_and_records_the_error(self) -> None:
        errors_before = error_log_count()
        with patch.dict(os.environ, {"FAKE_GO_FAIL": "1"}):
            self.assertRaisesRegex(AgentBuildError, "boom", self.build().run)

        self.assertEqual([p.name for p in self.root.iterdir()], [])
        self.assertIn("boom", frappe.db.get_single_value("Suite Cloud Settings", "agent_build_error"))
        self.assertEqual(error_log_count(), errors_before + 1)
        self.assertFalse(self.build().is_built)

    def test_the_version_follows_the_sources(self) -> None:
        first = self.build().version
        (self.source / "cmd" / "main.go").write_text("package main\n\nfunc main() {}\n")
        second = self.build().version
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("0.1.0+") and second.startswith("0.1.0+"))

    def test_prune_keeps_the_new_and_the_previous_build(self) -> None:
        for name in ("0.0.9+aaaaaaaa", "0.1.0+bbbbbbbb", ".gocache"):
            (self.root / name).mkdir()
        frappe.db.set_single_value("Suite Cloud Settings", "agent_built_version", "0.1.0+bbbbbbbb")
        clear_config_cache()

        version = self.build().run()
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, sorted([".gocache", "0.1.0+bbbbbbbb", version]))

    def test_a_second_build_is_not_queued_while_one_runs(self) -> None:
        with patch.object(build, "is_job_enqueued", return_value=True):
            self.assertFalse(build.enqueue_build())
        with (
            patch.object(build, "is_job_enqueued", return_value=False),
            patch.object(build.frappe, "enqueue") as enqueue,
        ):
            self.assertTrue(build.enqueue_build())
        self.assertEqual(enqueue.call_args.kwargs["job_id"], build.JOB_ID)

    def test_migrate_queues_a_build_only_when_outdated_and_go_exists(self) -> None:
        frappe.flags.in_test = False
        self.addCleanup(lambda: setattr(frappe.flags, "in_test", True))
        with (
            patch.object(build, "AgentBuild", lambda: self.build()),
            patch.object(build, "enqueue_build") as enqueue,
        ):
            build.build_if_outdated()
            enqueue.assert_called_once()

            enqueue.reset_mock()
            self.build().run()
            build.build_if_outdated()
            enqueue.assert_not_called()

            with patch.object(build, "find_go", return_value=None):
                (self.source / "VERSION").write_text("0.2.0\n")
                build.build_if_outdated()
                enqueue.assert_not_called()

    def test_build_agent_needs_a_toolchain(self) -> None:
        settings = frappe.get_single("Suite Cloud Settings")
        with patch.object(build, "find_go", return_value=None):
            self.assertRaisesRegex(frappe.ValidationError, "Go toolchain", settings.build_agent)
        with (
            patch.object(build, "enqueue_build", return_value=True),
            patch.object(build, "AgentBuild", lambda: self.build()),
        ):
            self.assertEqual(settings.build_agent()["queued"], True)


class TestAgentDeploy(AgentBuildFixture):
    def setUp(self) -> None:
        super().setUp()
        frappe.flags.do_not_enqueue = True
        self.addCleanup(lambda: setattr(frappe.flags, "do_not_enqueue", False))
        configure_settings()
        settings = frappe.get_single("Suite Cloud Settings")
        settings.telemetry_secret = "agent-secret"
        settings.public_url = "https://cloud.example.test"
        settings.save()
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
        self.assertEqual(config["suite_cloud"]["secret"], "agent-secret")
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
        self.assertIn("agent-secret", variables["__secret_values__"])
        self.assertIn(self.cluster.get_password("metrics_password"), variables["__secret_values__"])

        # Sources change before the next build finishes: the nodes keep getting the last build.
        (self.source / "cmd" / "main.go").write_text("package main\n\nfunc main() {}\n")
        self.assertNotEqual(self.build().version, version)
        self.assertEqual(deploy.agent_variables(self.node)["agent_version"], version)
        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            self.assertIn(version, deploy.install_agent(self.node).title)

        settings = frappe.get_single("Suite Cloud Settings")
        settings.telemetry_secret = ""
        settings.save()
        clear_config_cache()
        self.assertFalse(deploy.agent_variables(self.node)["agent_enabled"])

    def test_node_and_gateway_jobs_carry_the_agent_variables_as_secrets(self) -> None:
        self.build().run()
        for variables in (
            bootstrap.build_node_variables({"node": self.node.name}),
            egress.build_gateway_variables({"gateway": self.gateway.name}),
        ):
            self.assertIn("agent_config_json", variables["__secret_keys__"])
            self.assertIn("agent-secret", variables["__secret_values__"])
            self.assertTrue(variables["agent_enabled"])
            self.assertIn("agent-secret", variables["agent_config_json"])

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

    def test_a_changed_secret_rolls_the_agents_out(self) -> None:
        self.node.db_set("status", "Active")
        self.gateway.db_set("status", "Active")
        draining = make_node(self.cluster, ipv4="203.0.113.12")
        draining.db_set("status", "Draining")
        make_node(self.cluster, ipv4="203.0.113.13").db_set("status", "Provisioned")
        settings = frappe.get_single("Suite Cloud Settings")

        def jobs_after(secret: str) -> list[dict]:
            before = frappe.get_all("Server Job", pluck="name")
            settings.reload()
            settings.telemetry_secret = secret
            with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
                settings.save()
            self.assertTrue(settings.flags.telemetry_secret_changed)
            return frappe.get_all("Server Job", {"name": ["not in", before]}, ["server", "title"])

        # No build yet: an Error Log instead of jobs the play would refuse.
        errors = error_log_count()
        self.assertEqual(jobs_after("rotated-once"), [])
        self.assertEqual(error_log_count(), errors + 1)

        version = self.build().run()
        jobs = jobs_after("rotated-twice")
        self.assertEqual({job.server for job in jobs}, {self.node.name, draining.name, self.gateway.name})
        self.assertTrue(all(job.title.startswith(f"Install agent {version}") for job in jobs))

        # Cleared: the agents are stopped, build or not.
        jobs = jobs_after("")
        self.assertEqual(
            {job.title for job in jobs},
            {f"Stop agent on {s.hostname}" for s in (self.node, draining, self.gateway)},
        )

        # The form sends the saved secret masked: nothing happens.
        settings.reload()
        settings.save()
        self.assertFalse(settings.flags.telemetry_secret_changed)

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
