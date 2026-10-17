"""Custom domains on a hosted site, served by Frappe Cloud's proxy and certified by Pilot.

Pilot names the records a domain needs (a CNAME to the regional proxy and a TXT token that
Central caches for 24 hours) and attaches the domain once Central sees them. For the site's own
``<label>.<site zone>`` Suite Cloud publishes the records itself; for any other domain the tenant
publishes them.
"""

import json

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, get_datetime, now, now_datetime

from suite_cloud.dns.resolver import verify_dns_record
from suite_cloud.hosting import output_tail, wait_for_task
from suite_cloud.hosting.central.pilot import PilotConflict, is_finished, task_id
from suite_cloud.hosting.central.site import SiteUnavailableError
from suite_cloud.suite_cloud.doctype.dns_record.dns_record import (
    confirm_managed_records,
    reconcile_managed_records,
)
from suite_cloud.suite_cloud.doctype.dns_zone.dns_zone import relative_host
from suite_cloud.utils import get_config

RECORDS_MAX_AGE_HOURS = 23  # Central keeps a verification token 24 hours
PUBLISHED_TYPES = ("CNAME", "TXT")
HTTPS_CHECK_SECONDS = 15


class DomainNotReadyError(frappe.ValidationError):
    """Records are missing or changed; the message says what to publish."""


def ensure_site_domain(site) -> bool:
    """The site's own domain (see ``labels.site_domain``), published, attached and, unless the site
    already has one, primary; True once it is."""

    domain = site.site_domain
    row = site.get_domain(domain) or add_row(site, domain, managed=True)
    if row.status != "Active":
        fetch_records(site, row)
        publish_managed_records(site)
        if not confirm_managed_records("Suite Site", site.name):
            return False  # the next poll checks once the provider has them
        if not records_visible(row) or not attach(site, row):
            return False
    if not site.primary_domain:
        if problem := get_https_problem(domain):
            raise SiteUnavailableError(problem)  # the site keeps its own host name meanwhile
        make_primary(site, domain)
    return True


def add_domain(site, domain: str) -> dict:
    """A domain the tenant publishes itself; returns the records to publish."""

    if site.get_domain(domain):
        frappe.throw(_("{0} is already a domain of this site.").format(domain))
    row = add_row(site, domain, managed=False)
    fetch_records(site, row)
    return records_of(row)


def verify_domain(site, domain: str) -> bool:
    """Attaches the domain once its records resolve; a renewed token means publishing again."""

    row = get_row(site, domain)
    previous = records_of(row)
    if fetch_records(site, row) and records_of(row) != previous:
        frappe.throw(
            _("The verification token of {0} changed; publish the new records.").format(domain),
            DomainNotReadyError,
        )
    if not records_visible(row):
        frappe.throw(_("The DNS records of {0} do not resolve yet.").format(domain), DomainNotReadyError)
    return attach(site, row)


def make_primary(site, domain: str) -> None:
    """Every other host name of the site redirects to the primary one, so it must serve HTTPS."""

    row = get_row(site, domain)
    if row.status != "Active":
        frappe.throw(_("{0} is not attached yet.").format(domain))
    if problem := get_https_problem(domain):
        frappe.throw(problem, DomainNotReadyError)
    pilot = site.get_pilot_client()
    run_task(pilot, pilot.set_primary(site.hostname, domain))
    for other in site.domains:
        other.is_primary = int(other.domain == domain)
    site.primary_domain = domain
    site.save(ignore_permissions=True)


def get_https_problem(domain: str) -> str | None:
    """Why the domain does not answer over HTTPS with a valid certificate, as visitors reach it.
    Pilot reports a domain's certificate once it holds one, but the proxy in front of the server
    may not route TLS for the name yet."""

    try:
        requests.get(f"https://{domain}/api/method/ping", timeout=HTTPS_CHECK_SECONDS, allow_redirects=False)
    except requests.RequestException as e:
        return _("https://{0} does not answer over HTTPS yet: {1}").format(domain, e)
    return None


