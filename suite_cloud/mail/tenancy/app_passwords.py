"""An account's app passwords: the cluster holds them, Mail App Password rows mirror them.

A row without a credential id is created on the cluster when the account saves and its secret is
shown once; a row taken out is revoked once the save commits. The row described "Suite Cloud" is
the Suite site's own login: always present and always first.
"""

import ipaddress

import frappe
from frappe import _
from frappe.utils import escape_html, now_datetime

from suite_cloud.mail.stalwart.credentials import Credential
from suite_cloud.mail.stalwart.errors import StalwartError, StalwartRejectedError
from suite_cloud.utils import local_datetime, utc_iso

SITE_LOGIN = "Suite Cloud"
QUOTA = "maxAppPasswords"
DEFAULT_LIMIT = 10
PROPERTIES = ["id", "description", "createdAt", "expiresAt", "allowedIps"]


def apply_default_limit(doc) -> None:
    """A new account gets the default limit unless the caller set one."""

    if QUOTA not in doc.quota_map():
        doc.append("quotas", {"quota": QUOTA, "value": DEFAULT_LIMIT})


def validate(doc) -> None:
    """Well-formed IPs, one Suite Cloud row on top, and no more rows than the limit allows."""

    before = doc.get_doc_before_save()
    if before is not None and not before.app_passwords and doc.stalwart_id:
        load(doc)  # an account mirrored for the first time starts from the cluster's list
    previous = {r.credential_id: r for r in before.app_passwords if r.credential_id} if before else {}
    for row in doc.app_passwords:
        row.allowed_ips = "\n".join(parse_ips(row.allowed_ips))
        was = previous.get(row.credential_id)
        if was and was.description == SITE_LOGIN != row.description:
            frappe.throw(_("Rotate the {0} app password instead of renaming it.").format(SITE_LOGIN))

    limit = doc.quota_map().get(QUOTA)
    place_site_login(doc, limit)
    if limit and len(doc.app_passwords) > limit:
        frappe.throw(_("The account may have at most {0} app passwords.").format(limit))


def place_site_login(doc, limit: int | None) -> None:
    rows = [r for r in doc.app_passwords if r.description == SITE_LOGIN]
    if len(rows) > 1:
        frappe.throw(_("{0} is listed twice.").format(SITE_LOGIN))
    if rows:
        login = rows[0]
    else:
        login = doc.append("app_passwords", {"description": SITE_LOGIN})
        make_room(doc, limit)
    reorder(doc, [login, *[r for r in doc.app_passwords if r is not login]])


def make_room(doc, limit: int | None) -> None:
    """The site's login outranks the oldest of the other passwords when the account is full."""

    while limit and len(doc.app_passwords) > limit:
        existing = [r for r in doc.app_passwords if r.credential_id and r.description != SITE_LOGIN]
        if not existing:
            return
        doc.app_passwords.remove(min(existing, key=lambda r: str(r.created_at or "")))


def reorder(doc, rows: list) -> None:
    doc.app_passwords = rows
    for index, row in enumerate(rows, 1):
        row.idx = index


