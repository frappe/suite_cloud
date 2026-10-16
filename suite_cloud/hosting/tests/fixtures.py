"""Hosting fixtures: zones, a region and settings, with Central, Pilot and the sites faked."""

from unittest.mock import patch

import frappe

from suite_cloud.hosting.tests.fakes import PROXY_ZONE, REGION, FakeCentral, FakePilot, FakeSite, install
from suite_cloud.tests.fixtures import configure_settings, make_zone, no_dns_provider
from suite_cloud.utils import clear_config_cache

SITE_ZONE = "sites.test"
MAIL_ZONE = "tenants.test"
OWNER = "hosting-owner@frappe.test"


class HostingFixtures:
    """Mixed into a test case; call ``setup_hosting`` from setUp."""

    def setup_hosting(self, **settings) -> None:
        frappe.flags.do_not_enqueue = True
        self.addCleanup(setattr, frappe.flags, "do_not_enqueue", False)
        for zone in (SITE_ZONE, MAIL_ZONE):
            make_zone(zone, is_default=0, dns_provider="Cloudflare", dns_provider_token="token")
        configure_settings(
            **{
                "central_url": "https://central.test",
                "central_team": "suite-team",
                "central_api_key": "key",
                "central_api_secret": "secret",
                "default_region": None,
                "max_sites_per_vm": 3,
                "ready_sites_per_region": 0,
                "min_free_site_slots": 2,
                "auto_create_vms": 0,
                "allow_signup": 1,
                "site_dns_zone": SITE_ZONE,
                "tenant_mail_zone": MAIL_ZONE,
                **settings,
            }
        )
        remove_hosting_data()
        # Regions of the site the tests run on would get servers and pooled sites too (rolled back).
        frappe.db.set_value("Region", {"name": ["!=", REGION]}, "enabled", 0)
        make_region()
        self.set_setting("default_region", REGION)

        self.central, self.pilot, self.site_api = FakeCentral(), FakePilot(), FakeSite()
        for context in (
            install(self.central, self.pilot, self.site_api),
            no_dns_provider(),
            patch("suite_cloud.hosting.domains.verify_dns_record", return_value=True),
            patch("suite_cloud.mail.doctype.mail_domain.mail_domain.verify_dns_record", return_value=True),
        ):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def set_setting(self, field: str, value) -> None:
        frappe.db.set_single_value("Suite Cloud Settings", field, value)
        clear_config_cache()

    def make_vm(self, **fields):
        vm = frappe.get_doc({"doctype": "Virtual Machine", "region": REGION, **fields})
        vm.insert()
        return frappe.get_doc("Virtual Machine", vm.name)

    def make_hosted_tenant(self, title: str = "hosted.frappe.test", **fields):
        from suite_cloud.mail.tests.fixtures import make_owner

        frappe.db.delete("Suite Tenant", {"title": title})
        tenant = frappe.get_doc(
            {"doctype": "Suite Tenant", "title": title, "user": make_owner(OWNER), **fields}
        )
        tenant.insert()
        return tenant

    def site(self, name: str):
        return frappe.get_doc("Suite Site", name)


def make_region(**fields):
    region = (
        frappe.get_doc("Region", REGION) if frappe.db.exists("Region", REGION) else frappe.new_doc("Region")
    )
    region.update(
        {"region": REGION, "enabled": 1, "proxy_zone": PROXY_ZONE, "stalwart_region": "blr", **fields}
    )
    region.save()
    return region


def remove_hosting_data() -> None:
    """Tests share a transaction per class and may run on a site with real data: only test rows go."""

    sites = frappe.get_all("Suite Site", {"region": REGION}, pluck="name")
    frappe.db.delete("DNS Record", {"dns_zone": ["in", [SITE_ZONE, MAIL_ZONE]]})
    for doctype in ("Mail Account", "Mail Domain"):
        frappe.db.delete(doctype, {"name": ["like", f"%{MAIL_ZONE}"]})
    frappe.db.delete("Suite Site Domain", {"parent": ["in", sites or [""]]})
    frappe.db.delete("Suite Site", {"region": REGION})
    frappe.db.delete("Virtual Machine", {"region": REGION})
    frappe.db.delete("Suite Tenant Request", {"email": ["like", "%frappe.test"]})