def remove_domain(site, domain: str) -> None:
    row = get_row(site, domain)
    if row.is_primary:
        frappe.throw(_("{0} is the primary domain; make another one primary first.").format(domain))
    if row.status == "Active":
        pilot = site.get_pilot_client()
        run_task(pilot, pilot.remove_domain(site.hostname, domain))
    site.remove(row)
    site.save(ignore_permissions=True)
    publish_managed_records(site)


# --- steps ----------------------------------------------------------------------


def add_row(site, domain: str, managed: bool):
    row = site.append(
        "domains", {"domain": domain.strip().lower(), "status": "Pending", "managed": int(managed)}
    )
    site.save(ignore_permissions=True)
    return site.get_domain(row.domain)


def fetch_records(site, row) -> bool:
    """Asks Pilot again when the stored records are missing or near their token's expiry."""

    fresh_after = add_to_date(now_datetime(), hours=-RECORDS_MAX_AGE_HOURS)
    if row.dns_records and row.records_fetched_at and get_datetime(row.records_fetched_at) > fresh_after:
        return False
    answer = site.get_pilot_client().dns_records(site.hostname, row.domain)
    row.dns_records = json.dumps(flatten_records(answer, row.domain))
    row.records_fetched_at = now()
    site.save(ignore_permissions=True)
    return True


def attach(site, row) -> bool:
    """Registers the domain with Pilot; True once it is attached."""

    pilot = site.get_pilot_client()
    if not row.task:
        try:
            task = pilot.add_domain(site.hostname, row.domain)
        except PilotConflict as e:
            if "already attached" in str(e).lower():
                return set_row(site, row, status="Active", failure_reason=None)
            set_row(site, row, failure_reason=str(e))  # Central does not see the records yet
            return False
        set_row(site, row, task=task_id(task))
    task = wait_for_task(pilot, row.task)
    if not is_finished(task):
        return False
    if task["status"] != "success":
        set_row(site, row, status="Failed", task=None, failure_reason=output_tail(pilot, row.task))
        return False
    return set_row(site, row, status="Active", task=None, failure_reason=None)


def publish_managed_records(site) -> None:
    """Every managed domain's records in the site zone, as DNS Records owned by the site."""

    zone = get_config("site_dns_zone")
    rows = []
    for row in site.domains:
        if row.managed and zone:
            rows += [
                {
                    "dns_zone": zone,
                    "host": relative_host(r["fqdn"], zone),
                    "type": r["type"],
                    "value": r["value"],
                    "category": "Other",
                }
                for r in records_of(row)
            ]
    reconcile_managed_records("Suite Site", site.name, rows)


def records_visible(row) -> bool:
    records = records_of(row)
    return bool(records) and all(verify_dns_record(r["fqdn"], r["type"], r["value"]) for r in records)


# --- helpers ------------------------------------------------------------------------


def flatten_records(answer: dict, domain: str) -> list[dict]:
    """Pilot groups its records (``cname``, ``txt``, ``a``); proxy routing needs the CNAME and TXT."""

    records = []
    for group in (answer or {}).values():
        for record in group if isinstance(group, list) else []:
            if record.get("type") in PUBLISHED_TYPES:
                records.append(
                    {
                        "type": record["type"],
                        "fqdn": absolute(record.get("host"), domain),
                        "value": record["value"].rstrip("."),
                    }
                )
    return records


def absolute(host: str | None, domain: str) -> str:
    host = (host or "@").rstrip(".").lower()
    if host == "@" or host == domain:
        return domain
    return host if host.endswith(f".{domain}") else f"{host}.{domain}"


def records_of(row) -> list[dict]:
    return json.loads(row.dns_records) if row.dns_records else []


def get_row(site, domain: str):
    if not (row := site.get_domain(domain)):
        frappe.throw(_("{0} is not a domain of this site.").format(domain), frappe.DoesNotExistError)
    return row


def set_row(site, row, **values) -> bool:
    row.update(values)
    site.save(ignore_permissions=True)
    return True


def run_task(pilot, task: dict) -> None:
    task = wait_for_task(pilot, task_id(task))
    if not is_finished(task):
        frappe.throw(_("Pilot is still working on it; try again in a few minutes."))
    if task["status"] != "success":
        frappe.throw(_("Pilot could not finish: {0}").format(output_tail(pilot, task_id(task))))
