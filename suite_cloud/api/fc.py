"""Frappe Cloud endpoints: provisioning a Suite Site when a Suite-enabled site is created.

Callers authenticate with a Frappe user carrying the "Frappe Cloud" role (standard API
key/secret). The secret returned by ``create_site`` and ``rotate_site_secret`` is shown once;
FC stores it in the site's configuration.
"""

import frappe
from frappe import _

from suite_cloud.utils import get_public_url

ROLE = "Frappe Cloud"


def require_frappe_cloud() -> None:
    frappe.only_for((ROLE, "System Manager"))


@frappe.whitelist(methods=["POST"])
def create_site(
    site: str,
    cluster: str | None = None,
    region: str | None = None,
    fc_reference: str | None = None,
    max_domains: int | None = None,
    max_accounts: int | None = None,
    default_disk_quota_gb: float | None = None,
) -> dict:
    require_frappe_cloud()
    site = (site or "").strip().lower()
    if frappe.db.exists("Suite Site", site):
        frappe.throw(_("Site {0} already exists.").format(site), frappe.DuplicateEntryError)

    doc = frappe.get_doc(
        {
            "doctype": "Suite Site",
            "site_name": site,
            "cluster": pick_cluster(cluster, region),
            "fc_reference": fc_reference,
        }
    )
    for field, value in {
        "max_domains": max_domains,
        "max_accounts": max_accounts,
        "default_disk_quota_gb": default_disk_quota_gb,
    }.items():
        if value is not None:
            doc.set(field, value)
    doc.insert(ignore_permissions=True)

    frappe.local.response["http_status_code"] = 201
    return {**credentials(doc, doc.new_secret), **doc.to_api(), "suite_cloud_url": get_public_url()}


@frappe.whitelist(methods=["GET", "POST"])
def get_site(site: str) -> dict:
    require_frappe_cloud()
    return {**load(site).to_api(), "suite_cloud_url": get_public_url()}


@frappe.whitelist(methods=["POST"])
def rotate_site_secret(site: str) -> dict:
    require_frappe_cloud()
    doc = load(site)
    return credentials(doc, doc.rotate_secret())


@frappe.whitelist(methods=["POST"])
def suspend_site(site: str) -> dict:
    require_frappe_cloud()
    doc = load(site)
    doc.suspend()
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
def resume_site(site: str) -> dict:
    require_frappe_cloud()
    doc = load(site)
    doc.resume()
    return doc.to_api()


@frappe.whitelist(methods=["POST"])
def archive_site(site: str, delete_data: bool = False) -> dict:
    require_frappe_cloud()
    doc = load(site)
    doc.archive(delete_data=bool(delete_data))
    return doc.to_api()


def load(site: str):
    site = (site or "").strip().lower()
    if not frappe.db.exists("Suite Site", site):
        frappe.throw(_("Site {0} not found.").format(site), frappe.DoesNotExistError)
    return frappe.get_doc("Suite Site", site)


def credentials(doc, secret: str) -> dict:
    return {"api_key": doc.api_key, "api_secret": secret, "authorization_source": "Suite Site"}


def pick_cluster(cluster: str | None, region: str | None) -> str:
    """Explicit cluster, else the region's default (or only) cluster, else the global default."""

    candidates = frappe.get_all(
        "Stalwart Cluster",
        filters={"enabled": 1, "status": "Active"},
        fields=["name", "region", "is_default"],
        order_by="is_default desc, creation asc",
    )
    if cluster:
        if not any(c.name == cluster for c in candidates):
            frappe.throw(_("Cluster {0} is not active.").format(cluster))
        return cluster

    if region:
        regional = [c for c in candidates if (c.region or "").lower() == region.strip().lower()]
        if regional:
            return regional[0].name

    default = next((c for c in candidates if c.is_default), None) or (candidates[0] if candidates else None)
    if not default:
        frappe.throw(_("No active cluster is available."))
    return default.name
