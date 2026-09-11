"""Site-facing API: what a Frappe Suite site may do with its own slice of the directory.

Authentication is Frappe's own API-key scheme pointed at the Suite Site DocType: the site
sends ``Authorization: token <api_key>:<api_secret>`` plus ``Frappe-Authorization-Source:
Suite Site``, which makes the request run as the shared site service user. Every endpoint
then resolves the calling site from the key in the header and only ever touches documents
owned by that site. Objects of other sites are reported as missing, never as forbidden.
"""

import base64
import functools
import json
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import cint

from suite_cloud.cloud_mail.stalwart.errors import StalwartRejectedError, StalwartUnauthorizedError
from suite_cloud.utils import get_config

OWNED_DOCTYPES = {"Mail Domain", "Mail Account", "Mail Group", "Mailing List"}
MANAGER_ROLES = ("System Manager", "Suite Cloud Manager")
RATE_LIMIT = 300  # requests per site per minute


class SiteAuthError(frappe.AuthenticationError):
    pass


class SiteSuspendedError(frappe.PermissionError):
    pass


class SiteAddressError(frappe.PermissionError):
    """The key is valid but the request did not come from one of the site's allowed addresses."""


class StalwartRejected(frappe.ValidationError):
    """Stalwart refused the change; the type/description are safe to show the caller."""

    http_status_code = 422


class ClusterMisconfiguredError(frappe.ValidationError):
    """Suite Cloud's own credentials for the cluster are wrong: an operator problem."""

    http_status_code = 502


def current_site():
    """The Suite Site behind this request (cached for the request)."""

    if site := getattr(frappe.local, "suite_site", None):
        return site

    site = _resolve_site()
    frappe.local.suite_site = site
    return site


def _resolve_site():
    api_key = _api_key_from_header()
    service_user = get_config("site_service_user")

    if frappe.session.user == service_user and api_key:
        name = frappe.db.get_value("Suite Site", {"api_key": api_key})
    elif frappe.session.user != "Guest" and set(frappe.get_roles()) & set(MANAGER_ROLES):
        # Operators may act on behalf of a site from the desk or a script.
        name = frappe.form_dict.get("site") or frappe.get_request_header("X-Suite-Site")
    else:
        name = None

    if not name or not frappe.db.exists("Suite Site", name):
        raise SiteAuthError(_("Site authentication failed."))

    site = frappe.get_cached_doc("Suite Site", name)
    if not site.enabled or site.status != "Active":
        raise SiteSuspendedError(_("Site {0} is {1}.").format(site.name, site.status.lower()))
    request_ip = getattr(frappe.local, "request_ip", None)
    if frappe.session.user == service_user and not site.allows_ip(request_ip):
        # A key copied out of a site's config is worthless from anywhere but the site's own servers.
        # Either the key has leaked or the site moved servers; operators need to know which.
        frappe.log_error(
            title=f"[Suite Cloud] {site.name}: request from an address outside its allowed list",
            message=_("Request from {0} to {1}; allowed: {2}").format(
                request_ip or _("an unknown address"),
                getattr(getattr(frappe.local, "request", None), "path", None) or "?",
                ", ".join(site.to_api()["allowed_ips"]),
            ),
        )
        raise SiteAddressError(_("Site {0} does not accept requests from this address.").format(site.name))
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


def throttle(site) -> None:
    """A fixed one-minute window per site, counted in Redis."""

    if not getattr(frappe.local, "request", None):
        return

    window = frappe.utils.now_datetime().strftime("%Y%m%d%H%M")
    key = frappe.cache.make_key(f"suite_cloud:ratelimit:{site.name}:{window}")
    count = frappe.cache.incr(key)
    if count == 1:
        frappe.cache.expire(key, 90)
    if count > RATE_LIMIT:
        raise frappe.TooManyRequestsError(
            _("Rate limit of {0} requests per minute exceeded.").format(RATE_LIMIT)
        )


def site_api(fn: Callable) -> Callable:
    """Resolves the site, throttles, and turns Stalwart errors into API-shaped exceptions."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        throttle(current_site())
        try:
            return fn(*args, **kwargs)
        except StalwartRejectedError as e:
            raise StalwartRejected(_("The mail server rejected the change: {0}").format(_describe(e))) from e
        except StalwartUnauthorizedError as e:
            frappe.log_error(title="[Suite Cloud] Cluster credentials rejected", message=str(e))
            raise ClusterMisconfiguredError(_("The mail cluster refused Suite Cloud's credentials.")) from e

    return wrapper


def _describe(error: StalwartRejectedError) -> str:
    if error.error_type and error.description:
        return f"{error.error_type} ({error.description})"
    return error.error_type or error.description or "unknown error"


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

    site = current_site()
    if doctype not in OWNED_DOCTYPES:
        raise ValueError(doctype)

    name = normalize_name(name)
    doc = None
    if name and frappe.db.exists(doctype, name):
        doc = frappe.get_doc(doctype, name, for_update=for_update)
    if doc is None or doc.site != site.name:
        raise frappe.DoesNotExistError(_("{0} {1} not found.").format(_(doctype), name))
    return doc


def owned_names(doctype: str, filters: dict | None = None, **kwargs) -> list[str]:
    filters = {"site": current_site().name, **(filters or {})}
    return frappe.get_all(doctype, filters=filters, pluck="name", order_by="name asc", **kwargs)


def owned_page(
    doctype: str,
    search: str | None,
    start: Any,
    limit: Any,
    cap: int,
    search_fields: tuple[str, ...] = ("name", "description"),
    filters: dict | None = None,
) -> tuple[list[str], int]:
    """One page of the site's document names by name, plus how many match in all."""

    filters = {"site": current_site().name, **(filters or {})}
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
        order_by="name asc",
        limit_start=max(cint(start), 0),
        limit_page_length=page_size(limit, cap),
    )
    return names, cint(total)


def as_alias_rows(value: Any) -> list[dict]:
    """Aliases arrive as addresses, or as ``{email, enabled, description}`` objects; rows come out.

    A JSON string is accepted too, so a form can post either shape.
    """

    if isinstance(value, str) and value.strip().startswith("["):
        value = frappe.parse_json(value)
    rows = []
    for item in as_list(value) if not isinstance(value, list) else value:
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


def as_list(value: Any) -> list[str]:
    """Accepts a JSON list, a comma/newline separated string or None."""

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            # Form-encoded clients (FrappeClient, query strings) send lists as JSON text.
            try:
                value = json.loads(text)
            except ValueError:
                frappe.throw(_("Expected a list."))
        else:
            value = text.replace("\n", ",").split(",")
    return [str(v).strip() for v in value if str(v).strip()]


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def ping() -> dict:
    """Confirms the credentials and returns where the site's mail lives."""

    return current_site().to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def update_site_profile(title: str | None = None, contact_email: str | None = None) -> dict:
    """What the site says about itself: its workspace name as the title, and where to reach it.

    Only the fields passed change; an empty string clears the contact and resets the title to the
    site name.
    """

    site = current_site()
    if title is not None:
        site.title = title.strip()
    if contact_email is not None:
        contact_email = contact_email.strip().lower()
        if contact_email:
            frappe.utils.validate_email_address(contact_email, throw=True)
        site.contact_email = contact_email or None
    site.save(ignore_permissions=True)
    return site.to_api()
