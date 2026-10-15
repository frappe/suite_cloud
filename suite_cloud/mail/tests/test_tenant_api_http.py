"""The tenant API over HTTP: the v2 route, the verb rule, the envelopes and the status codes."""

from unittest.mock import patch

import frappe
from frappe.utils import get_test_client

from suite_cloud.api.mail import domains
from suite_cloud.mail.tests.test_tenant_api import TenantApiTestCase

API = "/api/v2/method/suite_cloud.api."


class TestHttpContract(TenantApiTestCase):
    def request(self, verb: str, method: str, **params):
        """One request through the WSGI app on this test's own connection.

        A request normally opens its own connection, commits and closes it. Here it borrows the
        test's connection, so it sees the fixtures and leaves commit and rollback to the test.
        """

        db = frappe.local.db  # the real connection, not the proxy, or the request loops on itself
        headers = {
            "Authorization": f"token {self.site.api_key}:{self.site.new_secret}",
            "Frappe-Authorization-Source": "Suite Tenant",
            "X-Frappe-Site-Name": frappe.local.site,
        }
        payload = {"query_string": params} if verb == "GET" else {"json": params}
        with (
            patch("frappe.database.get_db", return_value=db),
            patch.object(db, "commit"),
            patch.object(db, "rollback"),
            patch("frappe.destroy"),
        ):
            response = get_test_client().open(
                f"{API}{method}", method=verb, headers=headers, base_url="http://localhost/", **payload
            )
        frappe.flags.do_not_enqueue = True  # the request re-initialised the thread-local
        self.act_as(self.site)
        return response.status_code, response.get_json(silent=True)

    def test_a_read_answers_under_data(self) -> None:
        status, body = self.request("GET", "tenant.ping")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["site"], self.site.name)

    def test_another_verb_is_refused(self) -> None:
        for verb, method in (
            ("POST", "tenant.ping"),
            ("GET", "mail.domains.delete_domain"),
            ("POST", "tenant.update_tenant_profile"),
        ):
            with self.subTest(f"{verb} {method}"):
                status, body = self.request(verb, method)
                self.assertEqual((status, body["errors"][0]["type"]), (403, "PermissionError"))

    def test_a_refusal_carries_its_type_status_and_message(self) -> None:
        cases = (
            ("GET", "mail.accounts.get_account", {"email": "nobody@acme.com"}, 404, "DoesNotExistError"),
            ("GET", "mail.accounts.get_quotas", {"emails": "a@x.com, b@x.com"}, 400, "InvalidInputError"),
            ("POST", "mail.accounts.create_account", {"email": "x", "password": "p"}, 422, "ValidationError"),
            ("POST", "mail.accounts.create_account", {"email": "x"}, 400, "InvalidInputError"),
        )
        for verb, method, params, status, type_ in cases:
            with self.subTest(method):
                got, body = self.request(verb, method, **params)
                error = body["errors"][0]
                self.assertEqual((got, error["type"]), (status, type_))
                self.assertTrue(error["message"])

    def test_an_error_carries_only_its_type_and_message(self) -> None:
        _, body = self.request("POST", "mail.accounts.create_account", email="x")
        error = body["errors"][0]
        self.assertEqual(error["message"], "The password field is required.")
        self.assertEqual(set(error) - {"exception"}, {"type", "message"})

    def test_a_taken_address_is_a_409_in_plain_words(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        self.request("POST", "mail.accounts.create_account", email="jane@acme.com", password="password123")
        status, body = self.request(
            "POST", "mail.accounts.create_account", email="jane@acme.com", password="password123"
        )
        self.assertEqual((status, body["errors"][0]["type"]), (409, "DuplicateEntryError"))
        self.assertEqual(
            body["errors"][0]["message"], "jane@acme.com is already the address of an existing account."
        )

    def test_a_list_travels_as_the_array_text_on_a_get(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        status, body = self.request("GET", "mail.accounts.get_quotas", emails='["nobody@acme.com"]')
        self.assertEqual((status, body), (200, {"data": {}}))

    def test_a_read_with_a_long_list_travels_as_a_query_with_a_json_body(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        emails = [f"nobody{i}@acme.com" for i in range(500)]
        status, body = self.request("QUERY", "mail.accounts.get_quotas", emails=emails)
        self.assertEqual((status, body), (200, {"data": {}}))

    def test_a_delete_answers_204_without_a_body(self) -> None:
        domains.create_domain("acme.com")
        self.verify("acme.com")
        status, body = self.request("DELETE", "mail.domains.delete_domain", domain="acme.com")
        self.assertEqual((status, body), (204, None))
        self.assertFalse(frappe.db.exists("Mail Domain", "acme.com"))
