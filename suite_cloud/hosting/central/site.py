"""A hosted site's own REST API, as its Administrator.

The session comes from a Pilot login link and is used for API calls only. It never reaches a
browser, and since it never renders a page it carries no CSRF token, so it may POST.
"""

from collections.abc import Callable
from urllib.parse import parse_qs, quote, urlparse

import requests
from frappe import _

TIMEOUT = 60


class SiteError(Exception):
    def __init__(self, message: str, error_type: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status = status


class SiteUnavailableError(SiteError):
    """The site could not be reached or failed; the same call can be made again."""


class SiteClient:
    def __init__(self, hostname: str, login: Callable[[], str]) -> None:
        self.url = f"https://{hostname}"
        self.hostname = hostname
        self.login = login  # returns a fresh login link
        self.session = requests.Session()
        self.sign_in()

    def sign_in(self) -> None:
        sid = parse_qs(urlparse(self.login()).query)["sid"][0]
        self.session.cookies.set("sid", sid, domain=self.hostname)

    # --- documents --------------------------------------------------------------

    def get_doc(self, doctype: str, name: str) -> dict | None:
        try:
            return self.request("GET", f"/api/v2/document/{quote(doctype)}/{quote(name)}")
        except SiteError as e:
            if e.status == 404:
                return None
            raise

    def create_doc(self, doctype: str, values: dict) -> dict:
        return self.request("POST", f"/api/v2/document/{quote(doctype)}", json=values)

    def update_doc(self, doctype: str, name: str, values: dict) -> dict:
        return self.request("PATCH", f"/api/v2/document/{quote(doctype)}/{quote(name)}", json=values)

    def set_single(self, doctype: str, values: dict) -> dict:
        return self.update_doc(doctype, doctype, values)

    def call(self, method: str, **kwargs):
        return self.request("POST", f"/api/v2/method/{method}", json=kwargs)

    def run_doc_method(self, doctype: str, name: str, method: str, **kwargs):
        return self.request(
            "POST", f"/api/v2/document/{quote(doctype)}/{quote(name)}/method/{method}", json=kwargs
        )

    def ensure_user(self, values: dict) -> None:
        if not self.get_doc("User", values["email"]):
            self.create_doc("User", values)

    # --- transport ----------------------------------------------------------------

    def request(self, verb: str, path: str, **kwargs):
        response = self.send(verb, path, **kwargs)
        if response.is_redirect:
            # Followed, the session cookie would stay behind and a write could turn into a read.
            raise SiteUnavailableError(
                _("Site {0} redirected to {1}.").format(self.hostname, response.headers.get("Location"))
            )
        if response.status_code == 400 and error_type(response) == "CSRFTokenError":
            self.sign_in()  # a page rendered with the session: a fresh one carries no token
            response = self.send(verb, path, **kwargs)
        if response.status_code >= 400:
            raise site_error(response)
        return response.json().get("data") if response.content else None

    def send(self, verb: str, path: str, **kwargs) -> requests.Response:
        try:
            return self.session.request(
                verb, f"{self.url}{path}", timeout=TIMEOUT, allow_redirects=False, **kwargs
            )
        except requests.RequestException as e:
            raise SiteUnavailableError(_("Site {0} is not reachable: {1}").format(self.hostname, e))


def errors(response: requests.Response) -> list[dict]:
    try:
        return response.json().get("errors") or []
    except ValueError:
        return []


def error_type(response: requests.Response) -> str | None:
    found = errors(response)
    return found[0].get("type") if found else None


def site_error(response: requests.Response) -> SiteError:
    found = errors(response)
    message = "; ".join(e.get("message") or "" for e in found) or response.text[:500]
    cls = SiteUnavailableError if response.status_code >= 500 else SiteError
    return cls(message, error_type(response), response.status_code)


def get_site_client(site) -> SiteClient:
    """The seam tests replace with a fake."""

    pilot = site.get_virtual_machine().get_pilot_client()
    return SiteClient(get_api_host(site), lambda: pilot.site_login_url(site.hostname))


def get_api_host(site) -> str:
    """Its other host names redirect to the primary domain, so calls go there, but only to one whose
    records Suite Cloud publishes: a tenant can point its own domain at any server, which would get
    the Administrator session."""

    if not site.primary_domain:
        return site.hostname
    row = site.get_domain(site.primary_domain)
    if not (row and row.managed):
        raise SiteError(
            _(
                "{0} is the primary domain of Suite Site {1}, and its tenant controls its DNS, so "
                "Suite Cloud does not send it the site's Administrator session. Make the site domain "
                "primary to manage the site."
            ).format(site.primary_domain, site.name)
        )
    return site.primary_domain
