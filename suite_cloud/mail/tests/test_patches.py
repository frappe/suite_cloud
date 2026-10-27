from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.tests.fixtures import configure_settings, make_cluster, make_node
from suite_cloud.patches.v1_0 import drop_legacy_deployment_doctypes as drop_legacy
from suite_cloud.patches.v1_0 import rename_cloud_mail_module
from suite_cloud.suite_cloud.doctype.server_job.server_job import VARIABLES_BUILDERS, create_server_job

OLD_PREFIX = "suite_cloud.cloud_mail."
NEW_PREFIX = "suite_cloud.mail."


class TestRenameCloudMailModule(IntegrationTestCase):
    """The migrate has already registered the new Module Def when the patch meets the old one."""

    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        node = make_node(make_cluster())

        old = rename_cloud_mail_module.OLD
        frappe.get_doc(
            {"doctype": "Module Def", "name": old, "module_name": old, "app_name": "suite_cloud"}
        ).db_insert()
        frappe.db.set_value("DocType", "Mail Quota", "module", old)

        self.upgraded = {}
        for builder in VARIABLES_BUILDERS:
            job = create_server_job(node, "run-commands.yml", builder, variables_builder=builder)
            frappe.db.set_value(
                "Server Job", job.name, "variables_builder", builder.replace(NEW_PREFIX, OLD_PREFIX)
            )
            self.upgraded[job.name] = builder
        self.current = create_server_job(
            node, "run-commands.yml", "current", variables_builder=VARIABLES_BUILDERS[0]
        )

    def tearDown(self) -> None:
        frappe.flags.do_not_enqueue = False

    def test_links_and_builder_paths_move_to_the_new_module(self) -> None:
        rename_cloud_mail_module.execute()
        rename_cloud_mail_module.execute()  # a second run finds nothing left to move

        self.assertFalse(frappe.db.exists("Module Def", rename_cloud_mail_module.OLD))
        self.assertEqual(frappe.db.get_value("DocType", "Mail Quota", "module"), rename_cloud_mail_module.NEW)

        for name, builder in self.upgraded.items():
            self.assertEqual(frappe.db.get_value("Server Job", name, "variables_builder"), builder)
            self.assertTrue(callable(frappe.get_attr(builder)))
        self.assertEqual(
            frappe.db.get_value("Server Job", self.current.name, "variables_builder"), VARIABLES_BUILDERS[0]
        )


class TestDropLegacyDeploymentDoctypes(IntegrationTestCase):
    """The patch runs once, on a site from the first release; run again, it must keep Server Job."""

    def run_patch(self) -> list[str]:
        with (
            patch.object(frappe, "delete_doc") as delete_doc,
            patch.object(frappe.db, "sql_ddl"),
            patch.object(drop_legacy, "drop_index_if_exists"),
        ):
            drop_legacy.execute()
        return [call.args[1] for call in delete_doc.call_args_list]

    def test_a_site_past_the_patch_keeps_server_job(self) -> None:
        self.assertTrue(drop_legacy.is_current("Server Job"))
        # A column a later release adds, not synced yet, does not make today's Server Job legacy.
        with patch.object(frappe.db, "has_column", return_value=False):
            deleted = self.run_patch()
        self.assertNotIn("Server Job", deleted)
        self.assertIn("Mail Cluster", deleted)

    def test_an_old_server_job_schema_is_dropped(self) -> None:
        exists = frappe.db.table_exists
        legacy = lambda doctype, cached=True: doctype != "Server Job Task" and exists(doctype, cached)  # noqa: E731
        with patch.object(frappe.db, "table_exists", side_effect=legacy):
            self.assertFalse(drop_legacy.is_current("Server Job"))
            self.assertIn("Server Job", self.run_patch())
