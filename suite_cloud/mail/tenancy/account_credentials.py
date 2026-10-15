"""An account's app passwords and API keys: the cluster holds them, Mail Credential rows mirror them.

A row without a credential id is created on the cluster when the account saves and its secret is
shown once; a row taken out is revoked once the save commits. The description "Suite Cloud" is
reserved for the credential Suite Cloud itself mints: the app password is the Suite site's login
and is always present and first; the API key exists only once an operator rotates it.
"""

import ipaddress
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import escape_html, now_datetime

from suite_cloud.mail.stalwart.credentials import Credential
from suite_cloud.mail.stalwart.errors import StalwartError, StalwartRejectedError
from suite_cloud.utils import local_datetime, utc_iso

SITE_LOGIN = "Suite Cloud"
DEFAULT_LIMIT = 10
PROPERTIES = ["id", "description", "createdAt", "expiresAt", "allowedIps"]


@dataclass(frozen=True)
class Kind:
    field: str  # the table on Mail Account, also the service on the account client
    quota: str
    label: str
    plural: str
    always_present: bool  # the Suite Cloud row is minted whenever it is missing


APP_PASSWORDS = Kind("app_passwords", "maxAppPasswords", "App password", "app passwords", always_present=True)
API_KEYS = Kind("api_keys", "maxApiKeys", "API key", "API keys", always_present=False)
KINDS = (APP_PASSWORDS, API_KEYS)


def apply_default_limits(doc) -> None:
    """A new account gets the default limits unless the caller set them."""

    for kind in KINDS:
        if kind.quota not in doc.quota_map():
            doc.append("quotas", {"quota": kind.quota, "value": DEFAULT_LIMIT})


def validate(doc) -> None:
    """Well-formed IPs, the Suite Cloud row only where Suite Cloud put it, and rows within the limit."""

    before = doc.get_doc_before_save()
    for kind in KINDS:
        if before is not None and not before.get(kind.field) and doc.stalwart_id and not is_loaded(doc, kind):
            load(doc, kind)  # an account mirrored for the first time starts from the cluster's list
        previous = baseline(doc, before, kind)
        for row in doc.get(kind.field):
            row.allowed_ips = "\n".join(parse_ips(row.allowed_ips))
            was = previous.get(row.credential_id)
            if was and was.description == SITE_LOGIN != row.description:
                frappe.throw(_("Rotate the {0} {1} instead of renaming it.").format(SITE_LOGIN, kind.label))
            renamed = was is not None and was.description != SITE_LOGIN
            if (
                row.description == SITE_LOGIN
                and not row.flags.site_login
                and (renamed or not row.credential_id)
            ):
                frappe.throw(
                    _("{0} is reserved for the {1} Suite Cloud mints itself.").format(SITE_LOGIN, kind.label)
                )

        limit = doc.quota_map().get(kind.quota)
        place_site_login(doc, kind, limit)
        if limit and len(doc.get(kind.field)) > limit:
            frappe.throw(_("The account may have at most {0} {1}.").format(limit, kind.plural))


def place_site_login(doc, kind: Kind, limit: int | None) -> None:
    rows = [r for r in doc.get(kind.field) if r.description == SITE_LOGIN]
    if len(rows) > 1:
        frappe.throw(_("{0} is listed twice.").format(SITE_LOGIN))
    if rows:
        login = rows[0]
    elif kind.always_present:
        login = append_site_login(doc, kind)
        make_room(doc, kind, limit)
    else:
        return
    reorder(doc, kind, [login, *[r for r in doc.get(kind.field) if r is not login]])


def append_site_login(doc, kind: Kind):
    row = doc.append(kind.field, {"description": SITE_LOGIN})
    row.flags.site_login = True
    return row


def make_room(doc, kind: Kind, limit: int | None) -> None:
    """The site's login outranks the oldest of the other credentials when the account is full."""

    while limit and len(doc.get(kind.field)) > limit:
        existing = [r for r in doc.get(kind.field) if r.credential_id and r.description != SITE_LOGIN]
        if not existing:
            return
        doc.get(kind.field).remove(min(existing, key=lambda r: str(r.created_at or "")))


def reorder(doc, kind: Kind, rows: list) -> None:
    doc.set(kind.field, [])
    doc.extend(kind.field, rows)
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


def rotate_site_login(doc, kind: Kind) -> str:
    """Mints the Suite Cloud credential anew; the previous one is revoked once the save commits."""

    if doc.stalwart_id and not doc.get(kind.field):
        load(doc, kind)  # an account mirrored for the first time: the old credential must be known to go
    doc.set(kind.field, [r for r in doc.get(kind.field) if r.description != SITE_LOGIN])
    if not kind.always_present:
        append_site_login(doc, kind)
    doc.flags.secrets_in_response = True
    doc.save(ignore_permissions=True)
    return site_login_secret(doc, kind)


def push(doc, before) -> None:
    """Creates, updates and revokes on the cluster what the save changed."""

    for kind in KINDS:
        push_kind(doc, before, kind)


