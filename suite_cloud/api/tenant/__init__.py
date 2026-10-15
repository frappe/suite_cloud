"""Tenant-facing API: what a Frappe Suite site may do with its own slice of the directory.

Authentication is Frappe's own API-key scheme pointed at the Suite Tenant DocType: the site
sends ``Authorization: token <api_key>:<api_secret>`` plus ``Frappe-Authorization-Source:
Suite Tenant``, which makes the request run as the shared site service user. Every endpoint
then resolves the calling site from the key in the header and only ever touches documents
owned by that site. Objects of other sites are reported as missing, never as forbidden.
"""

import base64
import functools
import inspect
import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import cint, get_datetime, sbool

from suite_cloud.mail.stalwart.errors import (
    StalwartError,
    StalwartRejectedError,
    StalwartUnauthorizedError,
    StalwartUnavailableError,
)
from suite_cloud.mail.tenancy.addresses import NOUNS
from suite_cloud.utils import get_config

API_PATH = "/api/v2/method/suite_cloud.api."
ERROR_KEYS = ("type", "message", "exception")

OWNED_DOCTYPES = {"Mail Domain", "Mail Account", "Mail Group", "Mailing List"}
MANAGER_ROLES = ("System Manager", "Suite Cloud Manager")
RATE_LIMIT = 300  # requests per site per minute
ALIAS_CAP = 100  # aliases on one object; more than that is a list, not an account
MEMBERSHIP_CAP = 500  # groups or members named in one request
RECIPIENT_BATCH = 5000  # recipients added or removed in one request


class TenantAuthError(frappe.AuthenticationError):
    pass


class TenantSuspendedError(frappe.PermissionError):
    pass


class TenantAddressError(frappe.PermissionError):
    """The key is valid but the request did not come from one of the site's allowed addresses."""


class InvalidInputError(frappe.ValidationError):
    """The request itself is malformed: a value the API could not even parse."""

    http_status_code = 400


class ClusterRejectedError(frappe.ValidationError):
    """Stalwart refused the change; its description is safe to show the caller."""

    http_status_code = 422


class ClusterUnavailableError(frappe.ValidationError):
    """The mail server could not be reached; the same request can be tried again."""

    http_status_code = 503


class ClusterFailedError(frappe.ValidationError):
    """The mail server failed in a way the caller cannot fix; the detail goes to the Error Log."""

    http_status_code = 502


class ClusterMisconfiguredError(frappe.ValidationError):
    """Suite Cloud's own credentials for the cluster are wrong: an operator problem."""

    http_status_code = 502


def current_tenant():
    """The Suite Tenant behind this request (cached for the request)."""

    if site := getattr(frappe.local, "suite_tenant", None):
        return site

    site = _resolve_tenant()
    frappe.local.suite_tenant = site
    return site


def _resolve_tenant():
    api_key = _api_key_from_header()
    service_user = get_config("site_service_user")

    if frappe.session.user == service_user and api_key:
        name = frappe.db.get_value("Suite Tenant", {"api_key": api_key})
    elif frappe.session.user != "Guest" and set(frappe.get_roles()) & set(MANAGER_ROLES):
        # Operators may act on behalf of a site from the desk or a script.
        name = frappe.form_dict.get("tenant") or frappe.get_request_header("X-Suite-Tenant")
    else:
        name = None

    request_ip = getattr(frappe.local, "request_ip", None)
    if not name or not frappe.db.exists("Suite Tenant", name):
        throttle(f"address:{request_ip or 'unknown'}")  # guessing keys is paced like everything else
        frappe.throw(_("Tenant authentication failed."), TenantAuthError)

    site = frappe.get_cached_doc("Suite Tenant", name)
    throttle(f"site:{site.name}")  # counted before any refusal, so refusals cannot be free
    if not site.enabled or site.status != "Active":
        frappe.throw(
            _("Tenant {0} is {1} and cannot use the API.").format(site.name, site.status.lower()),
            TenantSuspendedError,
        )
    if frappe.session.user == service_user and not site.allows_ip(request_ip):
        # A key copied out of a site's config is worthless from anywhere but the site's own servers.
        # Either the key has leaked or the site moved servers; operators need to know which.
        log_refusal_once(site.name, request_ip, ", ".join((site.allowed_ips or "").split("\n")))
        frappe.throw(
            _("Tenant {0} does not accept requests from this address.").format(site.name), TenantAddressError
        )
    return site


def _api_key_from_header() -> str | None:
    scheme, _, credential = frappe.get_request_header("Authorization", "").partition(" ")
    if scheme.lower() == "token":
        return credential.split(":", 1)[0] or None
    if scheme.lower() == "basic":
        try:
            return base64.b64decode(credential).decode().split(":", 1)[0] or None
        except Exception:
            return None
    return None


