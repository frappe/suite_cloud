"""Pushes directory documents to their cluster.

Every push runs inside the document's own save, so a Stalwart refusal rolls the database
change back. Ids come back from Stalwart and are stored on the document.
"""

from typing import TYPE_CHECKING

import frappe
from frappe import _

from suite_cloud.stalwart import get_client
from suite_cloud.stalwart.directory import EmailAlias
from suite_cloud.stalwart.errors import StalwartRejectedError
from suite_cloud.tenancy.addresses import assert_address_available, validate_email_address

if TYPE_CHECKING:
    from frappe.model.document import Document


def client_for(doc: Document):
    cluster = frappe.get_cached_doc("Stalwart Cluster", doc.cluster)
    return get_client(cluster)


def push_create(doc: Document, service: str, payload) -> str:
    created = getattr(client_for(doc), service).create(payload)
    doc.db_set("stalwart_id", created["id"], update_modified=False)
    return created["id"]


def push_update(doc: Document, service: str, patch: dict) -> None:
    getattr(client_for(doc), service).update(doc.stalwart_id, patch)


def push_destroy(doc: Document, service: str) -> None:
    """A missing object is fine: Suite Cloud is the source of truth and is deleting anyway."""

    if not doc.stalwart_id:
        return
    try:
        getattr(client_for(doc), service).delete(doc.stalwart_id)
    except StalwartRejectedError as e:
        if e.error_type != "notFound":
            raise


# --- shared payload pieces ----------------------------------------------------------------


def domain_stalwart_id(domain_name: str) -> str:
    stalwart_id = frappe.db.get_value("Mail Domain", domain_name, "stalwart_id")
    if not stalwart_id:
        frappe.throw(_("Domain {0} is not registered on the cluster yet.").format(domain_name))
    return stalwart_id


def validate_aliases(doc: Document) -> None:
    """Aliases must sit on one of the site's domains and be free everywhere."""

    seen = set()
    for row in doc.aliases:
        row.alias_email = validate_email_address(row.alias_email)
        if row.alias_email == doc.email:
            frappe.throw(_("{0} is already the primary address.").format(row.alias_email))
        if row.alias_email in seen:
            frappe.throw(_("Alias {0} is listed twice.").format(row.alias_email))
        seen.add(row.alias_email)

        alias_domain = row.alias_email.split("@", 1)[1]
        if frappe.db.get_value("Mail Domain", alias_domain, "site") != doc.site:
            frappe.throw(_("Alias domain {0} does not belong to this site.").format(alias_domain))
        assert_address_available(row.alias_email, exclude=(doc.doctype, doc.name))


def aliases(doc: Document) -> list[EmailAlias]:
    return [
        EmailAlias(
            name=row.alias_email.split("@", 1)[0],
            domain_id=domain_stalwart_id(row.alias_email.split("@", 1)[1]),
            enabled=bool(row.enabled),
            description=row.description or None,
        )
        for row in doc.aliases
    ]


def aliases_payload(doc: Document) -> dict:
    return {str(i): alias.to_dict() for i, alias in enumerate(aliases(doc))}


def aliases_changed(before: Document, after: Document) -> bool:
    key = lambda rows: sorted((r.alias_email, bool(r.enabled), r.description or "") for r in rows)  # noqa: E731
    return key(before.aliases) != key(after.aliases)


def group_ids(doc: Document) -> list[str]:
    ids = []
    for row in doc.groups:
        stalwart_id = frappe.db.get_value("Mail Group", row.group, "stalwart_id")
        if not stalwart_id:
            frappe.throw(_("Group {0} is not registered on the cluster yet.").format(row.group))
        ids.append(stalwart_id)
    return ids


def group_ids_payload(doc: Document) -> dict:
    return {gid: True for gid in group_ids(doc)}
