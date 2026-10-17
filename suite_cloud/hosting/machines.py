"""Servers on Central: created, booted, checked and given the Suite app, one re-entrant step at a time.

``status`` mirrors Central. Suite Cloud's own setup lives in ``host_verified``, ``suite_app_status``
and ``ready_since``; an ``error`` stops it until an operator runs Retry.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now, now_datetime

from suite_cloud.hosting import (
    begin_step,
    central,
    commit,
    output_tail,
    run_in_background,
    undo_step,
    wait_for_task,
)
from suite_cloud.hosting.central import CentralRejectedError, CentralUnavailableError
from suite_cloud.hosting.central.pilot import PilotError, PilotNotReady, is_finished, task_id
from suite_cloud.utils import get_config, log_error, log_exception

PILOT_BOOT_DEADLINE = timedelta(minutes=30)
FAILED_ACTION_STATUSES = ("Failed", "Timed Out")
SERVER_STATUSES = (
    "Pending",
    "Provisioning",
    "Deploying",
    "Running",
    "Paused",
    "Stopped",
    "Failed",
    "Terminated",
)
SUITE_APP = "suite"


def enqueue_provision(vm: str) -> None:
    run_in_background("suite_cloud.hosting.machines.provision", f"virtual-machine:{vm}", vm=vm)


def provision(vm: str) -> None:
    """Runs the steps in order until one has to wait; the 5-minute fleet refresh comes back. A step
    already done returns at once, so a server that lost its readiness is set up again."""

    for step in (create, accept, boot, verify_host, install_suite):
        doc = frappe.get_doc("Virtual Machine", vm, for_update=True)
        if doc.error or doc.status in ("Failed", "Terminated"):
            return
        if not run_step(doc, step):
            return
    doc = frappe.get_doc("Virtual Machine", vm)
    if not doc.ready_since:
        doc.db_set("ready_since", now())
    commit()


def run_step(doc, step) -> bool:
    begin_step()
    try:
        done = step(doc)
    except (PilotNotReady, CentralUnavailableError):
        done = False
    except CentralRejectedError as e:
        stop(doc, central_message(e))
        done = False
    except Exception:
        undo_step()
        log_exception(f"Server {doc.name} failed at {step.__name__}", doc)
        stop(doc, _("{0} failed; see the Error Log.").format(step.__name__))
        done = False
    commit()
    return done


# --- steps ----------------------------------------------------------------------


def create(vm) -> bool:
    """The same request key and arguments on a retry return the creation Central already has."""

    if vm.central_action:
        return True
    status = central.get_central_client().create_server(**server_arguments(vm))
    other = frappe.db.get_value(
        "Virtual Machine", {"central_action": status["action"], "name": ["!=", vm.name]}
    )
    if other:
        # Central folds identical pending creations into one, whatever their keys.
        vm.db_set({"status": "Failed", "error": _("Central collapsed this creation into {0}.").format(other)})
        return False
    vm.db_set("central_action", status["action"])
    return True


def accept(vm) -> bool:
    if vm.central_resource_id:
        return True
    status = central.get_central_client().action_status(vm.central_action)
    if status.get("resource_id"):
        vm.db_set({"central_resource_id": status["resource_id"], "status": "Provisioning"})
        return True
    if status.get("status") in FAILED_ACTION_STATUSES:
        vm.db_set({"status": "Failed", "error": action_error_text(status)})
    return False


def boot(vm) -> bool:
    if not has_booted(vm):
        server = find_server(central.get_central_client().registry(), vm.central_resource_id)
        if server:
            apply_server(vm, server)
    return has_booted(vm)


def verify_host(vm) -> bool:
    """Pilot registers sites under the region's proxy zone, in production mode."""

    if vm.host_verified:
        return True
    pilot = vm.get_pilot_client()
    try:
        domains, settings = pilot.wildcard_domains(), pilot.settings()
    except PilotNotReady:
        if vm.booted_at and now_datetime() - get_datetime(vm.booted_at) > PILOT_BOOT_DEADLINE:
            stop(vm, _("Pilot did not answer within 30 minutes of the server booting."))
        return False
    settings = ensure_letsencrypt_email(vm, pilot, settings)
    zone = domains[0].lstrip(".") if domains else None
    vm.db_set(
        {
            "wildcard_zone": zone,
            "letsencrypt_email_set": int(bool((settings.get("letsencrypt") or {}).get("email"))),
        }
    )
    if problem := host_problem(vm, zone, settings):
        stop(vm, problem)
        return False
    vm.db_set("host_verified", 1)
    return True


def ensure_letsencrypt_email(vm, pilot, settings: dict) -> dict:
    """Pilot's settings once its Let's Encrypt email is set from the ACME Contact Email, which the
    certificates of site and custom domains need. A failure leaves the server without domains, not
    without sites, so it is logged and the host check goes on."""

    email = get_config("acme_contact_email")
    if (settings.get("letsencrypt") or {}).get("email") or not email:
        return settings
    try:
        pilot.set_letsencrypt_email(email)
        return pilot.settings()  # what Pilot keeps, not what it answered
    except PilotError:
        log_exception(f"Pilot on server {vm.name} did not take the Let's Encrypt email", vm)
        return settings


