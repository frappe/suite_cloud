import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.api import signup
from suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request import expire_requests
from suite_cloud.hosting.labels import LabelNotAvailableError
from suite_cloud.hosting.tests.fixtures import SITE_ZONE, HostingFixtures

EMAIL = "new-signup@frappe.test"
PASSWORD = "correct-horse-battery-staple-91"


class TestSignup(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting(tenant_mail_zone=None)
        self.make_vm()
        frappe.db.delete("Suite Tenant", {"user": EMAIL})
        frappe.db.delete("User", {"name": EMAIL})
        self.mails = []
        mailer = patch("frappe.sendmail", side_effect=lambda **kwargs: self.mails.append(kwargs))
        mailer.start()
        self.addCleanup(mailer.stop)

    def create(self, label: str = "acme", password: str = PASSWORD) -> str:
        return signup.create_request(email=EMAIL, full_name="Ada Lovelace", label=label, password=password)[
            "request"
        ]

    def code(self) -> str:
        return re.search(r"\d{6}", self.mails[-1]["message"]).group()

    def test_a_signup_ends_with_an_active_site_and_the_users_password(self) -> None:
        request = self.create()
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
        self.assertEqual(self.site_api.docs[("User", EMAIL)]["new_password"], PASSWORD)
        self.assertIsNone(
            frappe.utils.password.get_decrypted_password(
                "Suite Tenant Request", request, "password", raise_exception=False
            )
        )

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
        for label in ("www", "spf3", "proxy-eu", "site-1", "a-vm-b"):
            with self.subTest(label):
                self.assertRaises(LabelNotAvailableError, self.create, label)
        self.assertRaises(frappe.ValidationError, self.create, "Not Valid!")
        self.create("acme")
        self.assertRaises(LabelNotAvailableError, self.create, "acme")  # an open signup holds it

    def test_a_weak_password_is_refused(self) -> None:
        self.assertRaisesRegex(frappe.ValidationError, "too weak", self.create, "acme", "password")

    def test_signup_is_closed_unless_allowed(self) -> None:
        self.set_setting("allow_signup", 0)
        with self.assertRaises(signup.SignupClosedError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.http_status_code, 403)

    def test_stale_requests_expire_and_forget_their_password(self) -> None:
        request = self.create()
        frappe.db.set_value(
            "Suite Tenant Request", request, "expires_at", frappe.utils.add_to_date(None, hours=-1)
        )

        expire_requests()

        self.assertEqual(frappe.db.get_value("Suite Tenant Request", request, "status"), "Expired")
        self.assertIsNone(
            frappe.utils.password.get_decrypted_password(
                "Suite Tenant Request", request, "password", raise_exception=False
            )
        )


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
