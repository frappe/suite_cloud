"""A Suite Site from creation to a tenant's working site, one re-entrant step at a time.

Pending -> Creating -> Ready (pooled) | Assigned -> Configuring -> Active, and Dropping -> Dropped.
A step that has to wait returns, and ``poll_pending_sites`` comes back to it. The site domain and
the mail steps run after Active and never take it back.
"""

import ipaddress
import secrets
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now, now_datetime
from frappe.utils.background_jobs import get_job_status
from rq.job import JobStatus

from suite_cloud.hosting import (
    begin_step,
    checkpoint,
    commit,
    output_tail,
    progress,
    run_in_background,
    undo_step,
    wait_for_task,
)
from suite_cloud.hosting.central import CentralUnavailableError
from suite_cloud.hosting.central.pilot import PilotConflict, PilotNotReady, is_finished, task_id
from suite_cloud.hosting.central.site import SiteUnavailableError
from suite_cloud.suite_cloud.doctype.dns_record.dns_record import delete_managed_records
from suite_cloud.utils import get_config, log_exception

MAX_HOSTNAME_ATTEMPTS = 5
INVITE_DAYS = 7
WAITING_ERRORS = (PilotNotReady, SiteUnavailableError, CentralUnavailableError)


class SiteSetupError(Exception):
    pass


def enqueue_provision(site: str) -> None:
    run_in_background("suite_cloud.hosting.sites.provision", f"suite-site:{site}", site=site)


def provision(site: str) -> None:
    while True:
        doc = frappe.get_doc("Suite Site", site, for_update=True)
        step = STEPS.get(doc.status)
        if not step or not run_step(doc, step):
            return


def run_step(doc, step) -> bool:
    begin_step()
    try:
        advanced = step(doc)
    except WAITING_ERRORS as e:
        # Not a failure, but said: a site that only ever waits must show what it waits for.
        frappe.get_doc("Suite Site", doc.name).db_set("error", _("Waiting: {0}").format(e))
        advanced = False
    except Exception as e:
        undo_step()
        log_exception(f"Suite Site {doc.name} failed at {step.__name__}", doc)
        fail(frappe.get_doc("Suite Site", doc.name), str(e) or type(e).__name__)
        advanced = False
    progress.sync_site(doc)
    commit()
    return advanced


def fail(site, error: str) -> None:
    """The site and the signup waiting on it: the signup's Retry then retries the site."""

    site.db_set({"status": "Failed", "error": error})
    if (request := site.get_request()) and request.status == "Processing":
        request.db_set({"status": "Failed", "error": error})


# --- steps ------------------------------------------------------------------------


def create(site) -> bool:
    """Creates the site on Pilot with Suite installed; a hostname another server holds is re-rolled."""

    if not site.virtual_machine:
        if not place(site):
            site.db_set("error", _("Waiting: no server in {0} has a free site slot.").format(site.region))
            return False
        site.db_set({"virtual_machine": site.virtual_machine, "hostname": site.hostname, "error": None})
        progress.sync_site(site)  # kept by the checkpoint before the wait on Pilot
    pilot = site.get_pilot_client()
    if not site.pilot_task:
        if site.hostname in pilot.list_sites():
            return mark_created(site)
        try:
            # The same key returns the task of a request whose answer was lost.
            task = pilot.create_site(site.hostname, ["suite"], f"{site.name}:{cint(site.attempts)}")
        except PilotConflict as e:
            if e.code == "task_conflict":
                return False
            raise
        site.db_set({"pilot_task": task_id(task), "status": "Creating"})
    task = wait_for_task(pilot, site.pilot_task)
    if not is_finished(task):
        return False
    if task["status"] == "success":
        return mark_created(site)
    return reroll_hostname(site, output_tail(pilot, site.pilot_task))


def reroll_hostname(site, output: str) -> bool:
    """Central gives each label to one server only; another server may have taken this one."""

    if "already taken" not in output.lower() or cint(site.attempts) + 1 >= MAX_HOSTNAME_ATTEMPTS:
        raise SiteSetupError(output or _("Pilot could not create the site."))
    site.db_set(
        {
            "hostname": new_hostname(site.get_virtual_machine()),
            "attempts": cint(site.attempts) + 1,
            "pilot_task": None,
            "status": "Pending",
        }
    )
    return True


