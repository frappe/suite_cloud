"""The REST contract every API function keeps: its verb follows its name, bad input is a 400."""

from datetime import datetime

import frappe
from frappe.tests import UnitTestCase

from suite_cloud.api import fc, site
from suite_cloud.api.mail import accounts, dmarc, domains, groups, mailing_lists, meta, tls
from suite_cloud.api.site import InvalidInputError, api_errors, as_datetime, as_list
from suite_cloud.mail.stalwart.errors import (
    StalwartError,
    StalwartRejectedError,
    StalwartUnauthorizedError,
    StalwartUnavailableError,
)

MODULES = (site, fc, domains, accounts, groups, mailing_lists, meta, dmarc, tls)
VERB_BY_PREFIX = {
    "list_": "GET",
    "get_": "GET",
    "check_": "GET",
    "ping": "GET",
    "update_": "PATCH",
    "set_": "PUT",
    "delete_": "DELETE",
    "remove_": "DELETE",
}


def api_functions():
    """Every whitelisted function of the site API; Frappe Cloud's caller is checked on its own."""

    for module in MODULES:
        for name, member in vars(module).items():
            if callable(member) and member in frappe.whitelisted and member.__module__ == module.__name__:
                if module is fc and name in ("get_site", "update_site"):
                    continue
                yield f"{module.__name__}.{name}", name, member


def expected_verb(name: str) -> str:
    """Creates and actions are plain POSTs; everything else carries the verb its name says."""

    return next((verb for prefix, verb in VERB_BY_PREFIX.items() if name.startswith(prefix)), "POST")


class TestApiVerbs(UnitTestCase):
    def test_every_site_method_answers_only_the_verb_its_name_says(self) -> None:
        for path, name, function in api_functions():
            methods = frappe.allowed_http_methods_for_whitelisted_func[function]
            verb = expected_verb(name)
            with self.subTest(path):
                self.assertEqual(methods, ("GET", "QUERY") if verb == "GET" else (verb,))

    def test_frappe_cloud_methods_keep_post_for_their_caller(self) -> None:
        for name, function in (("get_site", fc.get_site), ("update_site", fc.update_site)):
            methods = frappe.allowed_http_methods_for_whitelisted_func[function]
            self.assertIn(expected_verb(name), methods)
            self.assertIn("POST", methods)

    def test_the_contract_covers_the_whole_package(self) -> None:
        names = {name for _, name, _ in api_functions()}
        self.assertTrue({"ping", "create_site", "list_accounts", "delete_mailing_list"} <= names)


class TestApiInput(UnitTestCase):
    def test_malformed_input_is_a_400(self) -> None:
        for call in (
            lambda: as_list("[not json"),
            lambda: as_list(["a", "b", "c"], cap=2),
            lambda: as_datetime("yesterday-ish"),
        ):
            with self.assertRaises(InvalidInputError) as ctx:
                call()
            self.assertEqual(ctx.exception.http_status_code, 400)

    def test_mail_server_failures_leave_with_a_status_and_a_plain_message(self) -> None:
        cases = (
            (
                StalwartRejectedError({"type": "forbidden", "description": "Quota exceeded."}),
                422,
                "Quota exceeded.",
            ),
            (StalwartRejectedError({"notCreated": {"0": {"type": "alreadyExists"}}}), 422, "alreadyExists"),
            (StalwartUnavailableError(), 503, "not reachable"),
            (StalwartUnauthorizedError("bad key"), 502, "credentials"),
            (StalwartError("boom"), 502, "could not complete"),
        )
        for raised, status, text in cases:
            with self.subTest(type(raised).__name__), self.assertRaises(frappe.ValidationError) as ctx:
                with api_errors():
                    raise raised
            self.assertEqual(ctx.exception.http_status_code, status)
            self.assertIn(text, str(ctx.exception))
            self.assertNotIn("Stalwart", str(ctx.exception))

    def test_moments_parse_or_stay_absent(self) -> None:
        self.assertIsNone(as_datetime(None))
        self.assertIsNone(as_datetime(""))
        self.assertEqual(as_datetime("2026-03-04 05:06:07"), datetime(2026, 3, 4, 5, 6, 7))