def host_problem(vm, zone: str | None, settings: dict) -> str | None:
    if not zone:
        return _("Pilot lists no wildcard domain, so sites on this server cannot be routed.")
    if not zone.startswith(f"{vm.region}."):
        # Central names every regional zone <region>.<wildcard domain>.
        return _("Pilot registers sites under {0}, which is not a zone of region {1}.").format(
            zone, vm.region
        )
    proxy_zone = learn_proxy_zone(vm.region, zone)
    if zone != proxy_zone:
        return _("Pilot registers sites under {0}, but region {1} serves {2}.").format(
            zone, vm.region, proxy_zone
        )
    if not (settings.get("production") or {}).get("enabled"):
        return _("Pilot is not in production mode.")
    return None


def learn_proxy_zone(region: str, zone: str) -> str:
    """The region's proxy zone; the first server verified there sets it, under a lock, so two
    first servers cannot both set it."""

    proxy_zone = frappe.db.get_value("Region", region, "proxy_zone", for_update=True)
    if not proxy_zone:
        frappe.db.set_value("Region", region, "proxy_zone", zone)
        proxy_zone = zone
    return proxy_zone


def install_suite(vm) -> bool:
    if vm.suite_app_status == "Installed":
        return True
    pilot = vm.get_pilot_client()
    if not vm.suite_app_task:
        if app := find_app(pilot.list_apps()):
            return mark_installed(vm, app)
        task = pilot.install_app(get_config("suite_app_repo"), get_config("suite_app_ref"))
        vm.db_set({"suite_app_status": "Installing", "suite_app_task": task_id(task)})
    task = wait_for_task(pilot, vm.suite_app_task)
    if not is_finished(task):
        return False
    if task["status"] != "success":
        vm.db_set({"suite_app_status": "Failed", "error": output_tail(pilot, vm.suite_app_task)})
        return False
    app = find_app(pilot.list_apps())
    if not app:
        vm.db_set({"suite_app_status": "Failed", "error": _("The install finished without the Suite app.")})
        return False
    return mark_installed(vm, app)


def mark_installed(vm, app: dict) -> bool:
    vm.db_set(
        {
            "suite_app_status": "Installed",
            "suite_app_commit": app.get("current_commit"),
            "suite_app_task": None,
        }
    )
    return True


# --- fleet ------------------------------------------------------------------------


def sync_fleet() -> None:
    """Daily: Central's regions, then every server, including those Central no longer lists."""

    from suite_cloud.hosting.doctype.region.region import sync_regions

    sync_regions()
    commit()
    refresh_fleet(mark_missing=True)


def refresh_fleet(mark_missing: bool = False) -> None:
    """Cron: one registry call updates every server, then unfinished ones are driven on.

    ``mark_missing`` ends the servers Central no longer lists, e.g. terminated from its dashboard,
    so that no new site is placed on them.
    """

    if not frappe.get_cached_doc("Suite Cloud Settings").is_central_configured:
        return
    # Read before the registry: a server accepted in between is then missing here, not from Central.
    vms = [frappe.get_doc("Virtual Machine", n) for n in frappe.get_all("Virtual Machine", pluck="name")]
    try:
        registry = central.get_central_client().registry()
    except CentralUnavailableError:
        return
    for vm in vms:
        if not vm.central_resource_id or vm.status == "Terminated":
            continue
        if server := find_server(registry, vm.central_resource_id):
            apply_server(vm, server)
        elif mark_missing:
            mark_terminated(vm)
    report_unknown_creations(registry, {vm.central_action for vm in vms})
    commit()
    for vm in vms:
        if vm.is_building:
            enqueue_provision(vm.name)
        elif vm.is_ready:
            refresh_foreign_sites(vm)


def mark_terminated(vm) -> None:
    vm.db_set(
        {
            "status": "Terminated",
            "enabled": 0,
            "error": _("Central no longer lists server {0}.").format(vm.central_resource_id),
            "last_synced_at": now(),
        }
    )
    log_error(f"Server {vm.name} is gone from Central", vm.central_resource_id)


def refresh_foreign_sites(vm) -> None:
    """Sites Pilot lists that are not Suite Sites take slots too."""

    try:
        names = set(vm.get_pilot_client().list_sites())
    except PilotNotReady:
        return
    ours = set(
        frappe.get_all(
            "Suite Site", {"virtual_machine": vm.name, "hostname": ["is", "set"]}, pluck="hostname"
        )
    )
    vm.db_set("foreign_site_count", len(names - ours))
    commit()


def report_unknown_creations(registry: dict, known: set) -> None:
    """A creation in the team that no Virtual Machine started is someone else's spend."""

    for creation in registry.get("creations") or []:
        action = creation.get("action")
        if action in known or frappe.cache.get_value(f"suite_cloud:unknown-creation:{action}"):
            continue
        frappe.cache.set_value(f"suite_cloud:unknown-creation:{action}", 1, expires_in_sec=86400)
        log_error("Unknown Central server creation", frappe.as_json(creation))


