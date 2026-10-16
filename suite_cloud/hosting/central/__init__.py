"""Frappe Cloud's control plane (Central): the servers Suite Cloud owns in its team."""

import json

import frappe
import requests
from frappe import _

from suite_cloud.utils import get_config

TIMEOUT = 30
SERVERS = "central.api.servers"
IMAGES = "central.api.images"


class CentralError(Exception):
    pass


class CentralUnavailableError(CentralError):
    """Central could not be reached or failed; the same call can be made again."""


class CentralRejectedError(CentralError):
    """Central refused the call; ``code`` and ``remediation`` come from its action error."""

    def __init__(self, message: str, code: str | None = None, remediation: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation


class CentralClient:
    def __init__(self, url: str, api_key: str, api_secret: str, team: str) -> None:
        self.url = url.rstrip("/")
        self.team = team
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"token {api_key}:{api_secret}"

    # --- methods ----------------------------------------------------------------

    def my_capabilities(self) -> list[str]:
        return self.get("central.api.identity.my_capabilities", team=self.team)

    def my_teams(self) -> list[dict]:
        """The active teams of the key's user: ``name`` is the id the other calls take, ``label``
        the team name (else the owner's email, else the id)."""

        return self.get("central.api.identity.my_teams")

    def list_instances(self) -> list[dict]:
        """The regions the team may create servers in."""

        return self.get(f"{SERVERS}.list_instances", team=self.team)

    def list_images(self, region: str, offering: str) -> list[dict]:
        """Every page of the offering's images in the region."""

        images, offset = [], 0
        while offset is not None:
            page = self.get(
                f"{IMAGES}.list_images", team=self.team, region=region, offering=offering, offset=offset
            )
            images += page.get("items") or []
            offset = page.get("next_offset")
        return images

    def eligible_plans(self, region: str, offering: str, image_id: str) -> dict:
        return self.get(
            f"{IMAGES}.eligible_plans", team=self.team, region=region, offering=offering, image_id=image_id
        )

    def create_server(self, **arguments) -> dict:
        return self.post(f"{SERVERS}.create_server", team=self.team, **arguments)

    def action_status(self, action: str) -> dict:
        return self.get(f"{SERVERS}.action_status", name=action)

    def retry_action(self, action: str) -> dict:
        return self.post(f"{SERVERS}.retry_action", name=action)

    def registry(self) -> dict:
        """``{servers, sites, creations}`` of the team, in one call."""

        return self.get(f"{SERVERS}.registry", team=self.team)

    def server_overview(self, resource_id: str) -> dict:
        return self.get(f"{SERVERS}.server_overview", team=self.team, resource_id=resource_id)["server"]

    def terminate_server(self, resource_id: str) -> dict:
        return self.post(f"{SERVERS}.terminate_server", team=self.team, resource_id=resource_id)

    def get_bench_link(self, resource_id: str) -> str:
        """``<gateway>/?sid=<bench token>``, valid for an hour."""

        return self.get("central.api.sso.get_bench_link", server=resource_id, team=self.team)["url"]

    # --- transport ----------------------------------------------------------------

    def get(self, method: str, **params):
        return self.request("GET", method, params=params)

    def post(self, method: str, **body):
        return self.request("POST", method, json=body)

    def request(self, verb: str, method: str, **kwargs):
        try:
            response = self.session.request(
                verb, f"{self.url}/api/method/{method}", timeout=TIMEOUT, **kwargs
            )
        except requests.RequestException as e:
            raise CentralUnavailableError(_("Central is not reachable: {0}").format(e))
        if response.status_code >= 500:
            raise CentralUnavailableError(_("Central failed with HTTP {0}.").format(response.status_code))
        body = parse_json(response)
        if response.status_code >= 400:
            raise rejection(body, response.status_code)
        return body.get("message")


def parse_json(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def rejection(body: dict, status: int) -> CentralRejectedError:
    """Central's action error travels in ``_server_messages``; other refusals carry only a message."""

    for message in server_messages(body):
        if error := message.get("action_error"):
            return CentralRejectedError(
                error.get("message") or "", error.get("code"), error.get("remediation")
            )
    messages = [m.get("message") for m in server_messages(body) if m.get("message")]
    text = "; ".join(messages) or body.get("exception") or _("HTTP {0}").format(status)
    return CentralRejectedError(text, body.get("exc_type"))


def server_messages(body: dict) -> list[dict]:
    try:
        return [json.loads(entry) for entry in json.loads(body.get("_server_messages") or "[]")]
    except (TypeError, ValueError):
        return []


def get_central_client(credentials: dict | None = None) -> CentralClient:
    """The seam tests replace with a fake. ``credentials`` (url, api_key, api_secret) are tried
    before they are saved, to list the teams the key can pick from."""

    if credentials:
        return CentralClient(credentials["url"], credentials["api_key"], credentials["api_secret"], team="")
    settings = frappe.get_cached_doc("Suite Cloud Settings")
    if not settings.is_central_configured:
        frappe.throw(_("Set the Central URL, team, API key and secret in Suite Cloud Settings."))
    return CentralClient(
        get_config("central_url"),
        get_config("central_api_key"),
        settings.get_password("central_api_secret"),
        get_config("central_team"),
    )
