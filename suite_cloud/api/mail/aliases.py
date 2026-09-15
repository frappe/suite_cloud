"""One alias at a time on an account, group or mailing list.

The site's dashboard edits aliases one row at a time; doing that through the full-replace
``set_*_aliases`` calls means two admins editing the same object at once lose each other's rows.
These helpers lock the parent row for the change instead.
"""

import frappe
from frappe import _

from suite_cloud.api.site import normalize_name, owned


def add(doctype: str, email: str, alias: str, description: str | None = None):
    """Adds ``alias``; an alias that is already there is left as it is."""

    doc = owned(doctype, email, for_update=True)
    alias = normalize_name(alias)
    if _row(doc, alias) is None:
        doc.append("aliases", {"alias_email": alias, "enabled": 1, "description": description or None})
        doc.save(ignore_permissions=True)
    return doc


def remove(doctype: str, email: str, alias: str):
    """Removes ``alias``; the primary address cannot go, an absent alias is already gone."""

    doc = owned(doctype, email, for_update=True)
    alias = normalize_name(alias)
    if alias == doc.email:
        frappe.throw(_("The primary address cannot be removed."))
    row = _row(doc, alias)
    if row is not None:
        doc.remove(row)
        doc.save(ignore_permissions=True)
    return doc


def set_enabled(doctype: str, email: str, alias: str, enabled: bool):
    doc = owned(doctype, email, for_update=True)
    alias = normalize_name(alias)
    row = _row(doc, alias)
    if row is None:
        raise frappe.DoesNotExistError(_("Alias {0} not found on {1}.").format(alias, doc.email))
    row.enabled = int(bool(enabled))
    doc.save(ignore_permissions=True)
    return doc


def _row(doc, alias: str):
    return next((row for row in doc.aliases if row.alias_email == alias), None)
