import json
from pathlib import Path

import frappe
from frappe.database.utils import drop_index_if_exists
from frappe.model import no_value_fields

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


SHIPPED = {"Server Job": ("suite_cloud", "doctype", "server_job", "server_job.json")}


def execute() -> None:
    for doctype in LEGACY_DOCTYPES:
        if has_shipped_schema(doctype):
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


def has_shipped_schema(doctype: str) -> bool:
    """The table already has every field the app ships for this name: the site is past this patch,
    and deleting the DocType would delete today's records, and in developer mode its files."""

    if doctype not in SHIPPED or not frappe.db.table_exists(doctype, cached=False):
        return False
    path = Path(frappe.get_app_path("suite_cloud", *SHIPPED[doctype]))
    fields = json.loads(path.read_text())["fields"]
    columns = [field["fieldname"] for field in fields if field["fieldtype"] not in no_value_fields]
    return all(frappe.db.has_column(doctype, column) for column in columns)