def retry(vm) -> None:
    """Clears the error; a failed Central creation is asked to run again, a failed install starts anew."""

    if vm.central_action and not vm.central_resource_id and vm.status == "Failed":
        try:
            status = central.get_central_client().retry_action(vm.central_action)
        except CentralRejectedError as e:
            vm.db_set("error", central_message(e))
            return
        vm.db_set({"central_action": status.get("action") or vm.central_action})
    vm.db_set({"status": "Pending" if vm.status == "Failed" else vm.status, "error": None})
    reset_failed_install(vm)
    enqueue_provision(vm.name)


def reset_failed_install(vm) -> None:
    if vm.suite_app_status == "Failed":
        vm.db_set({"suite_app_status": "Pending", "suite_app_task": None})


# --- Central --------------------------------------------------------------------------


def resolve_server_arguments(vm) -> None:
    """Fixed on insert, so a retried create_server sends exactly what the first one did."""

    client = central.get_central_client()
    vm.image_offering = vm.image_offering or get_config("central_image_offering")
    vm.image_id = vm.image_id or newest_image(client.list_images(vm.region, vm.image_offering))
    vm.plan = (
        vm.plan
        or get_config("central_plan")
        or cheapest_plan(client.eligible_plans(vm.region, vm.image_offering, vm.image_id))
    )


def newest_image(images: list[dict]) -> str:
    """The newest image without a baked site: Suite sites are created on the server."""

    usable = [i for i in images if str((i.get("tags") or {}).get("has_site")) != "1"]
    if not usable:
        frappe.throw(_("Central offers no image without a baked site for this region."))
    return max(usable, key=lambda i: i.get("created_at") or 0)["id"]


def cheapest_plan(catalog: dict) -> str:
    """Central groups its rows by sub-category, cheapest first within a group only; a row's id is
    ``plan``."""

    rows = [row for group in (catalog.get("plans") or {}).values() for row in group]
    if not rows:
        frappe.throw(_("Central offers no plan for this image in the region within the team's spend limit."))
    return min(rows, key=lambda row: flt(row.get("rate")))["plan"]


def server_arguments(vm) -> dict:
    return {
        "region": vm.region,
        "title": vm.title,
        "request_key": vm.request_key,
        "plan": vm.plan,
        "offering": vm.image_offering,
        "image_id": vm.image_id,
        "has_public_ipv4": bool(vm.has_public_ipv4),
        "has_public_ipv6": bool(vm.has_public_ipv6),
    }


def find_server(registry: dict, resource_id: str | None) -> dict | None:
    return next((s for s in registry.get("servers") or [] if s.get("resource_id") == resource_id), None)


def apply_server(vm, server: dict) -> None:
    """Central's view of the server. The plan too: a resize on the dashboard changes it there, and
    Suite Cloud's own value only mattered for create_server, which never runs again."""

    values = {
        field: server.get(field)
        for field in ("gateway_url", "pilot_audience", "ipv6_address", "public_ipv4", "public_ipv6", "plan")
    }
    if server.get("status") in SERVER_STATUSES:
        values["status"] = server["status"]
    if values.get("status") == "Running" and not vm.booted_at:
        values["booted_at"] = now()
    vm.db_set({**values, **server_size(server), "last_synced_at": now()})


def server_size(server: dict) -> dict:
    """The server's size and the sites it fits; nothing until Central reports it."""

    memory_mb, disk_gb = cint(server.get("memory_megabytes")), flt(server.get("disk_gigabytes"))
    if not memory_mb or not disk_gb:
        return {}
    return {
        "vcpus": flt(server.get("vcpus")),
        "memory_mb": memory_mb,
        "disk_gb": disk_gb,
        "max_sites": sites_that_fit(memory_mb, disk_gb),
    }


def sites_that_fit(memory_mb: int, disk_gb: float) -> int:
    """As many sites as both memory and disk allow after the server's own share, up to the cap."""

    by_memory = (memory_mb - cint(get_config("reserved_memory_mb"))) // cint(get_config("memory_per_site_mb"))
    by_disk = int((disk_gb - cint(get_config("reserved_disk_gb"))) // cint(get_config("disk_per_site_gb")))
    return max(0, min(cint(get_config("max_sites_per_vm")), by_memory, by_disk))


def has_booted(vm) -> bool:
    return vm.status == "Running" and bool(vm.gateway_url and vm.pilot_audience)


def action_error_text(status: dict) -> str:
    error = status.get("error") or {}
    parts = [error.get("message") or _("Central reports the creation {0}.").format(status.get("status"))]
    if error.get("remediation"):
        parts.append(error["remediation"])
    return " ".join(parts)


def central_message(error: CentralRejectedError) -> str:
    return " ".join(part for part in (str(error), error.remediation) if part)


def find_app(apps: list[dict]) -> dict | None:
    return next((app for app in apps if app.get("name") == SUITE_APP), None)


def stop(vm, message: str) -> None:
    vm.db_set("error", message)
