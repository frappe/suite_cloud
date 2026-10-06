"""Runs the pinned Ansible for real, against this machine, through the code Server Jobs use."""

import os
import sys
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cloud_mail.tests.fixtures import configure_settings, make_cluster, make_node
from suite_cloud.provisioning.ansible import ping
from suite_cloud.suite_cloud.doctype.server_job.server_job import create_server_job

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
        self.enterContext(patch("suite_cloud.provisioning.ansible.inventory_line", local_inventory_line))

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_ad_hoc_ping_runs(self) -> None:
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


def local_inventory_line(alias: str, *args) -> str:
    """The node's inventory line with SSH swapped for a local connection, so no server is needed."""

    return f"{alias} ansible_connection=local ansible_python_interpreter={sys.executable}"