def mark_created(site) -> bool:
    site.db_set(
        {
            "site_created": 1,
            "pilot_task": None,
            "error": None,
            "status": "Assigned" if site.tenant else "Ready",
        }
    )
    return True


def configure(site) -> bool:
    """Connects the site to its tenant; the site is then Active. Its owner is invited once the
    site can send mail (``invite_owner``)."""

    site.db_set("status", "Configuring")
    if not site.suite_configured:
        configure_suite(site, site.get_site_client())
        site.db_set("suite_configured", 1)
        checkpoint()
    activate(site)
    return True


def configure_suite(site, client) -> None:
    tenant = frappe.get_doc("Suite Tenant", site.tenant)
    push_tenant_credentials(client, tenant)
    if tenant.stalwart_cluster:
        client.set_single("Mail Settings", {"server_url": tenant.get_cluster().base_url})
    request = site.get_request()
    client.call("suite.api.account.mark_onboarded", timezone=request.timezone if request else None)
    # Suite's own check: the site pings Suite Cloud with what it was just given.
    client.run_doc_method("Suite Settings", "Suite Settings", "validate_suite_cloud_credentials")


def push_tenant_credentials(client, tenant) -> None:
    suite_cloud_url = get_config("public_url") or frappe.utils.get_url()
    if not is_reachable_url(suite_cloud_url):
        raise SiteSetupError(
            _(
                "A hosted site cannot reach Suite Cloud at {0}. Set the Public URL in Suite Cloud "
                "Settings, e.g. a tunnel's address on a development machine."
            ).format(suite_cloud_url)
        )
    client.set_single(
        "Suite Settings",
        {
            "suite_cloud_url": suite_cloud_url,
            "suite_cloud_tenant": tenant.name,
            "suite_cloud_secret": tenant.get_password("secret"),
            "workspace_name": tenant.title,
            "contact_email": tenant.contact_email,
        },
    )


def is_reachable_url(url: str) -> bool:
    """Whether a server elsewhere could reach the address: not a local or private name."""

    host = (urlparse(url).hostname or "").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host and not host.endswith((".localhost", ".local")) and host != "localhost"
    return address.is_global


def activate(site) -> None:
    site.db_set({"status": "Active", "activated_at": now(), "error": site.follow_up_skip_reason})


def invite_owner(site) -> bool:
    """Suite's own invite to the owner, from the site's notification account: they choose their
    password when they accept, so none is ever sent."""

    invite = site.get_site_client().create_doc(
        "Mail Account Request",
        {
            "account": site.login_email,
            "backup_email": site.owner_email,
            "is_admin": 1,  # Suite Admin; no desk access
            "send_invite": 1,
            "expires_at": str(add_to_date(now_datetime(), days=INVITE_DAYS)),
        },
    )
    site.db_set({"owner_invite": invite["name"], "owner_invited_at": now()})
    if request := site.get_request():
        request.complete()
    return True


def resend_owner_invite(site) -> None:
    """The same invite again while it is valid; a fresh one once it expired or is gone."""

    client = site.get_site_client()
    invite = client.get_doc("Mail Account Request", site.owner_invite) if site.owner_invite else None
    if invite and invite.get("is_verified"):
        frappe.throw(_("The owner already accepted the invite."))
    if invite and get_datetime(invite.get("expires_at")) > now_datetime():
        client.run_doc_method("Mail Account Request", site.owner_invite, "send_verification_email")
        site.db_set("owner_invited_at", now())
        return
    invite_owner(site)


def follow_up(site) -> bool:
    """The site domain, mail and the owner's invite: each failure is noted on the site, which stays
    Active."""

    attempted = set()
    # Read again after each step: the owner's invite waits for the mail step before it.
    while steps := [(label, step) for label, step in site.pending_follow_ups() if label not in attempted]:
        label, step = steps[0]
        attempted.add(label)
        begin_step()
        try:
            done = step(site)
        except WAITING_ERRORS as e:
            frappe.get_doc("Suite Site", site.name).db_set("error", _("{0}: waiting: {1}").format(label, e))
        except Exception as e:
            undo_step()
            log_exception(f"{label} failed for Suite Site {site.name}", site)
            frappe.get_doc("Suite Site", site.name).db_set("error", f"{label}: {e}")
        else:
            if done and (frappe.db.get_value("Suite Site", site.name, "error") or "").startswith(f"{label}:"):
                frappe.db.set_value("Suite Site", site.name, "error", None)  # its own earlier failure
        progress.sync_site(site)
        commit()
        site = frappe.get_doc("Suite Site", site.name)
    return False


