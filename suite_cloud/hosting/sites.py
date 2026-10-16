"""A Suite Site from creation to a tenant's working site, one re-entrant step at a time.

Pending -> Creating -> Ready (pooled) | Assigned -> Configuring -> Active. A step that has to wait
returns, and ``poll_pending_sites`` comes back to it. The site domain and the mail steps run
after Active and never take it back.
"""

import secrets

import frappe
from frappe import _
from frappe.utils import cint, now
from frappe.utils.password import remove_encrypted_password

from suite_cloud.hosting import begin_step, commit, output_tail, run_in_background, undo_step, wait_for_task
from suite_cloud.hosting.central import CentralUnavailableError
from suite_cloud.hosting.central.pilot import PilotConflict, PilotNotReady, is_finished, task_id
from suite_cloud.hosting.central.site import SiteUnavailableError
from suite_cloud.suite_cloud.doctype.dns_record.dns_record import delete_managed_records
from suite_cloud.utils import get_config, log_exception

MAX_HOSTNAME_ATTEMPTS = 5
TENANT_SITE_ROLES = ("System Manager", "Suite Admin", "Suite User")
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
    except WAITING_ERRORS:
        advanced = False
    except Exception as e:
        undo_step()
        log_exception(f"Suite Site {doc.name} failed at {step.__name__}", doc)
        frappe.get_doc("Suite Site", doc.name).db_set(
            {"status": "Failed", "error": str(e) or type(e).__name__}
        )
        advanced = False
    commit()
    return advanced


# --- steps ------------------------------------------------------------------------


def create(site) -> bool:
    """Creates the site on Pilot with Suite installed; a hostname another server holds is re-rolled."""

    if not site.virtual_machine:
        if not place(site):
            return False
        site.db_set({"virtual_machine": site.virtual_machine, "hostname": site.hostname})
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
    """Connects the site to its tenant and creates the tenant's user; the site is then Active."""

    site.db_set("status", "Configuring")
    client = site.get_site_client()
    if not site.suite_configured:
        configure_suite(site, client)
        site.db_set("suite_configured", 1)
        commit()
    if not site.tenant_user_created:
        create_tenant_user(site, client)
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
    client.set_single(
        "Suite Settings",
        {
            "suite_cloud_url": get_config("public_url") or frappe.utils.get_url(),
            "suite_cloud_tenant": tenant.name,
            "suite_cloud_secret": tenant.get_password("secret"),
            "workspace_name": tenant.title,
            "contact_email": tenant.contact_email,
        },
    )


def create_tenant_user(site, client) -> None:
    """The tenant's owner on the site, with the password typed at signup; it is then forgotten."""

    user = frappe.get_doc("User", frappe.db.get_value("Suite Tenant", site.tenant, "user"))
    request = site.get_request()
    values = {
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "user_type": "System User",
        "send_welcome_email": 0,
        "roles": [{"role": role} for role in TENANT_SITE_ROLES],
    }
    if request and request.password:
        values["new_password"] = request.get_password("password")
    client.ensure_user(values)
    site.db_set("tenant_user_created", 1)
    if request:
        forget_password(request.name)


def activate(site) -> None:
    site.db_set({"status": "Active", "activated_at": now(), "error": site.follow_up_skip_reason})
    if request := site.get_request():
        frappe.db.set_value("Suite Tenant Request", request.name, "status", "Completed")


def follow_up(site) -> bool:
    """The site domain and mail: each failure is noted on the site, which stays Active."""

    for label, step in site.pending_follow_ups():
        begin_step()
        try:
            step(site)
        except WAITING_ERRORS:
            pass
        except Exception as e:
            undo_step()
            log_exception(f"{label} failed for Suite Site {site.name}", site)
            frappe.get_doc("Suite Site", site.name).db_set("error", f"{label}: {e}")
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
    if site.status == "Failed":
        site.db_set({"status": resume_status(site), "error": None})
    enqueue_provision(site.name)


def resume_status(site) -> str:
    if not site.site_created:
        return "Pending"
    if not site.tenant:
        return "Ready"
    return "Active" if site.tenant_user_created else "Assigned"


def enqueue_drop(site: str) -> None:
    run_in_background("suite_cloud.hosting.sites.drop", f"suite-site-drop:{site}", site=site)


def drop(site: str) -> None:
    doc = frappe.get_doc("Suite Site", site, for_update=True)
    if doc.status == "Dropped":
        return
    begin_step()
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
    doc.db_set({"status": "Dropped", "dropped_at": now(), "error": None})
    commit()


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


def forget_password(request: str) -> None:
    remove_encrypted_password("Suite Tenant Request", request, "password")
    frappe.db.set_value("Suite Tenant Request", request, "password", None, update_modified=False)
