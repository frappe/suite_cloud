"""Stalwart 0.16 management client (JMAP dialect ``urn:stalwart:jmap``).

``get_client`` speaks to a cluster or egress gateway with its stored API key; the admin and
account clients exist only for the two things a Bearer token cannot do: minting API keys and
creating app passwords inside a member's account (master-user login ``account%admin``).
"""

import hashlib
from typing import TYPE_CHECKING

import frappe

from suite_cloud.stalwart.client import StalwartClient
from suite_cloud.stalwart.connection import ConnectionInfo, JMAPConnection, SessionStore

if TYPE_CHECKING:
    from frappe.model.document import Document

SESSION_CACHE_KEY = "suite_cloud:stalwart:sessions"
DEFAULT_TIMEOUT = (15.0, 60.0)


def get_client(target: Document, timeout: tuple[float, float] = DEFAULT_TIMEOUT) -> StalwartClient:
    """Returns the management client for a Stalwart Cluster or Egress Gateway document."""

    token = target.get_password("api_key", raise_exception=False)
    if not token:
        frappe.throw(
            frappe._("{0} {1} has no Stalwart API key yet; it is minted when bootstrap finishes.").format(
                target.doctype, target.name
            )
        )

    info = ConnectionInfo(target.base_url, token=token, timeout=timeout, verify_ssl=verify_tls())
    store = session_store(f"{target.doctype}:{target.name}:{hashlib.sha1(token.encode()).hexdigest()}")
    return StalwartClient(JMAPConnection(info, session_store=store))


def get_admin_client(target: Document, timeout: tuple[float, float] = DEFAULT_TIMEOUT) -> StalwartClient:
    """Authenticates with the admin account's password; sessions are never cached."""

    info = ConnectionInfo(
        target.base_url,
        username=target.admin_username,
        password=target.get_password("admin_password"),
        timeout=timeout,
        verify_ssl=verify_tls(),
    )
    return StalwartClient(JMAPConnection(info))


def get_account_client(
    target: Document, email: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT
) -> StalwartClient:
    """Acts inside ``email``'s account via master-user login.

    Not cached on purpose: the session carries the account id, and Stalwart reuses ids when an
    address is deleted and recreated, so a stale session would scope calls to a gone account.
    """

    info = ConnectionInfo(
        target.base_url,
        username=f"{email}%{target.admin_username}",
        password=target.get_password("admin_password"),
        timeout=timeout,
        verify_ssl=verify_tls(),
    )
    return StalwartClient(JMAPConnection(info))


def forget_sessions(target: Document) -> None:
    """Drops cached sessions for a target, e.g. after its API key was rotated."""

    prefix = f"{target.doctype}:{target.name}:"
    for key in frappe.cache.hkeys(SESSION_CACHE_KEY) or []:
        key = key.decode() if isinstance(key, bytes) else key
        if key.startswith(prefix):
            frappe.cache.hdel(SESSION_CACHE_KEY, key)


def session_store(key: str) -> SessionStore:
    return SessionStore(
        get=lambda: frappe.cache.hget(SESSION_CACHE_KEY, key),
        set=lambda session: frappe.cache.hset(SESSION_CACHE_KEY, key, session),
        clear=lambda: frappe.cache.hdel(SESSION_CACHE_KEY, key),
    )


def verify_tls() -> bool:
    """TLS verification stays on unless a dev site opts out in site_config."""

    return bool((frappe.conf.suite_cloud or {}).get("verify_stalwart_tls", True))
