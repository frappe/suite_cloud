"""Where sites go: a pool of ready sites per region, and enough servers to hold them."""

import frappe
from frappe import _
from frappe.utils import cint

from suite_cloud.hosting import begin_step, commit, machines, undo_step
from suite_cloud.utils import get_config, log_exception

POOLED_STATUSES = ("Pending", "Creating", "Ready")


def claim_site(tenant: str, label: str, region: str) -> str:
    """The oldest ready site of the region, else a new one created for the tenant."""

    from suite_cloud.hosting import sites

    name = frappe.db.get_value(
        "Suite Site",
        {"region": region, "status": "Ready", "tenant": ["is", "not set"]},
        "name",
        order_by="creation asc",
        for_update=True,
        skip_locked=True,
    )
    if name:
        frappe.get_doc("Suite Site", name).db_set({"tenant": tenant, "label": label, "status": "Assigned"})
        sites.enqueue_provision(name)
        return name
    site = frappe.get_doc({"doctype": "Suite Site", "region": region, "tenant": tenant, "label": label})
    site.insert(ignore_permissions=True)
    if not site.virtual_machine and cint(get_config("auto_create_vms")) and not building_vms(region):
        ensure_region_capacity(region)
    return site.name


def pick_virtual_machine(region: str) -> str | None:
    """The server of the region with most free slots. Every candidate is locked before its slots are
    counted, so a second placement counts only once the first one is saved."""

    best, most = None, 0
    for name in frappe.db.get_values(
        "Virtual Machine",
        {"region": region, "enabled": 1, "status": "Running"},
        "name",
        order_by="name asc",  # one lock order: two placements cannot deadlock
        for_update=True,
        pluck=True,
    ):
        vm = frappe.get_doc("Virtual Machine", name)
        if vm.has_free_slot and vm.free_slots > most:
            best, most = name, vm.free_slots
    return best


def ensure_site_pool() -> None:
    """Cron: every enabled region keeps ``ready_sites_per_region`` unclaimed sites on the way or ready."""

    target = cint(get_config("ready_sites_per_region"))
    for region in enabled_regions():
        begin_step()
        try:
            fill_pool(region, target)
        except Exception:
            undo_step()
            log_exception(f"Site pool of {region} could not be filled")
            continue
        commit()


def fill_pool(region: str, target: int) -> None:
    pooled = frappe.db.count(
        "Suite Site", {"region": region, "tenant": ["is", "not set"], "status": ["in", POOLED_STATUSES]}
    )
    for _slot in range(target - pooled):
        vm = pick_virtual_machine(region)
        if not vm:
            return
        frappe.get_doc({"doctype": "Suite Site", "region": region, "virtual_machine": vm}).insert(
            ignore_permissions=True
        )


def ensure_capacity() -> None:
    """Hourly: a region short of free slots gets one more server, when servers are created automatically."""

    if not cint(get_config("auto_create_vms")):
        return
    for region in enabled_regions():
        begin_step()
        try:
            ensure_region_capacity(region)
        except Exception:
            undo_step()
            log_exception(f"Capacity of {region} could not be added")
            continue
        commit()


def ensure_region_capacity(region: str) -> str | None:
    if free_slots(region) >= cint(get_config("min_free_site_slots")):
        return None
    vm = frappe.get_doc({"doctype": "Virtual Machine", "region": region})
    machines.resolve_server_arguments(vm)
    assert_plan_fits_a_site(region, vm.plan)
    vm.insert(ignore_permissions=True)
    return vm.name


def free_slots(region: str) -> int:
    """Free slots on ready servers, plus the slots of servers still building, less sites waiting for one."""

    slots = 0
    for name in frappe.get_all("Virtual Machine", {"region": region, "enabled": 1}, pluck="name"):
        vm = frappe.get_doc("Virtual Machine", name)
        if vm.is_ready:
            slots += max(vm.free_slots, 0)
        elif vm.is_building:
            slots += cint(vm.max_sites)
    waiting = frappe.db.count(
        "Suite Site", {"region": region, "virtual_machine": ["is", "not set"], "status": "Pending"}
    )
    return slots - waiting


def assert_plan_fits_a_site(region: str, plan: str) -> None:
    """A server of ``plan`` whose reported size fits no site means another one would not either."""

    vm = frappe.db.get_value(
        "Virtual Machine",
        {"region": region, "plan": plan, "enabled": 1, "memory_mb": [">", 0], "max_sites": 0},
    )
    if vm:
        frappe.throw(
            _(
                "Server {0} of plan {1} fits no site under the Server Sizing allowances, so no server "
                "is added: choose a larger Central plan or lower the allowances."
            ).format(vm, plan)
        )


def building_vms(region: str) -> list[str]:
    names = frappe.get_all("Virtual Machine", {"region": region, "enabled": 1}, pluck="name")
    return [n for n in names if frappe.get_doc("Virtual Machine", n).is_building]


def enabled_regions() -> list[str]:
    return frappe.get_all("Region", {"enabled": 1}, pluck="name", order_by="name asc")


def assert_region_enabled(region: str | None) -> str:
    if not region or not frappe.db.get_value("Region", region, "enabled"):
        frappe.throw(_("Region {0} does not take sites.").format(region or _("(none)")))
    return region