STEPS = {
    "Pending": create,
    "Creating": create,
    "Assigned": configure,
    "Configuring": configure,
    "Active": follow_up,
}


# --- placement ------------------------------------------------------------------------


def place(site) -> bool:
    """Picks the server and hostname; nothing is saved here."""

    from suite_cloud.hosting.capacity import pick_virtual_machine

    vm = pick_virtual_machine(site.region)
    if not vm:
        return False
    site.virtual_machine = vm
    site.hostname = new_hostname(frappe.get_doc("Virtual Machine", vm))
    return True


def new_hostname(vm) -> str:
    """``suite-<hex>.<zone>``: Central refuses ``site-*``, ``proxy*`` and ``*-vm-*`` labels."""

    while True:
        hostname = f"suite-{secrets.token_hex(4)}.{vm.wildcard_zone}"
        if not frappe.db.exists("Suite Site", {"hostname": hostname}):
            return hostname


# --- operator actions ---------------------------------------------------------------------


def retry(site) -> None:
    """A failed task is not waited on again: a create with the same key returns a task still running."""

    if site.status == "Failed":
        site.db_set({"status": resume_status(site), "error": None, "pilot_task": None})
    if (request := site.get_request()) and request.status == "Failed":
        request.db_set({"status": "Processing", "error": None})
        progress.sync(request)
    enqueue_provision(site.name)


def resume_status(site) -> str:
    if not site.site_created:
        return "Pending"
    if not site.tenant:
        return "Ready"
    return "Active" if site.suite_configured else "Assigned"


def enqueue_drop(site: str) -> None:
    run_in_background("suite_cloud.hosting.sites.drop", f"suite-site-drop:{site}", site=site)


def drop(site: str) -> None:
    """Dropping is saved first, so no setup job starts on the site; one already running could write
    its state back afterwards, so the drop waits for it."""

    doc = frappe.get_doc("Suite Site", site, for_update=True)
    if doc.status == "Dropped":
        return
    if is_provisioning(site):
        doc.db_set("error", _("The site's setup job is running; run Drop Site again once it finishes."))
        commit()
        return
    doc.db_set("status", "Dropping")
    checkpoint()
    try:
        drop_from_server(doc)
        from suite_cloud.mail.tenancy import hosted_domain

        hosted_domain.remove(doc)
        delete_managed_records("Suite Site", doc.name)
    except Exception as e:
        undo_step()
        log_exception(f"Suite Site {site} could not be dropped", doc)
        frappe.get_doc("Suite Site", site).db_set("error", _("Drop failed: {0}").format(e))
        commit()
        return
    # The label is free again: it is unique among sites.
    doc.db_set({"status": "Dropped", "dropped_at": now(), "error": None, "label": None})
    commit()


def is_provisioning(site: str) -> bool:
    return get_job_status(f"suite-site:{site}") == JobStatus.STARTED


def drop_from_server(site) -> None:
    if not site.virtual_machine or not site.hostname:
        return
    pilot = site.get_pilot_client()
    if site.hostname not in pilot.list_sites():
        return
    task = pilot.drop_site(site.hostname)
    task = wait_for_task(pilot, task_id(task))
    if not is_finished(task):
        raise SiteSetupError(_("Pilot is still dropping the site; run Drop Site again shortly."))
    if task["status"] != "success":
        raise SiteSetupError(output_tail(pilot, task_id(task)))


# --- cron -------------------------------------------------------------------------------------


def poll_pending_sites() -> None:
    """Cron: sites mid-way get their job again; ready, failed and dropped ones are left alone."""

    for name in frappe.get_all(
        "Suite Site", {"status": ["in", ["Pending", "Creating", "Assigned", "Configuring"]]}, pluck="name"
    ):
        enqueue_provision(name)
    for name in frappe.get_all("Suite Site", {"status": "Active"}, pluck="name"):
        if frappe.get_doc("Suite Site", name).pending_follow_ups():
            enqueue_provision(name)