def parse_ips(text: str | None) -> list[str]:
    """One address or network per line (commas work too); anything else stops before the cluster."""

    ips = []
    for line in (text or "").replace(",", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ipaddress.ip_network(line, strict=False)
        except ValueError:
            frappe.throw(_("{0} is not an IP address or network.").format(line))
        ips.append(line)
    return ips


# --- cluster ------------------------------------------------------------------------------------


def push(doc, before) -> None:
    """Creates, updates and revokes on the cluster what the save changed."""

    previous = {r.credential_id: r for r in (before.app_passwords if before else []) if r.credential_id}
    kept = {r.credential_id for r in doc.app_passwords if r.credential_id}
    added = [r for r in doc.app_passwords if not r.credential_id]
    changed = [
        r
        for r in doc.app_passwords
        if r.credential_id in previous and fields(previous[r.credential_id]) != fields(r)
    ]
    gone = [credential_id for credential_id in previous if credential_id not in kept]
    if not (added or changed or gone):
        return

    service = doc.account_client().app_passwords
    for row in added:
        create(service, row, doc)
    for row in changed:
        service.update(row.credential_id, patch(row))
    if gone:
        # Only once the save is committed: a rollback would otherwise keep rows for credentials
        # that no longer exist, and a rotation would cut the site off before it has the new secret.
        frappe.db.after_commit.add(lambda: revoke(service, gone))


def create(service, row, doc) -> None:
    credential_id, secret = service.create_secret(credential(row))
    row.db_set({"credential_id": credential_id, "created_at": now_datetime()}, update_modified=False)
    doc.flags.setdefault("app_password_secrets", {})[row.name] = secret
    if not doc.flags.secrets_in_response:
        frappe.msgprint(
            _("App password for {0}: {1}. It is shown only once.").format(
                escape_html(row.description), f"<code>{escape_html(secret)}</code>"
            ),
            title=_("App Password"),
        )


def revoke(service, ids: list[str]) -> None:
    try:
        service.delete(ids)
    except StalwartRejectedError as e:
        if e.error_type != "notFound":
            raise


def load(doc) -> None:
    """Replaces the mirrored rows with the cluster's list, keeping rows not created there yet."""

    live = doc.account_client().app_passwords.get_all(properties=PROPERTIES)
    rows = sorted(
        (as_row(c) for c in live), key=lambda r: (r["description"] != SITE_LOGIN, str(r["created_at"]))
    )
    pending = [r for r in doc.app_passwords if not r.credential_id]
    doc.set("app_passwords", rows)
    reorder(doc, [*doc.app_passwords, *pending])


def refresh(doc) -> None:
    """The form opens on the cluster's list; the rows are rewritten when they differ from it."""

    if not doc.stalwart_id:
        return
    mirrored = [fields(r) for r in doc.app_passwords]
    try:
        load(doc)
    except StalwartError as e:
        frappe.log_error(title="[Suite Cloud] App password lookup failed", message=str(e))
        frappe.msgprint(
            _("The mail server did not answer; the app passwords shown may be stale."), indicator="orange"
        )
        return
    if [fields(r) for r in doc.app_passwords] == mirrored:
        return

    frappe.db.get_value(doc.doctype, doc.name, "name", for_update=True)
    for row in doc.app_passwords:
        row.owner = row.modified_by = frappe.session.user
        row.creation = row.modified = now_datetime()
    doc.update_child_table("app_passwords")
    frappe.local.flags.commit = True  # a form load is a GET, which Frappe otherwise rolls back


# --- shapes -------------------------------------------------------------------------------------


def credential(row) -> Credential:
    return Credential(
        description=row.description,
        expires_at=utc_iso(row.expires_at),
        allowed_ips=parse_ips(row.allowed_ips),
    )


def patch(row) -> dict:
    return {
        "description": row.description,
        "expiresAt": utc_iso(row.expires_at),
        "allowedIps": parse_ips(row.allowed_ips),
    }


def fields(row) -> tuple:
    return (row.credential_id, row.description, utc_iso(row.expires_at), row.allowed_ips or "")


def as_row(live: dict) -> dict:
    return {
        "credential_id": live["id"],
        "description": live.get("description") or live["id"],
        "expires_at": local_datetime(live.get("expiresAt")),
        "allowed_ips": "\n".join(live.get("allowedIps") or []),
        "created_at": local_datetime(live.get("createdAt")),
    }


def payload(row) -> dict:
    """The API shape of one app password; the secret is never part of it."""

    return {
        "id": row.credential_id,
        "description": row.description,
        "expires_at": utc_iso(row.expires_at),
        "allowed_ips": parse_ips(row.allowed_ips),
        "created_at": utc_iso(row.created_at),
    }


def row_by_id(doc, credential_id: str):
    row = next((r for r in doc.app_passwords if r.credential_id == credential_id), None)
    if row is None:
        frappe.throw(_("App password {0} was not found.").format(credential_id), frappe.DoesNotExistError)
    return row


def site_login_secret(doc) -> str:
    row = next(r for r in doc.app_passwords if r.description == SITE_LOGIN)
    return doc.flags.app_password_secrets[row.name]
