from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from suite_infra.install import (
    LEGACY_SETTINGS_DOCTYPE,
    before_install,
    complete_adopted_records,
    import_legacy_settings,
)


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


class TestAdoptedRecordBackfill(IntegrationTestCase):
    """A cluster and server as an older Suite would leave them: blanks where the patches never ran."""

    CLUSTER = "adopt-test-cluster.example.test"
    SERVER = "adopt-test-mx.example.test"

    def setUp(self) -> None:
        # Hostname validation resolves DNS through public resolvers; keep the tests offline.
        for module in ("mail_cluster.mail_cluster", "mail_server.mail_server"):
            patcher = patch(f"suite_infra.suite_infra.doctype.{module}.get_dns_record", return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

        frappe.db.delete("Mail Server", {"name": self.SERVER})
        frappe.db.delete("Mail Cluster", {"name": self.CLUSTER})

        cluster = frappe.new_doc("Mail Cluster")
        cluster.hostname = self.CLUSTER
        cluster.recovery_admin_user = "admin"
        cluster.enabled = 1
        cluster.insert()

        server = frappe.new_doc("Mail Server")
        server.cluster = self.CLUSTER
        server.hostname = self.SERVER
        server.ssh_port = 22
        server.ssh_user = "root"
        server.recovery_http_port = 8080
        server.enabled = 1
        server.insert()

        frappe.db.set_value(
            "Mail Cluster",
            self.CLUSTER,
            {"ssh_public_key": "", "ssh_private_key": "", "default_domain": ""},
            update_modified=False,
        )
        frappe.db.set_value(
            "Mail Server",
            self.SERVER,
            {"recovery_http_port": 0, "bootstrap_ndjson": ""},
            update_modified=False,
        )

    def test_fills_the_blanks_the_retired_patches_used_to(self) -> None:
        complete_adopted_records()

        cluster = frappe.get_doc("Mail Cluster", self.CLUSTER)
        self.assertTrue(cluster.ssh_public_key.startswith("ssh-rsa "))
        self.assertTrue(cluster.get_password("ssh_private_key").startswith("-----BEGIN"))
        self.assertEqual(cluster.default_domain, self.CLUSTER)
        self.assertTrue(cluster.data_store)

        server = frappe.get_doc("Mail Server", self.SERVER)
        self.assertEqual(server.recovery_http_port, 8080)
        self.assertIn(self.SERVER, server.bootstrap_ndjson)

    def test_keeps_values_that_are_already_set(self) -> None:
        frappe.db.set_value("Mail Server", self.SERVER, "recovery_http_port", 9090, update_modified=False)

        complete_adopted_records()

        self.assertEqual(frappe.db.get_value("Mail Server", self.SERVER, "recovery_http_port"), 9090)

    def test_one_bad_record_does_not_block_the_rest(self) -> None:
        # A server missing a mandatory value fails its save; the cluster must still complete.
        frappe.db.set_value("Mail Server", self.SERVER, "ssh_user", "", update_modified=False)

        complete_adopted_records()

        self.assertTrue(frappe.db.get_value("Mail Cluster", self.CLUSTER, "ssh_public_key"))
        self.assertFalse(frappe.db.get_value("Mail Server", self.SERVER, "bootstrap_ndjson"))
        self.assertTrue(
            frappe.db.exists("Error Log", {"method": ["like", "%Could not complete adopted Mail Server%"]})
        )