def push_kind(doc, before, kind: Kind) -> None:
    rows = doc.get(kind.field)
    previous = baseline(doc, before, kind)
    kept = {r.credential_id for r in rows if r.credential_id}
    added = [r for r in rows if not r.credential_id]
    changed = [
        r for r in rows if r.credential_id in previous and fields(previous[r.credential_id]) != fields(r)
    ]
    gone = [credential_id for credential_id in previous if credential_id not in kept]
    if not (added or changed or gone):
        return

    service = getattr(doc.account_client(), kind.field)
    if added:
        # A credential created before a later step fails outlives the rollback; it goes with it.
        created: list[str] = []
        frappe.db.after_rollback.add(lambda: revoke(service, created))
        for row in added:
            created.append(create(service, row, doc, kind))
    for row in changed:
        service.update(row.credential_id, patch(row))
    if gone:
        # Only once the save is committed: a rollback would otherwise keep rows for credentials
        # that no longer exist, and a rotation would cut the site off before it has the new secret.
        frappe.db.after_commit.add(lambda: revoke(service, gone))


def create(service, row, doc, kind: Kind) -> str:
    credential_id, secret = service.create_secret(credential(row))
    row.db_set({"credential_id": credential_id, "created_at": now_datetime()}, update_modified=False)
    doc.flags.setdefault("credential_secrets", {})[row.name] = secret
    if not doc.flags.secrets_in_response:
        frappe.msgprint(
            _("{0} for {1}: {2}. It is shown only once.").format(
                kind.label, escape_html(row.description), f"<code>{escape_html(secret)}</code>"
            ),
            title=kind.label,
        )
    return credential_id


def revoke(service, ids: list[str]) -> None:
    if not ids:
        return
    try:
        service.delete(ids)
    except StalwartRejectedError as e:
        if e.error_type != "notFound":
            raise


def load(doc, kind: Kind) -> None:
    """Replaces the mirrored rows with the cluster's list, keeping rows not created there yet."""

    live = getattr(doc.account_client(), kind.field).get_all(properties=PROPERTIES)
    rows = sorted(
        (as_row(c) for c in live), key=lambda r: (r["description"] != SITE_LOGIN, str(r["created_at"]))
    )
    doc.flags.setdefault("credential_baseline", {})[kind.field] = [frappe._dict(r) for r in rows]
    pending = [r for r in doc.get(kind.field) if not r.credential_id]
    doc.set(kind.field, rows)
    reorder(doc, kind, [*doc.get(kind.field), *pending])


def is_loaded(doc, kind: Kind) -> bool:
    return kind.field in (doc.flags.credential_baseline or {})


def baseline(doc, before, kind: Kind) -> dict:
    """What the cluster held before this save, by id: the list a load fetched in this request,
    else the saved rows. A credential taken out of it is revoked; one changed in it is updated."""

    rows = (doc.flags.credential_baseline or {}).get(kind.field) or (before.get(kind.field) if before else [])
    return {r.credential_id: r for r in rows if r.credential_id}


def refresh(doc) -> None:
    """The form opens on the cluster's lists; the rows are rewritten when they differ from it."""

    if not doc.stalwart_id:
        return
    try:
        changed = [kind for kind in KINDS if refresh_kind(doc, kind)]
    except StalwartError as e:
        frappe.log_error(title="[Suite Cloud] Credential lookup failed", message=str(e))
        frappe.msgprint(
            _("The mail server did not answer; the app passwords and API keys shown may be stale."),
            indicator="orange",
        )
        return
    if not changed:
        return

    frappe.db.get_value(doc.doctype, doc.name, "name", for_update=True)
    for kind in changed:
        for row in doc.get(kind.field):
            row.owner = row.modified_by = frappe.session.user
            row.creation = row.modified = now_datetime()
        doc.update_child_table(kind.field)
    frappe.local.flags.commit = True  # a form load is a GET, which Frappe otherwise rolls back


def refresh_kind(doc, kind: Kind) -> bool:
    mirrored = [fields(r) for r in doc.get(kind.field)]
    load(doc, kind)
    return [fields(r) for r in doc.get(kind.field)] != mirrored


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
    """The API shape of one credential; the secret is never part of it."""

    return {
        "id": row.credential_id,
        "description": row.description,
        "expires_at": utc_iso(row.expires_at),
        "allowed_ips": parse_ips(row.allowed_ips),
        "created_at": utc_iso(row.created_at),
    }


def row_by_id(doc, kind: Kind, credential_id: str):
    """A saved row, or one the cluster holds that this side has not mirrored yet."""

    row = find_row(doc, kind, credential_id)
    if row is None and doc.stalwart_id and not is_loaded(doc, kind):
        load(doc, kind)
        row = find_row(doc, kind, credential_id)
    if row is None:
        frappe.throw(_("{0} {1} was not found.").format(kind.label, credential_id), frappe.DoesNotExistError)
    return row


def find_row(doc, kind: Kind, credential_id: str):
    return next((r for r in doc.get(kind.field) if r.credential_id == credential_id), None)


def site_login_secret(doc, kind: Kind) -> str:
    row = next(r for r in doc.get(kind.field) if r.description == SITE_LOGIN)
    return doc.flags.credential_secrets[row.name]
