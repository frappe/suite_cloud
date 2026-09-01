from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from suite_infra.install import LEGACY_SETTINGS_DOCTYPE, before_install, import_legacy_settings


class TestLegacySettingsImport(IntegrationTestCase):
    def setUp(self) -> None:
        # Start from a site that never had Frappe Suite's Mail Settings.
        frappe.db.delete("Singles", {"doctype": LEGACY_SETTINGS_DOCTYPE})
        frappe.db.delete("__Auth", {"doctype": LEGACY_SETTINGS_DOCTYPE, "name": LEGACY_SETTINGS_DOCTYPE})
        settings = frappe.get_single("Suite Infra Settings")
        settings.root_domain_name = ""
        settings.dns_provider = ""
        settings.save()

    def test_copies_values_suite_left_behind(self) -> None:
        set_legacy_value("root_domain_name", "legacy.test")
        set_legacy_value("dns_provider", "Cloudflare")
        set_legacy_value("ansible_play_timeout", "2400")
        set_encrypted_password(
            LEGACY_SETTINGS_DOCTYPE, LEGACY_SETTINGS_DOCTYPE, "legacy-token", fieldname="dns_provider_token"
        )

        import_legacy_settings()

        settings = frappe.get_single("Suite Infra Settings")
        self.assertEqual(settings.root_domain_name, "legacy.test")
        self.assertEqual(settings.dns_provider, "Cloudflare")
        self.assertEqual(settings.ansible_play_timeout, 2400)
        self.assertEqual(settings.get_password("dns_provider_token"), "legacy-token")

    def test_nothing_to_import_leaves_settings_alone(self) -> None:
        import_legacy_settings()

        settings = frappe.get_single("Suite Infra Settings")
        self.assertFalse(settings.root_domain_name)
        self.assertFalse(settings.dns_provider)


class TestInstallGuard(IntegrationTestCase):
    def test_refuses_while_suite_still_owns_the_doctypes(self) -> None:
        frappe.db.set_value("DocType", "Mail Server", "module", "Mail", update_modified=False)

        with patch("frappe.get_installed_apps", return_value=["frappe", "suite"]):
            self.assertRaisesRegex(frappe.ValidationError, "Frappe Suite", before_install)

    def test_allows_install_once_suite_released_them(self) -> None:
        with patch("frappe.get_installed_apps", return_value=["frappe", "suite"]):
            before_install()

    def test_ignores_sites_without_suite(self) -> None:
        frappe.db.set_value("DocType", "Mail Server", "module", "Mail", update_modified=False)

        with patch("frappe.get_installed_apps", return_value=["frappe"]):
            before_install()


def set_legacy_value(field: str, value: str) -> None:
    frappe.db.delete("Singles", {"doctype": LEGACY_SETTINGS_DOCTYPE, "field": field})
    frappe.qb.into("Singles").columns("doctype", "field", "value").insert(
        LEGACY_SETTINGS_DOCTYPE, field, value
    ).run()
