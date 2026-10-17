"""A Suite Tenant Request's steps, read from the records the request, its site and mail domain keep.

A step is done when its record says so (the site's ``virtual_machine``, the mail domain verified),
so a retry, an undone step or the site domain running alongside mail needs no bookkeeping of its
own. ``sync`` saves what changed and tells the desk.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

import frappe
from frappe import _
from frappe.utils import get_datetime, now, time_diff_in_seconds

DONE = ("Completed", "Skipped")
EVENT = "suite_tenant_request_progress"


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    after: tuple[str, ...]
    is_done: Callable[[RequestState], bool]
    get_skip_reason: Callable[[RequestState], str | None] | None = None
    get_summary: Callable[[RequestState], str] | None = None
    follow_up: str | None = None  # the label the site's errors for this step start with


class RequestState:
    def __init__(self, request) -> None:
        self.request = request
        self.rows = {row.step: row for row in request.steps}

    @cached_property
    def site(self):
        name = self.request.suite_site
        if not name and self.request.tenant:  # claimed, but the request is not saved with it yet
            name = frappe.db.get_value(
                "Suite Site",
                {"tenant": self.request.tenant, "status": ["!=", "Dropped"]},
                "name",
                order_by="creation desc",
            )
        return frappe.get_doc("Suite Site", name) if name else None

    @cached_property
    def mail_domain(self):
        return frappe.get_doc("Mail Domain", self.site.mail_domain)

    def get_pool_reason(self) -> str | None:
        """The site was there before the request looked for one: no server or site was made for it."""

        started_at = self.rows["claim"].started_at
        if started_at and get_datetime(self.site.creation) < get_datetime(started_at):
            return _("Suite Site {0} was ready in the pool.").format(self.site.name)
        return None

    def get_site_domain_skip_reason(self) -> str | None:
        if not (self.site.assign_site_domain and self.site.site_domain):
            return _("The site gets no site domain.")
        return self.site.follow_up_skip_reason

    @property
    def are_mail_records_published(self) -> bool:
        records = {"managed_by_doctype": "Mail Domain", "managed_by": self.mail_domain.name}
        return bool(frappe.db.exists("DNS Record", records)) and not frappe.db.exists(
            "DNS Record", {**records, "is_verified": 0}
        )

    def get_status(self, step: Step) -> tuple[str, str | None]:
        row = self.rows[step.key]
        if row.status in DONE:
            return row.status, row.detail
        if any(self.rows[key].status not in DONE for key in step.after):
            return "Pending", None
        if step.get_skip_reason and (reason := step.get_skip_reason(self)):
            return "Skipped", reason
        if step.is_done(self):
            return "Completed", step.get_summary(self) if step.get_summary else None
        if self.request.status == "Failed":
            return "Failed", self.request.error
        if self.request.status == "Expired":
            return "Failed", _("The signup expired before its email was confirmed.")
        return "Running", self.get_waiting_reason(step)

    def get_waiting_reason(self, step: Step) -> str | None:
        if step.key == "verified":
            return _("Waiting for the code mailed to {0}.").format(self.request.email)
        error = self.site.error if self.site else None
        if not error:
            return None
        # The site domain runs alongside mail: each shows only its own errors.
        labels = {_(other.follow_up) for other in STEPS if other.follow_up}
        prefix = next((label for label in labels if error.startswith(f"{label}:")), None)
        return error if prefix in (None, step.follow_up and _(step.follow_up)) else None


STEPS = (
    Step("created", "Request created", (), lambda state: True),
    Step(
        "verified",
        "Email verified",
        ("created",),
        lambda state: state.request.status not in ("Pending", "Expired"),
    ),
    Step(
        "tenant",
        "Creating the user and Suite Tenant",
        ("verified",),
        lambda state: bool(state.request.tenant),
    ),
    Step(
        "claim",
        "Looking for a ready Suite Site",
        ("tenant",),
        lambda state: bool(state.site),
        get_summary=lambda state: _("Suite Site {0}").format(state.site.name),
    ),
    Step(
        "server",
        "Waiting for a Virtual Machine",
        ("claim",),
        lambda state: bool(state.site.virtual_machine),
        get_skip_reason=RequestState.get_pool_reason,
        get_summary=lambda state: _("Virtual Machine {0}").format(state.site.virtual_machine),
    ),
    Step(
        "site",
        "Creating the Suite Site",
        ("server",),
        lambda state: bool(state.site.site_created),
        get_skip_reason=RequestState.get_pool_reason,
        get_summary=lambda state: state.site.hostname,
    ),
    Step("configure", "Configuring the Suite Site", ("site",), lambda state: bool(state.site.activated_at)),
    Step(
        "site_domain",
        "Adding the site domain",
        ("configure",),
        lambda state: state.site.has_site_domain,
        get_skip_reason=RequestState.get_site_domain_skip_reason,
        get_summary=lambda state: state.site.site_domain,
        follow_up="Site domain",
    ),
    Step(
        "mail_domain",
        "Creating the Mail Domain",
        ("configure",),
        lambda state: bool(state.site.mail_domain),
        get_summary=lambda state: state.site.mail_domain,
        follow_up="Mail",
    ),
    Step(
        "mail_records",
        "Publishing the Mail Domain DNS records",
        ("mail_domain",),
        lambda state: state.are_mail_records_published,
        follow_up="Mail",
    ),
    Step(
        "mail_verified",
        "Verifying the Mail Domain DNS records",
        ("mail_records",),
        lambda state: state.mail_domain.is_live(),
        follow_up="Mail",
    ),
    Step(
        "notification_account",
        "Creating the notification account",
        ("mail_verified",),
        lambda state: bool(state.site.email_account_created),
        get_summary=lambda state: state.site.notification_account,
        follow_up="Mail",
    ),
    Step(
        "invite",
        "Inviting the owner",
        ("notification_account",),
        lambda state: bool(state.site.owner_invite),
        get_summary=lambda state: state.site.login_email,
        follow_up="Owner invite",
    ),
    Step("completed", "Completed", ("invite",), lambda state: state.request.status == "Completed"),
)


def add_steps(request) -> None:
    """Every step up front, so the desk shows step N of all of them from the start."""

    for step in STEPS:
        row = request.append("steps", {"step": step.key, "title": step.title, "status": "Pending"})
        if step.key == "verified" and not request.flags.verify_by_otp:
            row.update({"status": "Skipped", "detail": _("Made in the desk: no code is sent.")})


def sync(request) -> None:
    """Saves the status of each step as the records stand, and tells the desk when one changed."""

    if not request.steps:
        return  # made before steps were kept
    state = RequestState(request)
    changed = False
    for step in STEPS:
        row = state.rows[step.key]
        status, detail = state.get_status(step)
        if (status, detail or None) != (row.status, row.detail or None):
            save_row(row, status, detail)
            changed = True
    if changed:
        publish(request)


def sync_site(site) -> None:
    if request := site.get_request():
        sync(request)


def save_row(row, status: str, detail: str | None) -> None:
    values = {"status": status, "detail": detail}
    timestamp = now()
    if status in ("Running", "Completed", "Failed"):
        values["started_at"] = row.started_at or timestamp  # a retried step keeps its first start
    if status in ("Completed", "Failed"):
        values.update(ended_at=timestamp, duration=time_diff_in_seconds(timestamp, values["started_at"]))
    elif status == "Running":
        values.update(ended_at=None, duration=None)
    frappe.db.set_value("Suite Tenant Request Step", row.name, values, update_modified=False)
    row.update(values)


def publish(request) -> None:
    current = next((row for row in request.steps if row.status in ("Running", "Failed")), None)
    frappe.publish_realtime(
        EVENT,
        {
            "request": request.name,
            "progress": sum(row.status in DONE for row in request.steps),
            "total": len(request.steps),
            "step": current.title if current else None,
        },
        doctype=request.doctype,
        docname=request.name,
        after_commit=True,
    )
