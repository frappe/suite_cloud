import frappe
from frappe.tests import IntegrationTestCase

from suite_infra.utils import get_config

# Unique to the tests so a site's own records never collide with them.
TEST_HOST = "suite-infra-test"


class TestSuiteInfraSettings(IntegrationTestCase):
    def setUp(self) -> None:
        frappe.db.delete("DNS Record", {"host": TEST_HOST})
        self.settings = frappe.get_single("Suite Infra Settings")
        self.settings.dns_provider = ""
        self.settings.root_domain_name = ""
        self.settings.save()
        clear_config_cache()

    def test_root_domain_name_is_lowercased(self) -> None:
        self.settings.root_domain_name = "Example.TEST"
        self.settings.save()

        self.assertEqual(self.settings.root_domain_name, "example.test")

    def test_dns_provider_requires_root_domain_name(self) -> None:
        self.settings.dns_provider = "Cloudflare"
        self.settings.dns_provider_token = "token"

        self.assertRaisesRegex(frappe.ValidationError, "Root Domain Name", self.settings.save)

    def test_dns_provider_requires_credentials(self) -> None:
        self.settings.root_domain_name = "example.test"
        self.settings.dns_provider = "Cloudflare"
        self.settings.dns_provider_token = ""

        self.assertRaisesRegex(frappe.ValidationError, "Token", self.settings.save)

    def test_config_prefers_settings_over_site_config(self) -> None:
        self.settings.root_domain_name = "settings.test"
        self.settings.save()
        clear_config_cache()

        with self.patch_site_config(root_domain_name="conf.test"):
            self.assertEqual(get_config("root_domain_name"), "settings.test")

    def test_config_falls_back_to_site_config(self) -> None:
        with self.patch_site_config(root_domain_name="conf.test"):
            self.assertEqual(get_config("root_domain_name"), "conf.test")
            self.assertEqual(get_config(("root_domain_name", "default_dns_ttl")), ("conf.test", 3600))

    def test_unknown_config_key_throws(self) -> None:
        self.assertRaises(frappe.ValidationError, get_config, "server_url")

    def test_root_domain_change_resets_dns_record_verification(self) -> None:
        self.settings.root_domain_name = "example.test"
        self.settings.save()

        record = make_dns_record()
        record.db_set("is_verified", 1)

        self.settings.root_domain_name = "renamed.test"
        self.settings.save()

        self.assertEqual(frappe.db.get_value("DNS Record", record.name, "is_verified"), 0)

    def test_dns_record_fqdn_uses_root_domain_name(self) -> None:
        self.settings.root_domain_name = "example.test"
        self.settings.save()
        clear_config_cache()

        self.assertEqual(make_dns_record().fqdn, f"{TEST_HOST}.example.test")

    def patch_site_config(self, **values):
        clear_config_cache()
        return self.patch_hooks_free_conf(values)

    def patch_hooks_free_conf(self, values: dict):
        from unittest.mock import patch

        return patch.dict(frappe.local.conf, {"suite_infra": values})


def make_dns_record():
    """Inserts a DNS Record without a provider, which leaves it unverified and enqueues nothing."""

    frappe.flags.do_not_enqueue = True
    record = frappe.new_doc("DNS Record")
    record.host = TEST_HOST
    record.type = "A"
    record.value = "203.0.113.10"
    record.category = "Sending Record"
    return record.insert()


def clear_config_cache() -> None:
    """get_config is request-cached; tests change settings mid-"request"."""

    cache = getattr(frappe.local, "request_cache", None)
    if cache is not None:
        cache.clear()
