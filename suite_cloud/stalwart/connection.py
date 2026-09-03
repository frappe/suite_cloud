import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from suite_cloud.stalwart.errors import StalwartError, StalwartUnauthorizedError, StalwartUnavailableError

# Gateway statuses a load balancer returns when the JMAP server behind it is down or overloaded.
UNAVAILABLE_STATUS_CODES = (502, 503, 504)


@dataclass
class ConnectionInfo:
    """Where to reach a Stalwart server and how to authenticate.

    Either ``token`` (an API key secret, sent as a Bearer token) or ``username``/``password``
    (HTTP Basic; also used for master-user logins of the form ``account%admin``).
    """

    url: str
    username: str | None = None
    password: str | None = None
    token: str | None = None
    timeout: tuple[float, float] = (15.0, 60.0)
    verify_ssl: bool = True


class SessionStore:
    """Persists a discovered JMAP session document (in Redis, for instance)."""

    def __init__(
        self, get: Callable[[], dict | None], set: Callable[[dict], None], clear: Callable[[], None]
    ) -> None:
        self.get = get
        self.set = set
        self.clear = clear


def http_session_factory() -> requests.Session:
    """Builds the HTTP session; tests swap this to mount a fake server."""

    return requests.Session()


class JMAPConnection:
    """An authenticated connection to one Stalwart server plus its discovered JMAP session."""

    def __init__(self, info: ConnectionInfo, session_store: SessionStore | None = None) -> None:
        self.info = info
        self._store = session_store
        self._http = http_session_factory()
        self._http.verify = info.verify_ssl
        if info.token:
            self._http.headers["Authorization"] = f"Bearer {info.token}"
        elif info.username is not None:
            self._http.auth = (info.username, info.password or "")

        self.session: dict = {}
        self._load_session()

    # --- session ----------------------------------------------------------

    def _load_session(self) -> None:
        if self._store and (session := self._store.get()):
            self.session = session
            return

        self.discover()

    def discover(self) -> None:
        """Fetches the JMAP session document from ``/.well-known/jmap``."""

        self.session = self.request("GET", urljoin(self.info.url, "/.well-known/jmap"))
        self.session["timestamp"] = time.time()
        if self._store:
            self._store.set(self.session)

    def forget_session(self) -> None:
        if self._store:
            self._store.clear()

    @property
    def capabilities(self) -> dict:
        return self.session.get("capabilities") or {}

    @property
    def api_url(self) -> str:
        return self.session["apiUrl"]

    @property
    def primary_accounts(self) -> dict:
        return self.session.get("primaryAccounts") or {}

    @property
    def accounts(self) -> dict:
        return self.session.get("accounts") or {}

    @property
    def state(self) -> str | None:
        return self.session.get("state")

    # --- transport --------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: Any = None,
        params: dict | None = None,
        timeout: tuple[float, float] | None = None,
        return_json: bool = True,
    ) -> Any:
        """Sends one HTTP request, translating failures into StalwartError subclasses."""

        try:
            response = self._http.request(
                method,
                url,
                headers=headers,
                json=json,
                params=params,
                timeout=timeout or self.info.timeout,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            raise StalwartUnavailableError() from e

        raise_for_status(response)
        return response.json() if return_json else response.content


def raise_for_status(response: requests.Response) -> None:
    if response.ok:
        return

    detail = f"HTTP {response.status_code} from {response.url}: {response.text[:500]}"
    if response.status_code in UNAVAILABLE_STATUS_CODES:
        raise StalwartUnavailableError(detail)
    if response.status_code in (401, 403):
        raise StalwartUnauthorizedError(detail)
    raise StalwartError(detail)
