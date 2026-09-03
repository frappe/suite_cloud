"""Site-facing API: what a Frappe Suite site may do with its own slice of the directory.

Authentication is Frappe's own API-key scheme pointed at the Suite Site DocType: the site
sends ``Authorization: token <api_key>:<api_secret>`` plus ``Frappe-Authorization-Source:
Suite Site``, which makes the request run as the shared site service user. Every endpoint
then resolves the calling site from the key in the header and only ever touches documents
owned by that site. Objects of other sites are reported as missing, never as forbidden.
"""

import base64
import functools
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _

from suite_cloud.stalwart.errors import StalwartRejectedError, StalwartUnauthorizedError
from suite_cloud.utils import get_config

OWNED_DOCTYPES = {"Mail Domain", "Mail Account", "Mail Group", "Mailing List"}
MANAGER_ROLES = ("System Manager", "Suite Cloud Manager")
RATE_LIMIT = 300  # requests per site per minute


class SiteAuthError(frappe.AuthenticationError):
    pass


class SiteSuspendedError(frappe.PermissionError):
    pass


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


def owned(doctype: str, name: str):
    """Loads one of the site's documents; anything else is a 404."""

    site = current_site()
    if doctype not in OWNED_DOCTYPES:
        raise ValueError(doctype)

    name = (name or "").strip().lower()
    doc = frappe.get_doc(doctype, name) if name and frappe.db.exists(doctype, name) else None
    if doc is None or doc.site != site.name:
        raise frappe.DoesNotExistError(_("{0} {1} not found.").format(_(doctype), name))
    return doc


def owned_names(doctype: str, filters: dict | None = None, **kwargs) -> list[str]:
    filters = {"site": current_site().name, **(filters or {})}
    return frappe.get_all(doctype, filters=filters, pluck="name", order_by="name asc", **kwargs)


def as_list(value: Any) -> list[str]:
    """Accepts a JSON list, a comma/newline separated string or None."""

    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    return [str(v).strip() for v in value if str(v).strip()]


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def ping() -> dict:
    """Confirms the credentials and returns where the site's mail lives."""

    return current_site().to_api()
