import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.utils import get_config


class TestSuiteCloudSettings(IntegrationTestCase):
    def setUp(self) -> None:
        self.settings = frappe.get_single("Suite Cloud Settings")
        clear_config_cache()

    def test_public_url_is_normalised(self) -> None:
        self.settings.public_url = "https://cloud.example.test/ "
        self.settings.save()

        self.assertEqual(self.settings.public_url, "https://cloud.example.test")

    def test_config_prefers_settings_over_site_config(self) -> None:
        self.settings.public_url = "https://settings.test"
        self.settings.save()
        clear_config_cache()

        with self.patch_site_config(public_url="https://conf.test"):
            self.assertEqual(get_config("public_url"), "https://settings.test")

    def test_config_falls_back_to_site_config(self) -> None:
        self.settings.public_url = ""
        self.settings.save()

        with self.patch_site_config(public_url="https://conf.test"):
            self.assertEqual(get_config("public_url"), "https://conf.test")
            self.assertEqual(
                get_config(("public_url", "default_dns_ttl")),
                ("https://conf.test", self.settings.default_dns_ttl),
            )

    def test_unknown_config_key_throws(self) -> None:
        self.assertRaises(frappe.ValidationError, get_config, "root_domain_name")

    def patch_site_config(self, **values):
        from unittest.mock import patch

        clear_config_cache()
        return patch.dict(frappe.local.conf, {"suite_cloud": values})


def clear_config_cache() -> None:
    """get_config is request-cached; tests change settings mid-"request"."""

    cache = getattr(frappe.local, "request_cache", None)
    if cache is not None:
        cache.clear()
