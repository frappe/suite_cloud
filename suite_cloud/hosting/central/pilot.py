"""Pilot, the bench agent on each Central server, reached with a bench token from Central."""

from urllib.parse import parse_qs, quote, urlparse

import frappe
import requests
from frappe import _

from suite_cloud.hosting import central
from suite_cloud.hosting.central import CentralError

TIMEOUT = 35
TOKEN_TTL = 50 * 60  # Central's bench tokens live 60 minutes
TERMINAL_TASK_STATUSES = ("success", "failed", "killed")


class PilotError(Exception):
    def __init__(self, message: str, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class PilotNotReady(PilotError):
    """Pilot cannot answer yet (booting, token not accepted, failing); try again later."""


class PilotConflict(PilotError):
    """A 409: ``code`` says which, e.g. ``task_conflict`` or ``site_name_conflict``."""


class PilotClient:
    def __init__(self, vm) -> None:
        self.vm = vm
        self.url = (vm.gateway_url or "").rstrip("/")
        self.session = requests.Session()

    # --- host -------------------------------------------------------------------

    def wildcard_domains(self) -> list[str]:
        return self.request("GET", "/api/v1/sites/wildcard-domains")["domains"]

    def settings(self) -> dict:
        return self.request("GET", "/api/v1/settings")

    def set_letsencrypt_email(self, email: str) -> None:
        """The account email Pilot requests certificates with. A Pilot before v0.0.74-pre-alpha answers
        but keeps none: read the settings again to know."""

        self.request("PATCH", "/api/v1/settings", json={"letsencrypt": {"email": email}})

    def list_sites(self) -> list[str]:
        return [site["name"] for site in self.request("GET", "/api/v1/sites")]

    def list_apps(self) -> list[dict]:
        return self.request("GET", "/api/v1/apps")

    def install_app(self, repo: str, branch: str) -> dict:
        return self.request("POST", "/api/v1/apps", json={"repo": repo, "branch": branch})

    # --- sites ------------------------------------------------------------------

    def create_site(self, name: str, apps: list[str], idempotency_key: str) -> dict:
        return self.request(
            "POST",
            "/api/v1/sites",
            json={"name": name, "apps": apps},
            headers={"Idempotency-Key": idempotency_key},
        )

    def drop_site(self, name: str) -> dict:
        return self.request(
            "DELETE", f"/api/v1/sites/{quote(name)}", headers={"Idempotency-Key": f"drop:{name}"}
        )

    def site_login_url(self, site: str) -> str:
        """A one-off Administrator session on the site: ``https://<site>/...?sid=<sid>``."""

        return self.request("POST", f"/api/v1/sites/{quote(site)}/login")["url"]

    # --- tasks ------------------------------------------------------------------

    def get_task(self, task_id: str) -> dict:
        return self.request("GET", f"/api/v1/tasks/{quote(task_id)}")

    def task_output(self, task_id: str) -> str:
        return self.request("GET", f"/api/v1/tasks/{quote(task_id)}/output/content")

    # --- domains ----------------------------------------------------------------

    def dns_records(self, site: str, domain: str) -> dict:
        return self.request("GET", f"/api/v1/sites/{quote(site)}/domains/{quote(domain)}/dns-records")

    def add_domain(self, site: str, domain: str) -> dict:
        return self.request("POST", f"/api/v1/sites/{quote(site)}/domains", json={"domain": domain})

    def set_primary(self, site: str, domain: str) -> dict:
        return self.request(
            "PATCH", f"/api/v1/sites/{quote(site)}/domains/{quote(domain)}", json={"primary": True}
        )

    def remove_domain(self, site: str, domain: str) -> dict:
        return self.request("DELETE", f"/api/v1/sites/{quote(site)}/domains/{quote(domain)}")

    # --- transport ----------------------------------------------------------------

    def request(self, verb: str, path: str, headers: dict | None = None, **kwargs):
        if not self.url:
            raise PilotNotReady(_("Server {0} has no gateway yet.").format(self.vm.name))
        headers = {"Authorization": f"Bearer {self.token()}", **(headers or {})}
        try:
            response = self.session.request(
                verb, f"{self.url}{path}", headers=headers, timeout=TIMEOUT, allow_redirects=False, **kwargs
            )
        except requests.RequestException as e:
            raise PilotNotReady(_("Pilot is not reachable: {0}").format(e))
        if response.status_code == 401:
            frappe.cache.delete_value(token_key(self.vm))
        if response.status_code >= 400:
            raise pilot_error(response)
        if response.status_code == 204:
            return None
        if "json" in response.headers.get("Content-Type", ""):
            return response.json()
        return response.text

    def token(self) -> str:
        """The bench token, for API use only: it is never handed to a browser."""

        key = token_key(self.vm)
        if token := frappe.cache.get_value(key):
            return token
        try:
            link = central.get_central_client().get_bench_link(self.vm.central_resource_id)
        except CentralError as e:
            raise PilotNotReady(_("Central gave no bench link yet: {0}").format(e))
        token = parse_qs(urlparse(link).query)["sid"][0]
        frappe.cache.set_value(key, token, expires_in_sec=TOKEN_TTL)
        return token


def pilot_error(response: requests.Response) -> PilotError:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    code, message = error.get("code"), error.get("message") or response.text[:500]
    if response.status_code == 409:
        return PilotConflict(message, code, 409)
    if response.status_code == 401 or response.status_code >= 500:
        return PilotNotReady(message, code, response.status_code)
    return PilotError(message, code, response.status_code)


def token_key(vm) -> str:
    return f"suite_cloud:pilot-token:{vm.name}"


def task_id(task: dict) -> str:
    return task["task_id"]


def is_finished(task: dict) -> bool:
    return task.get("status") in TERMINAL_TASK_STATUSES


def get_pilot_client(vm) -> PilotClient:
    """The seam tests replace with a fake."""

    return PilotClient(vm)
