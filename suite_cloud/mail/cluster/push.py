"""Queued config pushes: how a desk change reaches a cluster without the save waiting on it."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import now

from suite_cloud.mail.stalwart import has_credentials

if TYPE_CHECKING:
    from frappe.model.document import Document

PENDING_PUSH_TTL = 600  # seconds a queued push marks its cluster; a lost job blocks no longer
ERROR_LENGTH = 1000


def resync_cluster_after_commit(cluster_name: str) -> None:
    """How a desk change reaches the cluster: in a job after commit, so a remote failure or a push
    already running never fails the save. A failure shows on the cluster's form."""

    if frappe.flags.do_not_enqueue:
        resync_cluster(frappe.get_doc("Stalwart Cluster", cluster_name))
        return
    frappe.msgprint(
        _("The configuration of {0} is pushed in the background.").format(cluster_name), alert=True
    )
    frappe.db.after_commit.add(lambda: queue_cluster_push(cluster_name))


def queue_cluster_push(cluster: str) -> None:
    """One waiting push per cluster: one that has not started plans from fresh data, so it covers
    this change too, and a second would only hold a worker. One already running does not, so it
    clears the mark when it starts and the next change queues its successor."""

    if frappe.cache.set(pending_push_key(cluster), 1, nx=True, ex=PENDING_PUSH_TTL):
        frappe.enqueue(resync_cluster_job, cluster=cluster, queue="long")


def resync_cluster_job(cluster: str) -> None:
    """A queued push. It takes the cluster's push lock before it reads anything, in a fresh
    transaction, so it plans from all that was committed while it waited."""

    from suite_cloud.mail.doctype.stalwart_cluster.stalwart_cluster import push_lock

    frappe.cache.delete(pending_push_key(cluster))
    with recorded_failure(cluster), push_lock(cluster):
        if not frappe.in_test:
            frappe.db.commit()  # nothing written yet: this only starts a fresh snapshot
        doc = frappe.get_doc("Stalwart Cluster", cluster)
        if doc.status == "Active" and has_credentials(doc):
            doc.apply_config()


def resync_cluster(cluster: Document) -> None:
    if cluster.status == "Active" and has_credentials(cluster):
        cluster.push_config()


@contextmanager
def recorded_failure(cluster: str) -> Iterator[None]:
    """A queued push's failure is kept on the cluster, where its form shows it, and raised again
    for the job's log. The job owns its transaction, so its own work is rolled back first."""

    try:
        yield
    except Exception as error:
        if not frappe.in_test:
            frappe.db.rollback()
        message = f"{now()}: {error}"[:ERROR_LENGTH]
        frappe.db.set_value(
            "Stalwart Cluster", cluster, "last_config_sync_error", message, update_modified=False
        )
        if not frappe.in_test:
            frappe.db.commit()
        raise


def pending_push_key(cluster: str) -> str:
    return f"{frappe.local.site}:suite_cloud:push-pending:{cluster}"