def throttle(subject: str) -> None:
    """A fixed one-minute window per subject (a site, or an address that failed to authenticate)."""

    if not getattr(frappe.local, "request", None):
        return

    window = frappe.utils.now_datetime().strftime("%Y%m%d%H%M")
    key = frappe.cache.make_key(f"suite_cloud:ratelimit:{subject}:{window}")
    count = frappe.cache.incr(key)
    if count == 1:
        frappe.cache.expire(key, 90)
    if count > RATE_LIMIT:
        frappe.throw(
            _("The site sent more than {0} requests in a minute. Wait a moment and try again.").format(
                RATE_LIMIT
            ),
            frappe.TooManyRequestsError,
        )


def log_refusal_once(site_name: str, request_ip: str | None, allowed: str) -> None:
    """One Error Log per site and address per ten minutes: a flood must not drown the signal,
    and the row is inserted on the side so the refusal's rollback cannot discard it."""

    key = f"suite_cloud:refused:{site_name}:{request_ip or 'unknown'}"
    if frappe.cache.get_value(key):
        return
    frappe.cache.set_value(key, 1, expires_in_sec=600)
    frappe.log_error(
        title=f"[Suite Cloud] {site_name}: request from an address outside its allowed list",
        message=_("Request from {0} to {1}; allowed: {2}").format(
            request_ip or _("an unknown address"),
            getattr(getattr(frappe.local, "request", None), "path", None) or "?",
            allowed,
        ),
        defer_insert=True,
    )


def tenant_api(fn: Callable) -> Callable:
    """Resolves the site, throttles, and turns refusals into REST-shaped errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        current_tenant()  # resolves and throttles once per request
        assert_arguments(fn, args, kwargs)
        with api_errors():
            return fn(*args, **kwargs)

    return wrapper


def assert_arguments(fn: Callable, args: tuple, kwargs: dict) -> None:
    """A required parameter the request left out is a 400 that names it, not Python's TypeError."""

    parameters = list(inspect.signature(fn).parameters.values())[len(args) :]
    for parameter in parameters:
        if parameter.default is parameter.empty and parameter.name not in kwargs:
            frappe.throw(_("The {0} field is required.").format(parameter.name), InvalidInputError)


@contextmanager
def api_errors():
    """The mail server's refusals become 422, its outages 503 and its other failures 502. A refusal
    of Suite Cloud's own leaves as 422 too: Frappe's 417 for a validation error means nothing to a
    REST client."""

    try:
        yield
    except StalwartRejectedError as e:
        reason = e.description or e.error_type or _("no reason was given")
        frappe.throw(_("The mail server did not accept the change: {0}").format(reason), ClusterRejectedError)
    except StalwartUnavailableError:
        frappe.throw(
            _("The mail server is not reachable right now. Try again in a few minutes."),
            ClusterUnavailableError,
        )
    except StalwartUnauthorizedError as e:
        frappe.log_error(title="[Suite Cloud] Cluster credentials rejected", message=str(e))
        frappe.throw(
            _("The mail server did not accept Suite Cloud's credentials. Contact the Suite Cloud operators."),
            ClusterMisconfiguredError,
        )
    except StalwartError as e:
        frappe.log_error(title="[Suite Cloud] Cluster call failed", message=str(e))
        frappe.throw(
            _("The mail server could not complete the change. Contact the Suite Cloud operators."),
            ClusterFailedError,
        )
    except frappe.ValidationError as e:
        if e.http_status_code == 417:
            e.http_status_code = 422
        raise


def trim_errors(response, request) -> None:
    """Only the documented keys of an error leave the API: Frappe's desk hints (title, indicator,
    as_table) mean nothing to a REST client."""

    if not request.path.startswith(API_PATH) or response.status_code < 400 or not response.is_json:
        return
    body = response.get_json(silent=True)
    if not body or "errors" not in body:
        return
    body["errors"] = [{k: v for k, v in error.items() if k in ERROR_KEYS} for error in body["errors"]]
    response.set_data(json.dumps(body))


def normalize_name(name: str | None) -> str:
    """A domain or address the way it is stored: lowercase, the domain part IDNA-encoded."""

    name = (name or "").strip().lower()
    local, at, domain = name.rpartition("@")
    try:
        domain = domain.encode("idna").decode()
    except UnicodeError:
        pass  # a bad domain simply does not match anything
    return f"{local}@{domain}" if at else domain


