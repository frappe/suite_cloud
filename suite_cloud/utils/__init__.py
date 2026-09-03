import functools
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import frappe
from frappe import _
from frappe.utils.caching import request_cache

CONFIG_KEYS = (
    "root_domain_name",
    "default_dns_ttl",
    "public_url",
    "site_service_user",
    "stalwart_version",
    "stalwart_cli_version",
    "stalwart_download_url_template",
    "stalwart_cli_download_url_template",
    "acme_directory_url",
    "acme_contact_email",
    "server_job_timeout",
)


@request_cache
def get_config(key: str | tuple[str, ...] | None = None) -> dict[str, Any] | tuple | Any:
    """Fetches configuration values, prioritizing Suite Cloud Settings over the site config.

    The site config fallback is the ``suite_cloud`` dict in ``site_config.json``. Cached per
    request: the returned dict is shared, so callers must treat it as read-only.
    """

    site_conf = frappe.conf.suite_cloud or {}
    settings = frappe.get_cached_doc("Suite Cloud Settings")
    config = {}
    for field in CONFIG_KEYS:
        value = settings.get(field)
        # Only an unset value falls through, so a deliberate 0 in settings still wins.
        config[field] = site_conf.get(field) if value in (None, "") else value

    if not key:
        return config

    keys = (key,) if isinstance(key, str) else key
    for k in keys:
        if k not in config:
            frappe.throw(_("Suite Cloud config key '{0}' not found").format(k))

    return tuple(config[k] for k in keys) if len(keys) > 1 else config[keys[0]]


def get_public_url() -> str:
    """Returns the URL other systems use to reach this Suite Cloud site."""

    return (get_config("public_url") or frappe.utils.get_url()).rstrip("/")


def password_or_none(doc, field: str) -> str | None:
    """Returns the decrypted password if the field is set, otherwise None."""

    return doc.get_password(field) if doc.get(field) else None


def log_error(title: str | None = None, message: str | None = None, **kwargs) -> None:
    """Logs an error, prefixing the title with "[Suite Cloud]" so these errors can be filtered out."""

    prefix = "[Suite Cloud] "
    if title and not title.startswith(prefix):
        title = f"{prefix}{title}"

    frappe.log_error(title=title, message=message, **kwargs)


def enqueue_job(
    method: str | Callable, job_id: str | None = None, deduplicate: bool = False, **kwargs
) -> None:
    """Enqueues a background job, deriving a stable job id when deduplicating."""

    if deduplicate and not job_id:
        job_id = method.split(".")[-1] if isinstance(method, str) else method.__name__

    frappe.enqueue(method, job_id=job_id, deduplicate=deduplicate, **kwargs)


@contextmanager
def user_context(user: str) -> Generator[None]:
    """Temporarily switches the session user."""

    session_user = frappe.session.user
    session_sid = frappe.session.sid
    session_data = frappe.session.data.copy()
    form_dict = frappe.local.form_dict

    if session_user == user:
        yield
        return

    try:
        frappe.set_user(user)
        yield
    finally:
        # frappe.set_user() overwrites session.sid with the username and wipes session.data and
        # form_dict, so restore all three alongside the user to avoid corrupting the original
        # session. form_dict matters beyond tidiness: rate limiting keys its counter off
        # form_dict.cmd, so leaving it emptied silently unlimits the rest of the request.
        frappe.set_user(session_user)
        frappe.session.sid = session_sid
        frappe.session.data = session_data
        frappe.local.form_dict = form_dict


def reconnect_on_failure(max_retries: int = 3) -> Callable:
    """Decorator that reconnects to the database and retries when the connection drops.

    Ansible plays run for minutes and report progress through the ORM, long enough for the
    server to close an idle connection in between events.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0

            while True:
                try:
                    return func(*args, **kwargs)

                except Exception as e:
                    if not is_connection_error(e) or retries >= max_retries:
                        raise type(e)(f"{e!s} | Retries attempted: {retries}/{max_retries}") from e

                    retries += 1
                    frappe.db.connect()

        return wrapper

    return decorator


def is_connection_error(exception: Exception) -> bool:
    operational_error = getattr(frappe.db, "OperationalError", ())
    return frappe.db.is_interface_error(exception) or isinstance(exception, operational_error)
