import frappe

# The first Suite Cloud release shipped a Docker-based single-server deployment. It was replaced
# wholesale (clusters, nodes, plans, tenancy) and nothing in it is migrated: the app had no
# production data. "Server Job" keeps its name but has a new schema, so the old record and table
# must be gone before the model sync recreates it.
LEGACY_DOCTYPES = (
    "Server Job Command",
    "Server Job",
    "Server Deployment Service",
    "Server Deployment",
    "Server Ansible Play Variable",
    "Server Ansible Play Task",
    "Server Ansible Play",
    "Mail Server",
    "Mail Cluster",
    "Mail Cluster Store HTTP Auth",
    "Mail Cluster Store",
    "Mail Directory Group Member",
    "Mail Directory Email",
    "Mail Directory Account",
)


def execute() -> None:
    for doctype in LEGACY_DOCTYPES:
        frappe.delete_doc(
            "DocType",
            doctype,
            force=True,
            ignore_missing=True,
            ignore_permissions=True,
            delete_permanently=True,
        )
        frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{doctype}`")

    # DNS Record used to be unique on (host, type); round-robin A records need several rows.
    for index in ("host_type", "unique_host_type"):
        frappe.db.sql_ddl(f"ALTER TABLE `tabDNS Record` DROP INDEX IF EXISTS `{index}`")
