import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.hosting import machines
from suite_cloud.hosting.tests.fakes import REGION
from suite_cloud.hosting.tests.fixtures import HostingFixtures
from suite_cloud.utils import user_context


class TestVirtualMachine(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()

    def test_a_new_server_is_created_booted_verified_and_given_suite(self) -> None:
        vm = self.make_vm()

        self.assertEqual(vm.title, f"suite-{REGION}-{vm.name}")
        self.assertEqual(
            (vm.image_id, vm.plan, vm.max_sites), ("img-new", "plan-small", 3)
        )  # baked image skipped
        self.assertEqual(vm.status, "Running")
        self.assertTrue(vm.host_verified and vm.letsencrypt_email_set)
        self.assertEqual(vm.wildcard_zone, f"{REGION}.frappe.test")
        self.assertEqual((vm.suite_app_status, vm.suite_app_commit), ("Installed", "abc123"))
        self.assertTrue(vm.ready_since)
        self.assertTrue(vm.has_free_slot)
        arguments = self.central.calls[0][1]
        self.assertIs(arguments["has_public_ipv4"], False)
        self.assertEqual(arguments["request_key"], f"suite-cloud-{vm.name}")

    def test_a_retried_create_sends_the_persisted_arguments(self) -> None:
        self.central.boot = False
        vm = self.make_vm()
        self.assertEqual(vm.status, "Provisioning")
        vm.db_set("central_action", None)  # as if the answer to create_server was lost

        machines.provision(vm.name)

        first, second = (call[1] for call in self.central.calls if call[0] == "create_server")
        self.assertEqual(first, second)
        self.assertEqual(len(self.central.actions), 1)

    def test_a_failed_creation_waits_for_retry(self) -> None:
        self.central.fail_creations = True
        vm = self.make_vm()
        self.assertEqual(vm.status, "Failed")
        self.assertIn("Region at capacity. Try later.", vm.error)

        self.central.fail_creations = False
        vm.retry()

        vm.reload()
        self.assertIn(("retry_action", vm.central_action), self.central.calls)
        self.assertEqual(vm.status, "Running")
        self.assertTrue(vm.ready_since)

    def test_pilot_not_answering_is_not_yet(self) -> None:
        self.pilot.not_ready = True
        vm = self.make_vm()
        self.assertFalse(vm.host_verified or vm.error)

        self.pilot.not_ready = False
        machines.refresh_fleet()

        self.assertTrue(frappe.get_doc("Virtual Machine", vm.name).ready_since)

    def test_pilot_silent_past_the_deadline_stops_the_server(self) -> None:
        self.pilot.not_ready = True
        vm = self.make_vm()
        vm.db_set("booted_at", frappe.utils.add_to_date(None, minutes=-31))

        machines.provision(vm.name)

        self.assertIn("30 minutes", frappe.db.get_value("Virtual Machine", vm.name, "error"))

    def test_a_host_under_another_zone_fails_verification(self) -> None:
        self.pilot.wildcards = [".other.frappe.test"]
        vm = self.make_vm()

        self.assertFalse(vm.host_verified)
        self.assertIn("other.frappe.test", vm.error)
        self.assertFalse(vm.has_free_slot)
        self.assertFalse(vm.is_building)

    def test_suite_already_present_is_not_installed_again(self) -> None:
        self.pilot.apps.append({"name": "suite", "current_commit": "fff000"})
        vm = self.make_vm()

        self.assertEqual(vm.suite_app_commit, "fff000")
        self.assertNotIn("install_app", [call[0] for call in self.pilot.calls])

    def test_identical_creations_collapsed_by_central_fail_the_second(self) -> None:
        vm = self.make_vm()
        self.central.create_server = lambda **arguments: self.central.public(vm.central_action)
        other = self.make_vm()

        self.assertEqual(other.status, "Failed")
        self.assertIn(vm.name, other.error)

    def test_refresh_reads_every_server_and_counts_foreign_sites(self) -> None:
        vm = self.make_vm()
        self.central.servers[vm.central_resource_id]["public_ipv6"] = "2001:db8::5"

        machines.refresh_fleet()

        vm.reload()
        self.assertEqual(vm.public_ipv6, "2001:db8::5")
        self.assertEqual(vm.foreign_site_count, 1)  # the baked site.local
        self.assertEqual(vm.free_slots, 2)

    def test_open_pilot_asks_for_a_fresh_link(self) -> None:
        vm = self.make_vm()
        with user_context("Administrator"):
            self.assertIn("?sid=", vm.open_pilot())
