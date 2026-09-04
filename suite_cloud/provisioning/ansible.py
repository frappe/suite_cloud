"""Runs Ansible playbooks for Server Jobs through ansible-runner, tracking progress per task."""

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import ansible_runner
import frappe
import yaml
from frappe import _
from frappe.utils import now, time_diff_in_seconds

from suite_cloud.provisioning.ssh import SSHTarget, inventory_line, private_key_file
from suite_cloud.utils import reconnect_on_failure

if TYPE_CHECKING:
    from suite_cloud.suite_cloud.doctype.server_job.server_job import ServerJob

PLAYBOOKS_DIR = os.path.join(os.path.dirname(__file__), "playbooks")
FINAL_TASK_STATUSES = ("Success", "Failed", "Unreachable", "Skipped")
RUNNER_ENV = {"ANSIBLE_HOST_KEY_CHECKING": "False", "ANSIBLE_RETRY_FILES_ENABLED": "False"}


def playbook_path(playbook: str) -> str:
    path = os.path.join(PLAYBOOKS_DIR, playbook)
    if not os.path.isfile(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(PLAYBOOKS_DIR):
        frappe.throw(_("Playbook {0} does not exist.").format(playbook))
    return path


def playbook_task_names(playbook: str) -> list[str]:
    """Task names in play order, following import_playbook and flattening blocks."""

    with open(playbook_path(playbook)) as f:
        plays = yaml.safe_load(f) or []

    names: list[str] = []
    for play in plays:
        if "import_playbook" in play:
            names.extend(playbook_task_names(play["import_playbook"]))
            continue
        names.extend(_task_names(play.get("tasks") or []))
    return names


def _task_names(tasks: list[dict]) -> list[str]:
    names = []
    for task in tasks:
        if "block" in task:
            names.extend(_task_names(task["block"]))
            names.extend(_task_names(task.get("rescue") or []))
            names.extend(_task_names(task.get("always") or []))
        elif task.get("name"):
            names.append(task["name"])
    return names


@dataclass
class RunOutcome:
    status: str
    stats: dict = field(default_factory=dict)
    error_log: str | None = None


@contextmanager
def private_data_dir() -> Iterator[str]:
    """ansible-runner writes extravars (our secrets) into its data dir; it must not outlive the run."""

    path = tempfile.mkdtemp(prefix="suite-cloud-run-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def ping(target: SSHTarget) -> tuple[bool, str]:
    """Checks SSH access with Ansible's ping module (no playbook needed)."""

    with private_key_file(target.private_key) as key_path, private_data_dir() as data_dir:
        runner = ansible_runner.run(
            private_data_dir=data_dir,
            module="ping",
            host_pattern="all",
            inventory=inventory_line("target", target, key_path),
            envvars=RUNNER_ENV,
            quiet=True,
        )
        ok = runner.rc == 0 and runner.status == "successful"
        detail = "" if ok else _runner_detail(runner)  # the artifacts vanish with the directory
    return ok, detail


class PlaybookRun:
    """One execution of a Server Job's playbook; events update the job's task rows."""

    def __init__(self, job: ServerJob, variables: dict[str, Any]) -> None:
        self.job = job
        # Literal secrets the playbook may echo (a CLI error quoting the plan, say); masked
        # before anything is stored. Longest first so a secret containing another is caught whole.
        self.secrets = sorted({s for s in variables.pop("__secret_values__", []) if s}, key=len, reverse=True)
        self.variables = variables
        self.tasks = {row.task: row.name for row in job.tasks}
        self.total = len(job.tasks)
        self.done = 0

    def run(self) -> RunOutcome:
        server = self.job.get_server()
        target = server.ssh_target()
        alias = server.name
        stats: dict = {}
        with private_key_file(target.private_key) as key_path, private_data_dir() as data_dir:
            runner = ansible_runner.run(
                private_data_dir=data_dir,
                playbook=playbook_path(self.job.playbook),
                inventory=inventory_line(alias, target, key_path),
                extravars=self.variables,
                envvars=RUNNER_ENV,
                event_handler=self.handle_event,
                quiet=True,
            )
            for event in getattr(runner, "events", []) or []:
                if event.get("event") == "playbook_on_stats":
                    stats = _host_stats(event.get("event_data") or {}, alias)
            succeeded = runner.rc == 0 and not stats.get("failures") and not stats.get("unreachable")
            detail = None if succeeded else self.mask(_runner_detail(runner))

        if not succeeded:
            self._fail_pending_tasks()
        return RunOutcome(status="Success" if succeeded else "Failed", stats=stats, error_log=detail)

    def mask(self, text: str | None) -> str | None:
        if not text:
            return text
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    # --- events -----------------------------------------------------------------

    def handle_event(self, event: dict) -> bool:
        kind = event.get("event")
        data = event.get("event_data") or {}
        if kind == "playbook_on_task_start":
            self.update_task(data.get("task"), "Running")
        elif kind == "runner_on_ok":
            self.update_task(data.get("task"), "Success", data)
        elif kind == "runner_on_failed":
            self.update_task(data.get("task"), "Skipped" if data.get("ignore_errors") else "Failed", data)
        elif kind == "runner_on_unreachable":
            self.update_task(data.get("task"), "Unreachable", data)
        elif kind == "runner_on_skipped":
            self.update_task(data.get("task"), "Skipped", data)
        return True

    @reconnect_on_failure()
    def update_task(self, task: str | None, status: str, data: dict | None = None) -> None:
        row_name = self.tasks.get(task or "")
        if not row_name:
            return

        values: dict[str, Any] = {"status": status}
        if status == "Running":
            values["started_at"] = now()
        else:
            result = dict((data or {}).get("res") or {})
            values.update(
                {
                    "stdout": self.mask(_text(result.pop("stdout", None))),
                    "stderr": self.mask(_text(result.pop("stderr", None))),
                    "exception": self.mask(_text(result.pop("msg", None))),
                }
            )
            for noisy in ("stdout_lines", "stderr_lines", "invocation"):
                result.pop(noisy, None)
            values["result"] = self.mask(_result_json(result))
            started_at = frappe.db.get_value("Server Job Task", row_name, "started_at")
            ended_at = now()
            values["ended_at"] = ended_at
            values["duration"] = time_diff_in_seconds(ended_at, started_at or ended_at)
            self.done += 1

        frappe.db.set_value("Server Job Task", row_name, values, update_modified=False)
        if not frappe.in_test:
            frappe.db.commit()
        frappe.publish_realtime(
            "server_job_progress",
            {"job": self.job.name, "task": task, "progress": self.done, "total": self.total},
            doctype="Server Job",
            docname=self.job.name,
        )

    def _fail_pending_tasks(self) -> None:
        frappe.db.set_value(
            "Server Job Task",
            {"parent": self.job.name, "status": ["in", ["Pending", "Running"]]},
            "status",
            "Failed",
            update_modified=False,
        )


def _host_stats(event_data: dict, alias: str) -> dict:
    stats = {}
    for key in ("ok", "changed", "failures", "skipped", "processed", "rescued", "ignored"):
        stats[key] = (event_data.get(key) or {}).get(alias, 0)
    stats["unreachable"] = (event_data.get("dark") or {}).get(alias, 0)
    return stats


def _runner_detail(runner: Any) -> str:
    parts = [f"status: {getattr(runner, 'status', None)}", f"rc: {getattr(runner, 'rc', None)}"]
    for attr in ("stdout", "stderr"):
        try:
            stream = getattr(runner, attr, None)
            text = stream.read() if hasattr(stream, "read") else stream
        except Exception as e:  # ansible-runner raises when the artifact file is gone
            text = f"({attr} unavailable: {e})"
        if text:
            parts.append(f"{attr}:\n{_text(text)[-4000:]}")
    return "\n".join(parts)


def _result_json(result: dict, limit: int = 20000) -> str:
    """The task result as JSON that fits the column; cutting the string would corrupt it."""

    text = json.dumps(result, indent=2, default=str)
    if len(text) <= limit:
        return text
    # Loops are the usual culprit: keep the summary, drop the per-item detail.
    slim = {k: v for k, v in result.items() if k != "results"}
    slim["truncated"] = True
    text = json.dumps(slim, indent=2, default=str)
    if len(text) <= limit:
        return text
    return json.dumps({"truncated": True, "msg": str(result.get("msg", ""))[:2000]}, indent=2)


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, list | tuple):
        return "\n".join(str(v) for v in value)
    return str(value)
