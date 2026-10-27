import frappe
from frappe.database.utils import drop_index_if_exists

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


# A table only the current DocType of the name has: the legacy Server Job had Server Job Command
# rows, today's has Server Job Task rows. Unlike its columns, it does not change in later releases.
CURRENT_MARKERS = {"Server Job": "Server Job Task"}


def execute() -> None:
    for doctype in LEGACY_DOCTYPES:
        if is_current(doctype):
            continue
        frappe.delete_doc(
            "DocType",
            doctype,
            force=True,
            ignore_missing=True,
            ignore_permissions=True,
            delete_permanently=True,
        )
        frappe.db.sql_ddl(frappe.qb.drop_table(f"tab{doctype}").if_exists().get_sql())

    # DNS Record used to be unique on (host, type); round-robin A records need several rows.
    for index in ("host_type", "unique_host_type"):
        drop_index_if_exists("tabDNS Record", index)


def is_current(doctype: str) -> bool:
    """The site already has today's DocType of this name: it is past this patch, and deleting it
    would delete today's records, and in developer mode its files."""

    marker = CURRENT_MARKERS.get(doctype)
    return bool(marker) and frappe.db.table_exists(marker, cached=False)
