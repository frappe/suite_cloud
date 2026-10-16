import re
from unittest.mock import patch
from uuid import UUID

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password

from suite_cloud.api import signup
from suite_cloud.hosting.doctype.suite_tenant_request.suite_tenant_request import expire_requests
from suite_cloud.hosting.labels import LabelNotAvailableError
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, SITE_ZONE, HostingFixtures

EMAIL = "new-signup@frappe.test"


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

    def create(self, label: str = "acme") -> str:
        return signup.create_request(email=EMAIL, full_name="Ada Lovelace", label=label)["request"]

    def code(self) -> str:
        return re.search(r"\d{6}", self.mails[-1]["message"]).group()

    def stored_password(self, request: str) -> str | None:
        return get_decrypted_password("Suite Tenant Request", request, "password", raise_exception=False)

    def test_a_signup_ends_with_an_active_site_and_mailed_login_details(self) -> None:
        request = self.create()
        self.assertEqual(UUID(request).version, 7)
        password = self.stored_password(request)
        self.assertEqual(len(password), 24)  # generated, never typed
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
        self.assertEqual(self.site_api.docs[("User", EMAIL)]["new_password"], password)
        details = self.mails[-1]
        self.assertEqual((details["recipients"], details["delayed"]), ([EMAIL], False))  # never queued
        self.assertIn(password, details["message"])
        self.assertIn(frappe.db.get_value("Suite Site", doc.suite_site, "hostname"), details["message"])
        self.assertIsNone(self.stored_password(request))  # forgotten once mailed

    def test_a_failed_mail_keeps_the_password_for_resending(self) -> None:
        request = self.create()
        password = self.stored_password(request)
        code = self.code()
        with patch("frappe.sendmail", side_effect=ConnectionError("SMTP is down")):
            signup.verify_otp(request, code)

        doc = frappe.get_doc("Suite Tenant Request", request)
        self.assertEqual(doc.status, "Completed")
        self.assertIn("Resend Login Details", doc.error)
        self.assertEqual(self.stored_password(request), password)

        doc.resend_login_details()

        self.assertIn(password, self.mails[-1]["message"])
        self.assertIsNone(self.stored_password(request))
        self.assertFalse(frappe.db.get_value("Suite Tenant Request", request, "error"))
        self.assertRaisesRegex(frappe.ValidationError, "is gone", doc.resend_login_details)

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

    def test_signup_is_closed_unless_allowed(self) -> None:
        self.set_setting("allow_signup", 0)
        with self.assertRaises(signup.SignupClosedError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.http_status_code, 403)

    def test_a_request_made_in_the_desk_is_processed_without_a_code(self) -> None:
        request = frappe.get_doc(
            {"doctype": "Suite Tenant Request", "email": EMAIL, "full_name": "Ada Lovelace", "label": "acme"}
        ).insert()

        request.reload()
        self.assertEqual(request.status, "Completed")
        self.assertEqual(len(self.mails), 1)  # the login details only, no code
        self.assertIn(self.site_api.docs[("User", EMAIL)]["new_password"], self.mails[0]["message"])
        self.assertEqual(frappe.db.get_value("Suite Site", request.suite_site, "status"), "Active")

    def test_the_site_user_is_named_after_the_tenant_mail_domain(self) -> None:
        self.set_setting("tenant_mail_zone", MAIL_ZONE)
        request = frappe.get_doc(
            {"doctype": "Suite Tenant Request", "email": EMAIL, "full_name": "Ada Lovelace", "label": "acme"}
        ).insert()
        request.reload()

        login = f"new-signup@acme.{MAIL_ZONE}"
        self.assertIn(("User", login), self.site_api.docs)
        self.assertNotIn(("User", EMAIL), self.site_api.docs)
        self.assertEqual(frappe.db.get_value("Suite Tenant", request.tenant, "user"), EMAIL)
        details = self.mails[-1]
        self.assertEqual(details["recipients"], [EMAIL])  # mailed where they signed up
        self.assertIn(login, details["message"])
        site = frappe.get_doc("Suite Site", request.suite_site)
        site.assign_mail_domain = 0
        self.assertRaisesRegex(frappe.ValidationError, "already assigned", site.save)

    def test_a_notification_local_part_keeps_the_signup_address(self) -> None:
        self.set_setting("tenant_mail_zone", MAIL_ZONE)
        email = "notification@frappe.test"
        frappe.db.delete("User", {"name": email})
        frappe.get_doc(
            {"doctype": "Suite Tenant Request", "email": email, "full_name": "Ada Lovelace", "label": "acme"}
        ).insert()

        self.assertIn(("User", email), self.site_api.docs)

    def test_a_request_made_in_the_desk_is_checked_like_a_signup(self) -> None:
        def request(**fields):
            values = {"email": EMAIL, "full_name": "Ada Lovelace", "label": "acme", **fields}
            return frappe.get_doc({"doctype": "Suite Tenant Request", **values})

        self.assertRaises(LabelNotAvailableError, request(label="www").insert)
        self.assertRaises(frappe.ValidationError, request(email="not-an-address").insert)
        self.assertRaises(frappe.ValidationError, request(full_name=" ").insert)

    def test_stale_requests_expire_and_forget_their_password(self) -> None:
        request = self.create()
        frappe.db.set_value(
            "Suite Tenant Request", request, "expires_at", frappe.utils.add_to_date(None, hours=-1)
        )

        expire_requests()

        self.assertEqual(frappe.db.get_value("Suite Tenant Request", request, "status"), "Expired")
        self.assertIsNone(self.stored_password(request))


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
