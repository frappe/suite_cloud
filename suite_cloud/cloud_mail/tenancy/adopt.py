"""Adopts what a cluster already holds into a site's directory.

For a site whose cluster came from the previous architecture (one Stalwart per site, administered
by the site itself), the cluster has domains, accounts, groups and lists that Suite Cloud never
created. Adoption records them as Mail Domain, Mail Account, Mail Group and Mailing List with the
ids the cluster already uses, and pushes nothing back: the cluster is the source here.

Rules: the cluster must serve exactly this one site, so nothing can belong to anyone else; an
object whose record already exists anywhere is skipped; every object is its own transaction, so
one that fails to import (an alias on a foreign domain, say) is reported and the rest go in.
"""

import frappe
from frappe import _
from frappe.utils import cint

from suite_cloud.cloud_mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.cloud_mail.cluster.reconcile import INFRA_ACCOUNTS

SAVEPOINT = "adopt_object"


def adopt_directory(site_name: str) -> dict:
    """Returns ``{"adopted": {doctype: [names]}, "skipped": {doctype: [[name, reason]]}}``."""

    site = frappe.get_doc("Suite Site", site_name)
    cluster = site.get_cluster()
    others = set(
        frappe.get_all("Suite Site", {"cluster": cluster.name, "name": ["!=", site.name]}, pluck="name")
    )
    for doctype in ("Mail Domain", "Mail Account", "Mail Group", "Mailing List"):
        # Rows left by a site that moved or was removed still say whose the cluster's objects are.
        others.update(
            frappe.get_all(
                doctype, {"cluster": cluster.name, "site": ["!=", site.name]}, pluck="site", distinct=True
            )
        )
    if others:
        frappe.throw(
            _("Cluster {0} also serves {1}; adoption needs a cluster with this one site.").format(
                cluster.name, ", ".join(sorted(others))
            )
        )

    client = cluster.get_client()
    report = Report()
    domain_names = _adopt_domains(site, cluster, client, report)
    principals = client.accounts.get_all()  # users and groups share one object type
    groups = [p for p in principals if p.get("@type") == "Group"]
    users = [p for p in principals if p.get("@type") == "User"]
    group_names = _adopt_groups(site, groups, domain_names, report)
    disabled_role = client.roles.find_by_description(DISABLED_ROLE_DESCRIPTION)
    _adopt_accounts(site, users, domain_names, group_names, disabled_role and disabled_role["id"], report)
    _adopt_mailing_lists(site, client, domain_names, report)
    return report.as_dict()


class Report:
    def __init__(self) -> None:
        self.adopted: dict[str, list[str]] = {}
        self.skipped: dict[str, list[list[str]]] = {}

    def adopt(self, doctype: str, name: str) -> None:
        self.adopted.setdefault(doctype, []).append(name)

    def skip(self, doctype: str, name: str, reason: str) -> None:
        self.skipped.setdefault(doctype, []).append([name, reason])

    def as_dict(self) -> dict:
        return {"adopted": self.adopted, "skipped": self.skipped}


def _adopt_domains(site, cluster, client, report: Report) -> dict[str, str]:
    """``{stalwart id: domain name}`` for every domain of the cluster, adopted or already known."""

    names = {}
    for live in client.domains.get_all(
        properties=[
            "id",
            "name",
            "description",
            "isEnabled",
            "catchAllAddress",
            "subAddressing",
            "allowRelaying",
        ]
    ):
        name = live["name"]
        names[live["id"]] = name
        if name == cluster.default_domain:
            continue
        if frappe.db.exists("Mail Domain", name):
            report.skip("Mail Domain", name, _("already exists"))
            continue
        enabled = bool(live.get("isEnabled"))
        _insert(
            report,
            "Mail Domain",
            name,
            {
                "doctype": "Mail Domain",
                "domain_name": name,
                "site": site.name,
                "stalwart_id": live["id"],
                "enabled": int(enabled),
                "is_verified": int(enabled),  # working on the cluster: the operator vouches for it
                "description": live.get("description"),
                "catch_all_address": live.get("catchAllAddress"),
                "sub_addressing": int((live.get("subAddressing") or {}).get("@type") != "Disabled"),
                "allow_relaying": int(bool(live.get("allowRelaying"))),
            },
        )
    return names


