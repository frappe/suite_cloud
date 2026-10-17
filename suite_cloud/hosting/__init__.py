"""Suite sites hosted on Frappe Cloud servers that Suite Cloud creates through Central."""

import time

import frappe

from suite_cloud.utils import user_context

JOB_TIMEOUT = 1800
TASK_WAIT_SECONDS = 300  # how long one job waits on a Pilot task before the poller takes over
TASK_POLL_SECONDS = 5
STEP_SAVEPOINT = "suite_cloud_hosting_step"


def run_in_background(method: str, job_id: str, **kwargs) -> None:
    """One deduplicated job per object, as Administrator: a job enqueued from a Guest request
    would otherwise run as Guest. Tests run it inline."""

    if frappe.flags.do_not_enqueue:
        frappe.get_attr(method)(**kwargs)
        return
    with user_context("Administrator"):
        frappe.enqueue(
            method,
            queue="long",
            timeout=JOB_TIMEOUT,
            job_id=job_id,
            deduplicate=True,
            enqueue_after_commit=True,
            **kwargs,
        )


def commit() -> None:
    """Each finished step is kept: a later failure must not undo what Central or Pilot already did."""

    if not frappe.in_test:
        frappe.db.commit()


def begin_step() -> None:
    """A step that fails is undone alone, not the work before it (an insert that ran it inline)."""

    frappe.db.savepoint(STEP_SAVEPOINT)


def undo_step() -> None:
    frappe.db.rollback(save_point=STEP_SAVEPOINT)


def checkpoint() -> None:
    """Keeps what a step did so far: a later failure must not drop the record of an object that
    already exists on a server, or the retry would create it twice."""

    commit()
    begin_step()


def wait_for_task(pilot, task_id: str) -> dict:
    """The task once it finished, or as it stands after ``TASK_WAIT_SECONDS``."""

    from suite_cloud.hosting.central.pilot import is_finished

    checkpoint()  # no row lock is held while waiting
    deadline = time.monotonic() + TASK_WAIT_SECONDS
    task = pilot.get_task(task_id)
    while not is_finished(task) and time.monotonic() < deadline and not frappe.in_test:
        time.sleep(TASK_POLL_SECONDS)
        task = pilot.get_task(task_id)
    return task


def output_tail(pilot, task_id: str, lines: int = 20) -> str:
    output = pilot.task_output(task_id) or ""
    return "\n".join(output.strip().splitlines()[-lines:])
