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

    def test_config_ignores_site_config(self) -> None:
        self.settings.public_url = ""
        self.settings.save()

        with self.patch_site_config(public_url="https://conf.test"):
            public_url, ttl = get_config(("public_url", "default_dns_ttl"))
            self.assertFalse(public_url)
            self.assertEqual(ttl, self.settings.default_dns_ttl)

    def test_tls_verification_comes_from_settings(self) -> None:
        from suite_cloud.mail.stalwart import verify_tls

        self.settings.verify_stalwart_tls = 1
        self.settings.save()
        with self.patch_site_config(verify_stalwart_tls=False):
            self.assertTrue(verify_tls())

        self.settings.verify_stalwart_tls = 0
        self.settings.save()
        with self.patch_site_config(verify_stalwart_tls=True):
            self.assertFalse(verify_tls())

    def test_unknown_config_key_throws(self) -> None:
        self.assertRaises(frappe.ValidationError, get_config, "root_domain_name")

    def test_spam_rules_pin_is_backfilled_for_existing_sites(self) -> None:
        from suite_cloud.patches.v1_0 import pin_spam_filter_rules_version as patch

        default = frappe.get_meta(patch.SETTINGS).get_field(patch.FIELD).default
        self.assertTrue(default)

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELD, "")  # a site saved before the field
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELD), default)

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELD, "v3.0.0")
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELD), "v3.0.0")

    def test_report_retention_is_backfilled_for_existing_sites(self) -> None:
        from suite_cloud.patches.v1_0 import fill_report_retention_defaults as patch

        for field in patch.FIELDS:
            frappe.db.set_single_value(patch.SETTINGS, field, 0)
        patch.execute()
        for field in patch.FIELDS:
            self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, field), 90)

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELDS[0], 30)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELDS[0]), 30)

    def test_tls_verification_is_backfilled_for_existing_sites(self) -> None:
        from suite_cloud.patches.v1_0 import enable_stalwart_tls_verification as patch

        frappe.db.delete("Singles", {"doctype": patch.SETTINGS, "field": patch.FIELD})
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELD), 1)

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELD, 0)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELD), 0)

    def patch_site_config(self, **values):
        from unittest.mock import patch

        clear_config_cache()
        return patch.dict(frappe.local.conf, {"suite_cloud": values})


def clear_config_cache() -> None:
    """get_config is request-cached; tests change settings mid-"request"."""

    cache = getattr(frappe.local, "request_cache", None)
    if cache is not None:
        cache.clear()
