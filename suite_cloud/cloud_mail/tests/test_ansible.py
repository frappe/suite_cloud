"""Runs the pinned Ansible for real, against this machine, through the code Server Jobs use."""

import os
import shutil
import sys
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cloud_mail.tests.fixtures import configure_settings, make_cluster, make_node
from suite_cloud.provisioning.ansible import ping
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

INVENTORY_LINE = "suite_cloud.provisioning.ansible.inventory_line"
PLAYBOOK = """\
- name: Smoke
  hosts: all
  gather_facts: false
  tasks:
    - name: Say pong
      command: echo pong
"""


class TestPinnedAnsible(IntegrationTestCase):
    # The other tests fake ansible-runner. The pinned ansible-core does not list this Python for the
    # machine it runs on, so these run it: a break on the bench's side must not wait for production.

    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        self.node = make_node(make_cluster())
        self.enterContext(patch(INVENTORY_LINE, local_inventory_line(sys.executable)))

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_ad_hoc_ping_runs(self) -> None:
        self.assertEqual(ping(self.node.ssh_target()), (True, ""))

    def test_a_python_3_8_node_is_still_managed(self) -> None:
        # Ubuntu 20.04's Python, and the reason Ansible is held back: ansible-core 2.20+ refuses it.
        python = shutil.which("python3.8")
        if not python:
            if os.environ.get("CI"):
                self.fail("CI must provide a python3.8 for this check")
            self.skipTest("needs a python3.8 on PATH")

        with patch(INVENTORY_LINE, local_inventory_line(python)):
            self.assertEqual(ping(self.node.ssh_target()), (True, ""))

    def test_playbook_runs_and_reports_its_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as playbooks:
            with open(os.path.join(playbooks, "smoke.yml"), "w") as f:
                f.write(PLAYBOOK)
            with patch("suite_cloud.provisioning.ansible.PLAYBOOKS_DIR", playbooks):
                job = create_server_job(self.node, "smoke.yml", title="Smoke")

        job.reload()
        self.assertEqual((job.status, job.ok, job.failures, job.unreachable), ("Success", 1, 0, 0))
        self.assertEqual(
            [(task.task, task.status, task.stdout) for task in job.tasks], [("Say pong", "Success", "pong")]
        )


def local_inventory_line(python: str):
    """An inventory_line that swaps SSH for a local connection run by `python`: no server is needed."""

    def inventory_line(alias: str, *args) -> str:
        return f"{alias} ansible_connection=local ansible_python_interpreter={python}"

    return inventory_line
