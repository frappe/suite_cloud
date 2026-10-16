"""What a tenant owner may reach.

A user with the Suite Tenant role owns one or more Suite Tenants and the tenant API runs as that
user. The role's DocType permissions open the tenant-owned DocTypes; the hooks here narrow every
read and write to the rows of the tenants the user owns. System Managers are the operators and
see everything.
"""

import frappe
from frappe import _

TENANT_ROLE = "Suite Tenant"


def is_operator(user: str) -> bool:
    return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def owned_tenants(user: str) -> list[str]:
    return frappe.get_all("Suite Tenant", {"user": user}, pluck="name")


def tenant_query_conditions(user: str | None = None) -> str:
    """Suite Tenant list: the tenants the user owns."""

    user = user or frappe.session.user
    if is_operator(user):
        return ""
    return f"`tabSuite Tenant`.`user` = {frappe.db.escape(user)}"


def tenant_document_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
    """Lists of a tenant-owned DocType: the rows of the tenants the user owns."""

    user = user or frappe.session.user
    if is_operator(user):
        return ""
    tenants = owned_tenants(user)
    if not tenants:
        return "1=0"
    names = ", ".join(frappe.db.escape(name) for name in tenants)
    return f"`tab{doctype}`.`tenant` in ({names})"


def has_tenant_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    """A Suite Tenant is its owner's, besides the operators'."""

    user = user or frappe.session.user
    return is_operator(user) or doc.user == user


def has_tenant_document_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    """A tenant-owned document belongs to whoever owns its tenant, besides the operators."""

    user = user or frappe.session.user
    return is_operator(user) or doc.tenant in owned_tenants(user)


def assert_can_own(user: str) -> None:
    if not frappe.db.exists("Has Role", {"parent": user, "role": TENANT_ROLE}):
        frappe.throw(_("{0} needs the {1} role to own a tenant.").format(user, TENANT_ROLE))


def keep_owner_role(doc, method: str | None = None) -> None:
    """User hook: whoever owns a tenant keeps the role, since the tenant's API requests run as them."""

    if TENANT_ROLE in {row.role for row in doc.roles}:
        return
    if frappe.db.exists("Suite Tenant", {"user": doc.name}):
        frappe.throw(_("{0} owns a Suite Tenant and must keep the {1} role.").format(doc.name, TENANT_ROLE))