def _adopt_groups(site, groups: list[dict], domains: dict[str, str], report: Report) -> dict[str, str]:
    """``{stalwart id: group name}`` for every group of the cluster, adopted or already known."""

    names = {}
    for live in groups:
        email = _address(live, domains)
        if not email:
            continue
        names[live["id"]] = email
        if frappe.db.exists("Mail Group", email):
            report.skip("Mail Group", email, _("already exists"))
            continue
        _insert(
            report,
            "Mail Group",
            email,
            {
                "doctype": "Mail Group",
                "email": email,
                "site": site.name,
                "stalwart_id": live["id"],
                "description": live.get("description"),
                "aliases": _alias_rows(live, domains),
                "quotas": _quota_rows(live),
            },
        )
    return names


def _adopt_accounts(site, users: list[dict], domains, groups, disabled_role_id, report: Report) -> None:
    for live in users:
        if live.get("name") in INFRA_ACCOUNTS:
            continue
        email = _address(live, domains)
        if not email:
            continue
        if frappe.db.exists("Mail Account", email):
            report.skip("Mail Account", email, _("already exists"))
            continue
        roles = live.get("roles") or {}
        disabled = roles.get("@type") == "Custom" and disabled_role_id in (roles.get("roleIds") or {})
        _insert(
            report,
            "Mail Account",
            email,
            {
                "doctype": "Mail Account",
                "email": email,
                "site": site.name,
                "stalwart_id": live["id"],
                "enabled": int(not disabled),
                "display_name": live.get("description"),
                "locale": live.get("locale"),
                "time_zone": live.get("timeZone"),
                "aliases": _alias_rows(live, domains),
                "quotas": _quota_rows(live),
                "groups": [{"group": groups[g]} for g in (live.get("memberGroupIds") or {}) if g in groups],
            },
        )


def _adopt_mailing_lists(site, client, domains, report: Report) -> None:
    for live in client.mailing_lists.get_all():
        email = _address(live, domains)
        if not email:
            continue
        if frappe.db.exists("Mailing List", email):
            report.skip("Mailing List", email, _("already exists"))
            continue
        recipients = list(live.get("recipients") or {})
        _insert(
            report,
            "Mailing List",
            email,
            {
                "doctype": "Mailing List",
                "email": email,
                "site": site.name,
                "stalwart_id": live["id"],
                "description": live.get("description"),
                "aliases": _alias_rows(live, domains),
            },
            after=lambda doc: doc.add_recipients(recipients, push=False),
        )


def _insert(report: Report, doctype: str, name: str, data: dict, after=None):
    """One object per savepoint, ``after`` included: a refusal rolls back only that object."""

    frappe.db.savepoint(SAVEPOINT)
    try:
        doc = frappe.get_doc(data)
        doc.flags.adopting = True  # the cluster already holds it: no push, no limits
        doc.flags.skip_push = True
        doc.insert(ignore_permissions=True)
        if after is not None:
            after(doc)
    except Exception as e:
        frappe.db.rollback(save_point=SAVEPOINT)
        report.skip(doctype, name, str(e))
        return None
    report.adopt(doctype, name)
    return doc


def _address(live: dict, domains: dict[str, str]) -> str | None:
    if live.get("emailAddress"):
        return live["emailAddress"].lower()
    domain = domains.get(live.get("domainId"))
    return f"{live['name']}@{domain}".lower() if live.get("name") and domain else None


def _alias_rows(live: dict, domains: dict[str, str]) -> list[dict]:
    rows = []
    for alias in (live.get("aliases") or {}).values():
        domain = domains.get(alias.get("domainId"))
        if not domain or not alias.get("name"):
            continue
        rows.append(
            {
                "alias_email": f"{alias['name']}@{domain}".lower(),
                "enabled": int(alias.get("enabled", True)),
                "description": alias.get("description"),
            }
        )
    return rows


def _quota_rows(live: dict) -> list[dict]:
    return [
        {"quota": name, "value": cint(value)}
        for name, value in (live.get("quotas") or {}).items()
        if cint(value) > 0
    ]