def owned(doctype: str, name: str, for_update: bool = False):
    """Loads one of the site's documents; anything else is a 404.

    ``for_update`` locks the row until the request commits, for read-modify-write changes such
    as adding one alias, so two concurrent edits cannot drop each other's rows.
    """

    site = current_tenant()
    if doctype not in OWNED_DOCTYPES:
        raise ValueError(doctype)

    name = normalize_name(name)
    doc = None
    if name and frappe.db.exists(doctype, name):
        doc = frappe.get_doc(doctype, name, for_update=for_update)
    if doc is None or doc.site != site.name:
        frappe.throw(
            _("{0} {1} was not found.").format(_(NOUNS[doctype]).capitalize(), name), frappe.DoesNotExistError
        )
    return doc


def owned_names(doctype: str, filters: dict | None = None, **kwargs) -> list[str]:
    filters = {"site": current_tenant().name, **(filters or {})}
    return frappe.get_all(doctype, filters=filters, pluck="name", order_by="name asc", **kwargs)


def owned_page(
    doctype: str,
    search: str | None,
    start: Any,
    limit: Any,
    cap: int,
    search_fields: tuple[str, ...] = ("name", "description"),
    filters: dict | None = None,
    order_by: str = "name asc",
) -> tuple[list[str], int]:
    """One page of the site's document names by name, plus how many match in all."""

    filters = {"site": current_tenant().name, **(filters or {})}
    or_filters = None
    if search and search.strip():
        like = f"%{search.strip()}%"
        or_filters = [[field, "like", like] for field in search_fields]
    total = frappe.qb.get_query(
        doctype, filters=filters, or_filters=or_filters, fields=Count("*"), distinct=True
    ).run()[0][0]
    names = frappe.get_all(
        doctype,
        filters=filters,
        or_filters=or_filters,
        pluck="name",
        order_by=order_by,
        limit_start=max(cint(start), 0),
        limit_page_length=page_size(limit, cap),
    )
    return names, cint(total)


def as_alias_rows(value: Any) -> list[dict]:
    """Aliases arrive as addresses or as ``{email, enabled, description}`` objects; rows come out."""

    rows = []
    for item in as_items(value, ALIAS_CAP):
        if isinstance(item, dict):
            email = str(item.get("email") or item.get("alias_email") or "").strip()
            if not email:
                continue
            rows.append(
                {
                    "alias_email": email,
                    "enabled": int(bool(item.get("enabled", True))),
                    "description": item.get("description") or None,
                }
            )
        elif str(item).strip():
            rows.append({"alias_email": str(item).strip(), "enabled": 1, "description": None})
    return rows


def page_size(limit: Any, cap: int) -> int:
    """A page length between 1 and ``cap``; Frappe reads 0 as no limit, which would return every row."""

    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = cap
    return max(1, min(wanted, cap))


def as_list(value: Any, cap: int | None = None) -> list[str]:
    """The non-blank entries of a JSON array as text; ``cap`` bounds one request."""

    return [str(v).strip() for v in as_items(value, cap) if str(v).strip()]


def as_items(value: Any, cap: int | None = None) -> list:
    """A JSON array as a body carries it, or as its text in a query string; None is empty."""

    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, list):
        frappe.throw(_('Send a JSON array, for example ["a@example.com"].'), InvalidInputError)
    if cap is not None and len(value) > cap:
        frappe.throw(_("Send at most {0} entries in one request.").format(cap), InvalidInputError)
    return value


def as_datetime(value: Any) -> datetime | None:
    """A moment as ``get_datetime`` reads it, or None for nothing; anything else is a 400."""

    if not value:
        return None
    try:
        return get_datetime(value)
    except (TypeError, ValueError):
        frappe.throw(
            _("{0} is not a date. Use ISO 8601, for example 2026-03-04T05:06:07.").format(value),
            InvalidInputError,
        )


@frappe.whitelist(methods=["GET"])
@tenant_api
def ping() -> dict:
    """Confirms the credentials and returns where the site's mail lives."""

    return current_tenant().to_api()


@frappe.whitelist(methods=["GET"])
@tenant_api
def get_disk_usage(fresh: bool = False) -> dict:
    """The disk the site's accounts and groups use together, served from a short-lived cache;
    ``fresh`` asks the mail server now."""

    return current_tenant().get_disk_usage(sbool(fresh))


@frappe.whitelist(methods=["PATCH"])
@tenant_api
def update_tenant_profile(title: str | None = None, contact_email: str | None = None) -> dict:
    """What the site says about itself: its workspace name as the title, and where to reach it.

    Only the fields passed change; an empty string clears the contact and resets the title to the
    site name.
    """

    site = current_tenant()
    if title is not None:
        site.title = title.strip()
    if contact_email is not None:
        site.contact_email = contact_email.strip() or None
    site.save(ignore_permissions=True)
    return site.to_api()
