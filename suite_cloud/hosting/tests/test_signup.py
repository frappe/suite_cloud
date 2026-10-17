import re
from unittest.mock import patch
from uuid import UUID

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api import signup
from suite_cloud.hosting import progress
from suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request import expire_requests
from suite_cloud.hosting.labels import LabelNotAvailableError
from suite_cloud.hosting.tests.fakes import REGION
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, SITE_ZONE, HostingFixtures

EMAIL = "new-signup@frappe.test"


class TestSignup(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()
        self.make_vm()
        frappe.db.delete("Suite Tenant", {"user": EMAIL})
        frappe.db.delete("User", {"name": EMAIL})
        self.mails = []
        mailer = patch("frappe.sendmail", side_effect=lambda **kwargs: self.mails.append(kwargs))
        mailer.start()
        self.addCleanup(mailer.stop)

    def create(self, label: str = "acme") -> str:
        return signup.create_request(email=EMAIL, full_name="Ada Lovelace", label=label)["request"]

    def code(self) -> str:
        return re.search(r"\d{6}", self.mails[-1]["message"]).group()

    def test_a_signup_ends_with_the_owner_invited_from_the_site(self) -> None:
        request = self.create()
        self.assertEqual(UUID(request).version, 7)
        key = signup.verify_otp(request, self.code())["request_key"]

        status = signup.get_status(request, key)
        self.assertEqual(
            status, {"status": "Completed", "stage": "Active", "site_url": f"https://acme.{SITE_ZONE}"}
        )
        doc = frappe.get_doc("Suite Tenant Request", request)
        tenant = frappe.get_doc("Suite Tenant", doc.tenant)
        self.assertEqual((tenant.user, tenant.tenant_type), (EMAIL, "Suite Site"))
        self.assertIn("Suite Tenant", frappe.get_roles(EMAIL))
        self.assertEqual(frappe.db.get_value("User", EMAIL, "user_type"), "Website User")
        # The site invites its owner to their account at the mail domain; they choose the password.
        (invite,) = self.site_api.invites()
        self.assertEqual((invite["account"], invite["backup_email"]), (f"new-signup@acme.{MAIL_ZONE}", EMAIL))
        self.assertFalse([key for key in self.site_api.docs if key[0] == "User"])
        self.assertEqual(len(self.mails), 1)  # Suite Cloud mailed the code only; the site mails the invite

    def test_resend_invite_goes_through_the_site(self) -> None:
        request = frappe.get_doc("Suite Tenant Request", self.create_in_desk())
        request.resend_invite()

        self.assertIn(("send_verification_email", self.site_api.invites()[0]["name"]), self.site_api.calls)

    def test_a_wrong_code_is_refused_and_tries_are_capped(self) -> None:
        request = self.create()
        for _attempt in range(5):
            self.assertRaisesRegex(
                frappe.ValidationError, "not correct", signup.verify_otp, request, "000000x"
            )
        self.assertRaisesRegex(frappe.ValidationError, "expired", signup.verify_otp, request, self.code())

    def test_the_status_needs_the_request_key(self) -> None:
        request = self.create()
        self.assertRaises(frappe.DoesNotExistError, signup.get_status, request, "guess")

    def test_labels_are_checked(self) -> None:
        for label in ("www", "spf3", "proxy-eu", "site-1", "a-vm-b", "sites"):
            with self.subTest(label):
                self.assertRaises(LabelNotAvailableError, self.create, label)
        self.assertRaises(frappe.ValidationError, self.create, "Not Valid!")
        self.create("acme")
        self.assertRaises(LabelNotAvailableError, self.create, "acme")  # an open signup holds it

    def test_signup_is_closed_unless_allowed(self) -> None:
        self.set_setting("allow_signup", 0)
        with self.assertRaises(signup.SignupClosedError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.http_status_code, 403)

    def test_a_request_made_in_the_desk_is_processed_without_a_code(self) -> None:
        request = frappe.get_doc("Suite Tenant Request", self.create_in_desk())

        self.assertEqual(request.status, "Completed")
        self.assertEqual(self.mails, [])  # no code, and no password: the site invites the owner
        self.assertEqual(len(self.site_api.invites()), 1)
        self.assertEqual(frappe.db.get_value("Suite Site", request.suite_site, "status"), "Active")

    def test_sub_addressed_emails_are_refused_unless_allowed(self) -> None:
        values = {"doctype": "Suite Tenant Request", "full_name": "Ada Lovelace", "label": "acme"}
        doc = frappe.get_doc({**values, "email": "new-signup+test@frappe.test"})
        self.assertRaisesRegex(frappe.ValidationError, "sub-address", doc.insert)

        self.set_setting("allow_subaddressed_signups", 1)
        frappe.db.delete("User", {"name": "new-signup+test@frappe.test"})
        request = frappe.get_doc({**values, "email": "new-signup+test@frappe.test"}).insert()

        (invite,) = self.site_api.invites()
        self.assertEqual(invite["account"], f"new-signup@acme.{MAIL_ZONE}")  # the tag is left out
        self.assertEqual(invite["backup_email"], "new-signup+test@frappe.test")
        self.assertEqual(frappe.db.get_value("Suite Tenant Request", request.name, "status"), "Completed")
        notification = frappe.get_doc({**values, "label": "beta", "email": "notification+x@frappe.test"})
        self.assertRaisesRegex(
            frappe.ValidationError, "kept for the site's notifications", notification.insert
        )

    def test_the_notification_local_part_cannot_sign_up(self) -> None:
        values = {"doctype": "Suite Tenant Request", "full_name": "Ada Lovelace", "label": "acme"}
        doc = frappe.get_doc({**values, "email": "notification@frappe.test"})
        self.assertRaisesRegex(frappe.ValidationError, "kept for the site's notifications", doc.insert)

    def test_a_request_made_in_the_desk_is_checked_like_a_signup(self) -> None:
        def request(**fields):
            values = {"email": EMAIL, "full_name": "Ada Lovelace", "label": "acme", **fields}
            return frappe.get_doc({"doctype": "Suite Tenant Request", **values})

        self.assertRaises(LabelNotAvailableError, request(label="www").insert)
        self.assertRaises(frappe.ValidationError, request(email="not-an-address").insert)
        self.assertRaises(frappe.ValidationError, request(full_name=" ").insert)

    def test_a_failed_site_fails_the_signup_whose_retry_retries_the_site(self) -> None:
        self.pilot.create_site = lambda name, apps, key: self.pilot.task("failed", "Error: disk full")
        request = frappe.get_doc("Suite Tenant Request", self.create_in_desk())
        self.assertEqual(request.status, "Failed")
        self.assertIn("disk full", request.error)
        site = request.suite_site
        steps = self.steps(request.name)
        self.assertEqual((steps["server"].status, steps["site"].status), ("Completed", "Failed"))
        self.assertIn("disk full", steps["site"].detail)
        self.assertEqual(steps["configure"].status, "Pending")

        del self.pilot.create_site
        request.retry()

        request.reload()
        self.assertEqual((request.status, request.suite_site), ("Completed", site))
        retried = self.steps(request.name)["site"]
        self.assertEqual((retried.status, retried.detail), ("Completed", self.site(site).hostname))
        self.assertEqual(retried.started_at, steps["site"].started_at)  # from the first try

    def test_a_desk_request_shows_its_steps(self) -> None:
        request = frappe.get_doc("Suite Tenant Request", self.create_in_desk())

        self.assertEqual(
            [row.step for row in request.steps],
            [step.key for step in progress.STEPS],
        )
        steps = self.steps(request.name)
        self.assertEqual(steps["verified"].status, "Skipped")
        self.assertIn("desk", steps["verified"].detail)
        for key in ("created", "tenant", "claim", "server", "site", "configure"):
            with self.subTest(key):
                self.assertEqual(steps[key].status, "Completed")
                self.assertTrue(steps[key].started_at and steps[key].ended_at)
        site = self.site(request.suite_site)
        self.assertEqual(steps["claim"].detail, f"Suite Site {site.name}")
        self.assertEqual(steps["server"].detail, f"Virtual Machine {site.virtual_machine}")

    def test_a_site_from_the_pool_skips_the_server_and_site_steps(self) -> None:
        pooled = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()

        request = frappe.get_doc("Suite Tenant Request", self.create_in_desk())

        self.assertEqual(request.suite_site, pooled.name)
        steps = self.steps(request.name)
        self.assertEqual((steps["server"].status, steps["site"].status), ("Skipped", "Skipped"))
        self.assertIn(pooled.name, steps["server"].detail)
        self.assertEqual(steps["configure"].status, "Completed")

    def test_a_request_shows_what_it_waits_for(self) -> None:
        request = self.create()
        self.assertEqual(self.steps(request)["verified"].status, "Running")
        self.assertIn(EMAIL, self.steps(request)["verified"].detail)
        frappe.db.set_value("Virtual Machine", {"region": REGION}, "enabled", 0)

        signup.verify_otp(request, self.code())

        steps = self.steps(request)
        self.assertEqual((steps["verified"].status, steps["claim"].status), ("Completed", "Completed"))
        self.assertEqual(steps["server"].status, "Running")
        self.assertIn("no server in", steps["server"].detail)
        self.assertEqual(steps["site"].status, "Pending")

    def test_a_label_a_site_took_meanwhile_fails_the_signup(self) -> None:
        request = self.create()
        frappe.get_doc({"doctype": "Suite Site", "region": REGION, "label": "acme"}).insert()

        signup.verify_otp(request, self.code())

        doc = frappe.get_doc("Suite Tenant Request", request)
        self.assertEqual(doc.status, "Failed")
        self.assertIn("not available", doc.error)
        self.assertFalse(doc.suite_site)

    def test_stale_requests_expire(self) -> None:
        request = self.create()
        frappe.db.set_value(
            "Suite Tenant Request", request, "expires_at", frappe.utils.add_to_date(None, hours=-1)
        )

        expire_requests()

        self.assertEqual(frappe.db.get_value("Suite Tenant Request", request, "status"), "Expired")
        self.assertEqual(self.steps(request)["verified"].status, "Failed")
        self.assertIn("expired", self.steps(request)["verified"].detail)

    def steps(self, request: str) -> dict:
        return {row.step: row for row in frappe.get_doc("Suite Tenant Request", request).steps}

    def create_in_desk(self) -> str:
        doc = {
            "doctype": "Suite Tenant Request",
            "email": EMAIL,
            "full_name": "Ada Lovelace",
            "label": "acme",
        }
        return frappe.get_doc(doc).insert().name


class TestSignupContract(IntegrationTestCase):
    """Guests may call the signup, only with POST: secrets travel in bodies, never in URLs."""

    def functions(self):
        for name, member in vars(signup).items():
            if callable(member) and member in frappe.whitelisted and member.__module__ == signup.__name__:
                yield name, member

    def test_every_method_is_a_guest_post(self) -> None:
        names = set()
        for name, function in self.functions():
            names.add(name)
            with self.subTest(name):
                self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[function], ("POST",))
                self.assertIn(function, frappe.guest_methods)
        self.assertEqual(names, {"create_request", "verify_otp", "get_status"})
