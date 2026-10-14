import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.tests.fixtures import configure_settings, make_cluster, make_node
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
