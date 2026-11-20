# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from suite_cloud.mail.cluster import push, quota

# What a new cluster starts with; the lowest is its default. An admin adds others as needed.
DEFAULT_DAILY_LIMITS = (50, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000)


class MailSendLimit(Document):
    """A daily send limit the accounts of one cluster may have. Stalwart enforces each with one
    inbound throttle (``suite_cloud.mail.cluster.quota``), which a Mail Account links to."""

    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        cluster: DF.Link
        daily_limit: DF.Int
        is_default: DF.Check
        title: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = f"{self.cluster}-{cint(self.daily_limit)}"

    def validate(self) -> None:
        self.title = _("{0} per day").format(f"{cint(self.daily_limit):,}")
        if cint(self.daily_limit) < 1:
            frappe.throw(_("Daily Limit must be at least 1."))
        default = get_default_limit(self.cluster)
        if not default:
            self.is_default = 1
        elif default == self.name and not self.is_default:
            frappe.throw(_("Mark another limit of the cluster as the default instead."))

    def on_update(self) -> None:
        """A new default changes which throttle takes the accounts without a key, and which
        accounts need one, so the cluster is pushed."""

        if not (self.is_default and self.has_value_changed("is_default")):
            return
        others = frappe.get_all(
            "Mail Send Limit",
            {"cluster": self.cluster, "is_default": 1, "name": ("!=", self.name)},
            pluck="name",
        )
        for name in others:
            frappe.db.set_value("Mail Send Limit", name, "is_default", 0)
        if others:
            push.resync_cluster_after_commit(self.cluster)

    def on_trash(self) -> None:
        if self.is_default and not self.flags.deleting_cluster:
            frappe.throw(
                _("The cluster's default limit cannot be deleted. Mark another as the default first.")
            )

    def after_delete(self) -> None:
        if not self.flags.deleting_cluster:
            push.resync_cluster_after_commit(self.cluster)  # it deletes the throttle

    @frappe.whitelist()
    def sync_throttle(self) -> bool:
        """Creates the limit's throttle on the cluster if it is missing; True when it did."""

        frappe.only_for("System Manager")
        cluster = frappe.get_doc("Stalwart Cluster", self.cluster)
        if cluster.status != "Active":
            frappe.throw(_("Only an active cluster can be synced."))
        return quota.ensure_throttle(cluster, self)


def get_default_limit(cluster: str) -> str | None:
    return frappe.db.get_value("Mail Send Limit", {"cluster": cluster, "is_default": 1})


def get_daily_limits(cluster: str) -> list[int]:
    return sorted(frappe.get_all("Mail Send Limit", {"cluster": cluster}, pluck="daily_limit"))


def find_send_limit(cluster: str, daily_limit: int, label: str) -> str:
    """The cluster's limit of ``daily_limit`` recipients a day; anything else is refused."""

    name = frappe.db.get_value("Mail Send Limit", {"cluster": cluster, "daily_limit": cint(daily_limit)})
    if not name:
        values = ", ".join(map(str, get_daily_limits(cluster)))
        frappe.throw(
            _("{0} must be one of {1}, not {2}.").format(label, values, daily_limit), frappe.ValidationError
        )
    return name


def create_default_limits(cluster: Document, extra: set[int] = frozenset()) -> None:
    """``DEFAULT_DAILY_LIMITS`` and ``extra``, unless the cluster has them. The first a cluster
    gets, the lowest, becomes its default."""

    wanted = {value for value in (*DEFAULT_DAILY_LIMITS, *extra) if value >= 1}
    existing = set(get_daily_limits(cluster.name))
    for value in sorted(wanted - existing):
        frappe.get_doc({"doctype": "Mail Send Limit", "cluster": cluster.name, "daily_limit": value}).insert(
            ignore_permissions=True
        )
