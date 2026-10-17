from unittest.mock import patch
from uuid import UUID

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.hosting import capacity, sites
from suite_cloud.hosting.central.site import SiteUnavailableError
from suite_cloud.hosting.tests.fakes import REGION
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, SITE_ZONE, HostingFixtures


class TestSuiteSite(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()
        self.vm = self.make_vm()
        self.tenant = self.make_hosted_tenant()

    def test_a_pooled_site_stops_at_ready(self) -> None:
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()

        site.reload()
        self.assertEqual(UUID(site.name).version, 7)
        self.assertEqual(site.virtual_machine, self.vm.name)
        self.assertRegex(site.hostname, rf"^suite-[0-9a-f]{{8}}\.{REGION}\.frappe\.test$")
        self.assertEqual((site.status, site.site_created), ("Ready", 1))
        self.assertIn(site.hostname, self.pilot.sites)
        self.assertEqual(self.pilot.calls[-1][2], f"{site.name}:0")

    def test_a_claimed_site_is_configured_and_turns_active(self) -> None:
        pooled = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()

        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        site = self.site(name)
        self.assertEqual(name, pooled.name)
        self.assertEqual((site.status, site.tenant, site.label), ("Active", self.tenant.name, "acme"))
        settings = self.site_api.singles["Suite Settings"]
        self.assertEqual(settings["suite_cloud_tenant"], self.tenant.name)
        self.assertEqual(settings["suite_cloud_secret"], self.tenant.get_password("secret"))
        methods = [call[0] for call in self.site_api.calls]
        self.assertEqual(methods, ["suite.api.account.mark_onboarded", "validate_suite_cloud_credentials"])
        self.assertFalse([key for key in self.site_api.docs if key[0] == "User"])  # Suite creates the owner

    def test_the_owner_is_invited_once_the_site_can_send_mail(self) -> None:
        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        site = self.site(name)
        (invite,) = self.site_api.invites()
        self.assertEqual(invite["account"], f"hosting-owner@acme.{MAIL_ZONE}")
        self.assertEqual(invite["backup_email"], self.tenant.user)  # no signup: the owner's own address
        self.assertEqual((invite["is_admin"], invite["send_invite"]), (1, 1))
        days = (frappe.utils.get_datetime(invite["expires_at"]) - frappe.utils.now_datetime()).days
        self.assertEqual(days, 6)  # 7 days less the seconds the test took
        self.assertEqual(site.owner_invite, invite["name"])

    def test_resending_reuses_a_valid_invite_and_replaces_an_expired_one(self) -> None:
        site = self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

        site.resend_owner_invite()
        self.assertIn(("send_verification_email", site.owner_invite), self.site_api.calls)
        self.assertEqual(len(self.site_api.invites()), 1)

        self.site_api.invites()[0]["expires_at"] = str(frappe.utils.add_to_date(None, days=-1))
        expired = site.owner_invite
        site.resend_owner_invite()
        self.assertEqual(len(self.site_api.invites()), 2)
        self.assertNotEqual(self.site(site.name).owner_invite, expired)

        self.site_api.invites()[1]["is_verified"] = 1
        self.assertRaisesRegex(
            frappe.ValidationError, "already accepted", self.site(site.name).resend_owner_invite
        )

    def test_without_a_ready_site_one_is_created_for_the_tenant(self) -> None:
        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        self.assertEqual(self.site(name).status, "Active")

    def test_a_site_with_no_room_waits_and_a_server_is_added(self) -> None:
        self.set_setting("auto_create_vms", 1)
        self.vm.db_set("enabled", 0)

        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        new_vm = frappe.get_all(
            "Virtual Machine", {"region": REGION, "name": ["!=", self.vm.name]}, pluck="name"
        )
        self.assertEqual(len(new_vm), 1)
        site = self.site(name)
        self.assertEqual(site.status, "Pending")  # placed by the next poll, once the server is ready
        sites.poll_pending_sites()
        self.assertEqual(self.site(name).virtual_machine, new_vm[0])
        self.assertEqual(self.site(name).status, "Active")

    def test_a_hostname_taken_elsewhere_is_rerolled(self) -> None:
        self.pilot.taken.add("suite-aaaaaaaa")
        with patch("suite_cloud.hosting.sites.secrets.token_hex", side_effect=["aaaaaaaa", "bbbbbbbb"]):
            site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()

        site.reload()
        self.assertEqual((site.status, site.attempts), ("Ready", 1))
        self.assertTrue(site.hostname.startswith("suite-bbbbbbbb."))
        keys = [call[2] for call in self.pilot.calls if call[0] == "create_site"]
        self.assertEqual(keys, [f"{site.name}:0", f"{site.name}:1"])

    def test_crash_after_the_site_exists_is_picked_up(self) -> None:
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        site.db_set({"status": "Creating", "site_created": 0, "pilot_task": None})

        sites.provision(site.name)

        self.assertEqual(self.site(site.name).status, "Ready")
        creates = [call for call in self.pilot.calls if call[0] == "create_site"]
        self.assertEqual(len(creates), 1)  # found on the server, not created twice

    def test_a_lost_task_answer_returns_the_same_task(self) -> None:
        self.pilot.hold_tasks = True
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        task = self.site(site.name).pilot_task
        site.db_set("pilot_task", None)  # as if the answer to create_site was lost
        tasks = len(self.pilot.tasks)

        sites.provision(site.name)

        self.assertEqual(self.site(site.name).pilot_task, task)
        self.assertEqual(len(self.pilot.tasks), tasks)
        self.pilot.finish(task)
        sites.provision(site.name)
        self.assertEqual(self.site(site.name).status, "Ready")

    def test_a_failed_create_fails_the_site_and_retry_resumes(self) -> None:
        self.pilot.create_site = lambda name, apps, key: self.pilot.task("failed", "Error: disk full")
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()

        site.reload()
        self.assertEqual(site.status, "Failed")
        self.assertIn("disk full", site.error)

        del self.pilot.create_site
        site.retry()
        self.assertEqual(self.site(site.name).status, "Ready")

    def test_a_suite_cloud_url_no_site_can_reach_fails_loudly(self) -> None:
        self.set_setting("public_url", None)
        with patch("frappe.utils.get_url", return_value="http://suite-cloud1.localhost:8001"):
            site = self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

        self.assertEqual(site.status, "Failed")
        self.assertIn("cannot reach Suite Cloud at http://suite-cloud1.localhost:8001", site.error)

    def test_without_a_public_url_the_sites_own_url_is_given(self) -> None:
        self.set_setting("public_url", None)
        with patch("frappe.utils.get_url", return_value="https://cloud.example.com"):
            capacity.claim_site(self.tenant.name, "acme", REGION)

        self.assertEqual(
            self.site_api.singles["Suite Settings"]["suite_cloud_url"], "https://cloud.example.com"
        )

    def test_only_addresses_a_server_elsewhere_can_reach_are_given(self) -> None:
        for url, reachable in (
            ("https://cloud.example.com", True),
            ("https://abc.trycloudflare.com", True),
            ("http://suite-cloud1.localhost:8001", False),
            ("http://localhost:8000", False),
            ("http://test_site:8000", False),
            ("http://mac.local", False),
            ("http://127.0.0.1:8001", False),
            ("http://10.0.0.5", False),
            ("http://203.0.113.9", False),  # documentation range: not globally routable
        ):
            with self.subTest(url):
                self.assertEqual(sites.is_reachable_url(url), reachable)

    def test_a_waiting_step_says_what_it_waits_for(self) -> None:
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        self.site_api.call = lambda method, **kwargs: (_ for _ in ()).throw(
            SiteUnavailableError(
                "Suite Cloud is unreachable; try again shortly.", "SuiteCloudUnavailableError", 503
            )
        )

        capacity.claim_site(self.tenant.name, "acme", REGION)

        site.reload()
        self.assertEqual(site.status, "Configuring")
        self.assertIn("Waiting: Suite Cloud is unreachable", site.error)

    def test_active_comes_before_the_site_domain(self) -> None:
        self.pilot.attached = None  # any domain call would fail
        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        site = self.site(name)
        self.assertEqual(site.status, "Active")
        self.assertIn("Site domain", site.error)

    def test_the_site_domain_is_skipped_without_a_letsencrypt_email(self) -> None:
        self.vm.db_set("letsencrypt_email_set", 0)
        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        site = self.site(name)
        self.assertEqual(site.status, "Active")
        self.assertIn("Let's Encrypt", site.error)
        self.assertFalse(site.pending_follow_ups())
        self.assertFalse(site.domains)

        self.vm.db_set("letsencrypt_email_set", 1)  # Verify Host gave Pilot the email
        sites.provision(name)

        site = self.site(name)
        self.assertTrue(site.has_site_domain)
        self.assertFalse(site.error)  # the skip no longer holds

    def test_drop_site_frees_the_slot(self) -> None:
        name = capacity.claim_site(self.tenant.name, "acme", REGION)
        hostname = self.site(name).hostname
        self.assertTrue(frappe.db.exists("DNS Record", {"managed_by": name, "dns_zone": SITE_ZONE}))

        self.site(name).drop_site()

        site = self.site(name)
        self.assertEqual(site.status, "Dropped")
        self.assertNotIn(hostname, self.pilot.sites)
        self.assertFalse(frappe.db.exists("DNS Record", {"managed_by": name}))
        self.assertEqual(frappe.get_doc("Virtual Machine", self.vm.name).site_count, 0)
        self.assertIsNone(site.label)  # free for another site

    def test_drop_waits_for_a_running_setup_job(self) -> None:
        name = capacity.claim_site(self.tenant.name, "acme", REGION)

        with patch("suite_cloud.hosting.sites.is_provisioning", return_value=True):
            self.site(name).drop_site()
        self.assertEqual(self.site(name).status, "Active")
        self.assertIn("setup job is running", self.site(name).error)

        self.site(name).drop_site()
        self.assertEqual(self.site(name).status, "Dropped")

    def test_a_site_still_being_created_cannot_be_deleted(self) -> None:
        self.pilot.hold_tasks = True
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        self.assertEqual(self.site(site.name).status, "Creating")

        self.assertRaisesRegex(frappe.ValidationError, "Drop the site", self.site(site.name).delete)

    def test_labels_are_unique_among_sites(self) -> None:
        capacity.claim_site(self.tenant.name, "acme", REGION)

        other = frappe.get_doc({"doctype": "Suite Site", "region": REGION, "label": "acme"})
        self.assertRaises(frappe.UniqueValidationError, other.insert)


class TestCapacity(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()

    def test_the_pool_is_kept_full_without_duplicates(self) -> None:
        self.set_setting("ready_sites_per_region", 2)
        self.make_vm()

        capacity.ensure_site_pool()
        capacity.ensure_site_pool()

        statuses = frappe.get_all("Suite Site", {"region": REGION}, pluck="status")
        self.assertEqual(statuses, ["Ready", "Ready"])

    def test_the_pool_stops_at_the_server_limit(self) -> None:
        self.set_setting("ready_sites_per_region", 5)
        self.central.size = {"vcpus": 0.5, "memory_megabytes": 2048, "disk_gigabytes": 15.0}  # fits 2
        self.make_vm()

        capacity.ensure_site_pool()

        self.assertEqual(frappe.db.count("Suite Site", {"region": REGION}), 2)

    def test_capacity_adds_one_server_per_run_below_the_threshold(self) -> None:
        self.set_setting("auto_create_vms", 1)
        self.central.boot = False  # stays building

        capacity.ensure_capacity()
        capacity.ensure_capacity()  # the building server's slots count

        self.assertEqual(frappe.db.count("Virtual Machine", {"region": REGION}), 1)

    def test_no_server_is_added_while_one_fits_no_site(self) -> None:
        self.set_setting("auto_create_vms", 1)
        self.central.size = {"vcpus": 0.5, "memory_megabytes": 1024, "disk_gigabytes": 10.0}
        self.make_vm()  # too small for any site

        self.assertRaisesRegex(
            frappe.ValidationError, "fits no site", capacity.ensure_region_capacity, REGION
        )
        capacity.ensure_capacity()
        self.assertEqual(frappe.db.count("Virtual Machine", {"region": REGION}), 1)

        self.set_setting("central_plan", "plan-general")  # a larger plan is bought
        self.assertTrue(capacity.ensure_region_capacity(REGION))

    def test_capacity_does_nothing_when_off(self) -> None:
        capacity.ensure_capacity()
        self.assertEqual(frappe.db.count("Virtual Machine", {"region": REGION}), 0)
