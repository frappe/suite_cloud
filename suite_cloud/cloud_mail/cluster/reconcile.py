"""Compares the directory Suite Cloud believes a cluster has with what Stalwart reports.

Read-only by design: the report says what is missing or orphaned and an operator decides.
"""

from typing import TYPE_CHECKING

import frappe

if TYPE_CHECKING:
    from frappe.model.document import Document

INFRA_ACCOUNTS = {"admin", "relay"}


def directory_report(cluster: Document) -> dict:
    client = cluster.get_client()
    report = {
        "checked_at": frappe.utils.now(),
        "missing_on_stalwart": [],
        "orphans_on_stalwart": [],
        "id_mismatch": [],
    }

    live_domains = {d["name"]: d["id"] for d in client.domains.get_all(properties=["id", "name"])}
    ours = frappe.get_all("Mail Domain", {"cluster": cluster.name}, ["domain_name", "stalwart_id"])
    _compare(
        report, "Domain", {d.domain_name: d.stalwart_id for d in ours}, live_domains, {cluster.default_domain}
    )

    live_accounts = {
        a.get("emailAddress"): a["id"]
        for a in client.accounts.get_all(properties=["id", "emailAddress", "name"])
        if a.get("emailAddress") and a.get("name") not in INFRA_ACCOUNTS
    }
    ours_accounts = {
        a.email: a.stalwart_id
        for a in frappe.get_all("Mail Account", {"cluster": cluster.name}, ["email", "stalwart_id"])
    }
    ours_accounts.update(
        {
            g.email: g.stalwart_id
            for g in frappe.get_all("Mail Group", {"cluster": cluster.name}, ["email", "stalwart_id"])
        }
    )
    _compare(report, "Account", ours_accounts, live_accounts, set())

    live_lists = {
        m.get("emailAddress"): m["id"]
        for m in client.mailing_lists.get_all(properties=["id", "emailAddress"])
        if m.get("emailAddress")
    }
    ours_lists = {
        m.email: m.stalwart_id
        for m in frappe.get_all("Mailing List", {"cluster": cluster.name}, ["email", "stalwart_id"])
    }
    _compare(report, "MailingList", ours_lists, live_lists, set())
    return report


def _compare(report: dict, object_type: str, ours: dict, live: dict, ignore: set) -> None:
    for name, stalwart_id in ours.items():
        if name not in live:
            report["missing_on_stalwart"].append({"object": object_type, "name": name})
        elif live[name] != stalwart_id:
            report["id_mismatch"].append(
                {"object": object_type, "name": name, "ours": stalwart_id, "live": live[name]}
            )
    for name in live:
        if name not in ours and name not in ignore:
            report["orphans_on_stalwart"].append({"object": object_type, "name": name})
