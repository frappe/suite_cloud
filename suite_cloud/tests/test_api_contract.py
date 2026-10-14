"""The REST contract every API function keeps: its HTTP verb follows its name."""

import frappe
from frappe.tests import UnitTestCase

from suite_cloud.api import fc, site
from suite_cloud.api.mail import accounts, dmarc, domains, groups, mailing_lists, meta, tls

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
    for module in MODULES:
        for name, member in vars(module).items():
            if callable(member) and member in frappe.whitelisted and member.__module__ == module.__name__:
                yield f"{module.__name__}.{name}", name, member


def expected_verb(name: str) -> str:
    """Creates and actions are plain POSTs; everything else carries the verb its name says."""

    return next((verb for prefix, verb in VERB_BY_PREFIX.items() if name.startswith(prefix)), "POST")


class TestApiVerbs(UnitTestCase):
    def test_every_function_allows_the_verb_its_name_says(self) -> None:
        for path, name, function in api_functions():
            methods = frappe.allowed_http_methods_for_whitelisted_func[function]
            verb = expected_verb(name)
            with self.subTest(path):
                self.assertIn(verb, methods)
                if verb != "GET":
                    self.assertNotIn("GET", methods)
                if verb == "POST":
                    self.assertEqual(methods, ("POST",))

    def test_the_contract_covers_the_whole_package(self) -> None:
        names = {name for _, name, _ in api_functions()}
        self.assertGreater(len(names), 60)
        self.assertTrue({"ping", "create_site", "list_accounts", "delete_mailing_list"} <= names)
