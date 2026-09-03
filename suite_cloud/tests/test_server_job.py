from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cluster import bootstrap
from suite_cloud.provisioning.ansible import playbook_task_names
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job
from suite_cloud.tests.fixtures import configure_settings, make_cluster, make_node


def fake_runner(job_tasks: list[str], fail_task: str | None = None):
    """Mimics ansible_runner.run: fires task events for every task and returns a runner."""

    def run(**kwargs):
        handler = kwargs["event_handler"]
        alias = kwargs["inventory"].split(" ")[0]
        stats = {k: {alias: 0} for k in ("ok", "changed", "failures", "dark", "skipped")}
        for task in job_tasks:
            handler({"event": "playbook_on_task_start", "event_data": {"task": task}})
            if task == fail_task:
                handler(
                    {
                        "event": "runner_on_failed",
                        "event_data": {"task": task, "res": {"msg": "boom", "stderr": "err"}},
                    }
                )
                stats["failures"][alias] += 1
                break
            handler(
                {
                    "event": "runner_on_ok",
                    "event_data": {"task": task, "res": {"stdout": "done", "changed": True}},
                }
            )
            stats["ok"][alias] += 1
        events = [{"event": "playbook_on_stats", "event_data": stats}]
        return SimpleNamespace(
            rc=2 if fail_task else 0,
            status="failed" if fail_task else "successful",
            events=events,
            stdout="",
            stderr="",
        )

    return run


class TestServerJob(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        self.cluster = make_cluster()
        self.node = make_node(self.cluster)

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_playbook_task_names_follow_imports_and_blocks(self) -> None:
        names = playbook_task_names("bootstrap-cluster.yml")
        self.assertEqual(names[0], "Ensure a Debian-family host")  # from install-stalwart.yml
        self.assertIn("Install the Stalwart binary", names)  # inside a block
        self.assertIn("Apply the cluster plan", names)
        self.assertEqual(names[-1], "Wait for the listeners")

    def test_unknown_playbook_is_rejected(self) -> None:
        self.assertRaisesRegex(
            frappe.ValidationError, "does not exist", create_server_job, self.node, "../../hooks.py", "x"
        )

    def test_successful_run_records_tasks_and_fires_callback(self) -> None:
        tasks = playbook_task_names("run-commands.yml")
        with (
            patch("suite_cloud.provisioning.ansible.ansible_runner.run", fake_runner(tasks)),
            patch("suite_cloud.cluster.bootstrap.after_provision") as callback,
        ):
            job = create_server_job(
                self.node,
                "run-commands.yml",
                title="Run",
                context={"commands": ["echo hi"]},
                variables_builder="suite_cloud.cluster.bootstrap.build_command_variables",
                callback="after_provision",
            )

        job.reload()
        self.assertEqual(job.status, "Success")
        self.assertEqual(job.ok, 1)
        self.assertEqual([t.status for t in job.tasks], ["Success"])
        self.assertEqual(job.tasks[0].stdout, "done")
        self.assertIn('"commands"', job.variables)
        callback.assert_called_once()

    def test_failed_run_marks_remaining_tasks_and_counts_retry(self) -> None:
        tasks = playbook_task_names("configure-node.yml")
        with (
            patch(
                "suite_cloud.provisioning.ansible.ansible_runner.run",
                fake_runner(tasks, fail_task="Write config.json"),
            ),
            patch("suite_cloud.cluster.bootstrap.after_provision") as callback,
        ):
            job = create_server_job(
                self.node,
                "configure-node.yml",
                title="Configure",
                context={"node": self.node.name},
                variables_builder=bootstrap.NODE_VARIABLES_BUILDER,
                callback="after_provision",
            )

        job.reload()
        self.assertEqual(job.status, "Failed")
        self.assertEqual(job.retries, 1)
        failed = next(t for t in job.tasks if t.task == "Write config.json")
        self.assertEqual(failed.status, "Failed")
        self.assertEqual(failed.exception, "boom")
        self.assertEqual(job.tasks[-1].status, "Failed")  # never reached, failed by the runner wrap-up
        callback.assert_not_called()
        self.assertEqual(frappe.db.get_value("Stalwart Node", self.node.name, "status"), "Failed")

    def test_stale_running_job_can_be_retried(self) -> None:
        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            job = create_server_job(self.node, "run-commands.yml", title="Stuck")
        job.db_set({"status": "Running", "started_at": frappe.utils.add_to_date(None, hours=-2)})
        job.reload()
        self.assertTrue(job.is_stale())
        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.enqueue") as enqueue:
            job.retry()
        enqueue.assert_called_once()
        self.assertEqual(frappe.db.get_value("Server Job", job.name, "status"), "Pending")

    def test_variables_snapshot_redacts_secrets(self) -> None:
        variables = bootstrap.build_node_variables({"node": self.node.name})
        self.assertIn("admin_password", variables["__secret_keys__"])
        self.assertIn("STALWART_RECOVERY_ADMIN=admin:", variables["env_recovery"])
        self.assertNotIn("STALWART_RECOVERY", variables["env_normal"])
        self.assertIn('"@type": "PostgreSql"', variables["config_json"])
        self.assertEqual(variables["plan_marker"], ".suite-cloud-plan-v0")

    def test_provision_first_node_uses_bootstrap_playbook(self) -> None:
        with patch("suite_cloud.suite_cloud.doctype.server_job.server_job.ServerJob.execute"):
            job = bootstrap.provision_node(self.node)

        self.assertEqual(job.playbook, "bootstrap-cluster.yml")
        self.cluster.reload()
        self.node.reload()
        self.assertEqual(self.cluster.status, "Bootstrapping")
        self.assertEqual(self.cluster.bootstrap_node, self.node.name)
        self.assertEqual(self.cluster.config_version, 1)
        self.assertTrue(self.node.is_bootstrap_node)
        self.assertEqual(self.node.status, "Provisioning")

        second = make_node(self.cluster, "n2", "203.0.113.11")
        self.assertRaisesRegex(
            frappe.ValidationError, "still bootstrapping", bootstrap.provision_node, second
        )
