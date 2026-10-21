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

    def test_telemetry_retention_is_backfilled_for_existing_sites(self) -> None:
        from suite_cloud.patches.v1_0 import fill_telemetry_retention_default as patch

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELD, 0)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELD), 90)

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELD, 30)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELD), 30)

    def test_a_retention_under_a_day_is_refused(self) -> None:
        self.settings.telemetry_retention_days = 0
        self.assertRaises(frappe.ValidationError, self.settings.save)

    def test_telemetry_limits_are_backfilled_and_must_be_positive(self) -> None:
        from suite_cloud.patches.v1_0 import fill_telemetry_limit_defaults as patch

        frappe.db.set_single_value(patch.SETTINGS, patch.FIELDS[0], 0)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, patch.FIELDS[0]), 300)

        for field in patch.FIELDS:
            with self.subTest(field):
                settings = frappe.get_single("Suite Cloud Settings")
                settings.set(field, 0)
                self.assertRaisesRegex(frappe.ValidationError, "at least 1", settings.save)

    def test_agent_defaults_are_backfilled_and_must_be_positive(self) -> None:
        from suite_cloud.patches.v1_0 import fill_agent_defaults as patch

        for field in patch.FIELDS:
            frappe.db.set_single_value(patch.SETTINGS, field, 0)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, "agent_max_attempts"), 8)
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, "agent_metrics_interval_seconds"), 60)

        frappe.db.set_single_value(patch.SETTINGS, "agent_max_attempts", 3)
        patch.execute()
        self.assertEqual(frappe.db.get_single_value(patch.SETTINGS, "agent_max_attempts"), 3)

        for field in patch.FIELDS:
            with self.subTest(field):
                settings = frappe.get_single("Suite Cloud Settings")
                settings.set(field, 0)
                self.assertRaisesRegex(frappe.ValidationError, "at least 1", settings.save)

    def test_log_deletion_days_cannot_be_negative(self) -> None:
        self.settings.agent_log_delete_after_days = -1
        self.assertRaisesRegex(frappe.ValidationError, "negative", self.settings.save)

        self.settings.reload()
        self.settings.agent_log_delete_after_days = 0
        self.settings.save()
        self.assertEqual(get_config("agent_log_delete_after_days"), 0)

    def test_a_new_telemetry_secret_keeps_the_old_one_for_a_day(self) -> None:
        from frappe.utils.password import get_decrypted_password

        self.settings.telemetry_secret = ""  # the site may hold one already
        self.settings.save()
        self.settings.telemetry_secret = "first"
        self.settings.save()
        self.settings.reload()
        self.settings.save()  # the form sends the masked secret back: nothing rotates
        self.assertIsNone(self.settings.telemetry_previous_secret_until)

        self.settings.telemetry_secret = "second"
        self.settings.save()
        previous = get_decrypted_password(
            "Suite Cloud Settings", "Suite Cloud Settings", "telemetry_previous_secret"
        )
        self.assertEqual(previous, "first")
        hours = (
            self.settings.telemetry_previous_secret_until - frappe.utils.now_datetime()
        ).total_seconds() / 3600
        self.assertAlmostEqual(hours, 24, delta=0.1)

        self.settings.telemetry_secret = ""
        self.settings.save()
        self.assertIsNone(self.settings.telemetry_previous_secret_until)
        self.assertIsNone(
            get_decrypted_password(
                "Suite Cloud Settings",
                "Suite Cloud Settings",
                "telemetry_previous_secret",
                raise_exception=False,
            )
        )

    def test_the_telemetry_user_is_created_without_roles(self) -> None:
        from suite_cloud.install import TELEMETRY_USER, ensure_user

        ensure_user(TELEMETRY_USER, "Telemetry Agent")
        ensure_user(TELEMETRY_USER, "Telemetry Agent")  # a second migrate changes nothing
        user = frappe.get_doc("User", TELEMETRY_USER)
        self.assertEqual((user.user_type, user.roles), ("Website User", []))

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
