# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from suite_cloud.cloud_mail.cluster import dns as cluster_dns
from suite_cloud.cloud_mail.cluster import egress
from suite_cloud.cloud_mail.cluster.zone import GROUPS, build_domain_records, group_summaries
from suite_cloud.cloud_mail.stalwart.directory import Domain
from suite_cloud.cloud_mail.tenancy import ownership, sync
from suite_cloud.cloud_mail.tenancy.addresses import assert_domain_available, validate_domain_name
from suite_cloud.dns.resolver import verify_dns_record
from suite_cloud.utils import dkim_algorithms, get_config, utc_iso

# A change to any of these reaches the cluster; is_verified is set by hand only by managers.
PUSHED_FIELDS = ("description", "catch_all_address", "sub_addressing", "enabled", "is_verified")


class MailDomain(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.cloud_mail.doctype.mail_domain_dns_record.mail_domain_dns_record import (
            MailDomainDNSRecord,
        )

        authentication_records: DF.Table[MailDomainDNSRecord]
        autoconfig_records: DF.Table[MailDomainDNSRecord]
        catch_all_address: DF.Data | None
        cluster: DF.Link | None
        description: DF.Data | None
        discovery_records: DF.Table[MailDomainDNSRecord]
        dns_zone_file: DF.Code | None
        domain_name: DF.Data
        egress_pool: DF.Link | None
        enabled: DF.Check
        is_verified: DF.Check
        last_refreshed_at: DF.Datetime | None
        last_verified_at: DF.Datetime | None
        publish_client_discovery_records: DF.Check
        routing_records: DF.Table[MailDomainDNSRecord]
        site: DF.Link
        stalwart_id: DF.Data | None
        sub_addressing: DF.Check
        transport_security_records: DF.Table[MailDomainDNSRecord]
    # end: auto-generated types

    # --- lifecycle --------------------------------------------------------------

    def autoname(self) -> None:
        # Naming runs before validate, so the address is normalised here too.
        self.domain_name = validate_domain_name(self.domain_name)
        self.name = self.domain_name

    def validate(self) -> None:
        self.domain_name = validate_domain_name(self.domain_name)
        site = self.get_site()
        self.cluster = site.cluster
        if self.is_new():
            assert_domain_available(self.domain_name, self.site)
            site.assert_can_add_domain()
            if ownership.required():
                ownership.assert_ownership(site, self.domain_name)
        if self.catch_all_address:
            self.catch_all_address = self.catch_all_address.strip().lower()
        if (
            self.egress_pool
            and frappe.db.get_value("Egress IP Pool", self.egress_pool, "cluster") != self.cluster
        ):
            frappe.throw(_("Egress pool {0} belongs to another cluster.").format(self.egress_pool))
        if not self.is_new() and self.has_value_changed("enabled") and not self.enabled:
            # Proof of control lapses with the domain: enabling it again needs a fresh verification.
            self.is_verified = 0
        if not self.is_new() and self.has_value_changed("publish_client_discovery_records"):
            # The zone already holds the certificate-bound records; only which tables list them changes.
            self.rebuild_dns_records(self.dns_zone_file or "")

    def after_insert(self) -> None:
        payload = self.stalwart_payload()
        sync.push_create(self, "domains", payload)
        try:
            self.refresh_dns_records(expected_dkim_keys=len(payload.dkim_algorithms))
        except Exception:
            sync.push_destroy(self, "domains")  # the insert rolls back; the domain must not survive
            raise

    def on_update(self) -> None:
        if self.is_new() or not self.stalwart_id or self.flags.skip_push:
            return
        before = self.get_doc_before_save()
        if before and any(before.get(f) != self.get(f) for f in PUSHED_FIELDS):
            sync.push_update(self, "domains", self.stalwart_patch())
        if before and (before.egress_pool != self.egress_pool or before.enabled != self.enabled):
            egress.resync_cluster_after_commit(self.cluster)

    def on_trash(self) -> None:
        for doctype in ("Mail Account", "Mail Group", "Mailing List"):
            if frappe.db.exists(doctype, {"domain": self.name}):
                frappe.throw(_("Delete every {0} of {1} first.").format(_(doctype), self.domain_name))
        alias_filters = {"alias_email": ["like", f"%@{self.domain_name}"]}
        if alias := frappe.db.get_value("Mail Address Alias", alias_filters, "parent"):
            frappe.throw(_("Remove the aliases on {0} first (e.g. on {1}).").format(self.domain_name, alias))
        sync.push_destroy(self, "domains")

    def after_delete(self) -> None:
        if self.egress_pool or frappe.db.get_value("Suite Site", self.site, "egress_pool"):
            egress.resync_cluster_after_commit(self.cluster)

    # --- Stalwart -----------------------------------------------------------------

    def is_live(self) -> bool:
        """Mail flows only for verified domains: proof of control comes from the published records."""

        return bool(self.enabled and self.is_verified)

    def stalwart_payload(self) -> Domain:
        return Domain(
            name=self.domain_name,
            description=self.description or f"Suite site {self.site}",
            is_enabled=self.is_live(),
            dkim_algorithms=dkim_algorithms(),
            catch_all_address=self.catch_all_address or None,
            sub_addressing=bool(self.sub_addressing),
            report_address_uri=f"mailto:postmaster@{self.domain_name}",
        )

    def stalwart_patch(self) -> dict:
        return {
            "description": self.description or f"Suite site {self.site}",
            "isEnabled": self.is_live(),
            "catchAllAddress": self.catch_all_address or None,
            "subAddressing": {"@type": "Enabled" if self.sub_addressing else "Disabled"},
        }

    # --- DNS ------------------------------------------------------------------------

    @frappe.whitelist()
    def refresh_dns_records(self, expected_dkim_keys: int | None = None) -> None:
        self.assert_saved()
        """Re-reads the zone Stalwart expects and rebuilds the record rows (verification kept).

        Right after creation the read waits for the DKIM keys Stalwart is still generating.
        """

        zone_file = sync.client_for(self).domains.get_zone_file(
            self.stalwart_id, expected_dkim_keys=cint(expected_dkim_keys)
        )
        self.rebuild_dns_records(zone_file)
        self.last_refreshed_at = now()
        # Liveness is only ever changed by a verification run: a rotated DKIM selector must not
        # take a working domain offline before its owner had a chance to publish it.
        self.save_records()

    def rebuild_dns_records(self, zone_file: str) -> None:
        """Replaces the group tables with the rows parsed from ``zone_file`` (verification kept)."""

        cluster = self.get_cluster()
        rows = build_domain_records(
            self.domain_name,
            zone_file,
            spf_include=cluster_dns.spf_include(cluster),
            include_client_discovery=bool(self.publish_client_discovery_records),
            default_ttl=cint(get_config("default_dns_ttl")) or 300,
        )
        verified = {
            (r.record_type, r.host, (r.value or "").strip()): r for r in self.dns_rows() if r.is_verified
        }
        for group in GROUPS:
            self.set(group["key"], [])
        for row in rows:
            previous = verified.get((row["record_type"], row["host"], row["value"].strip()))
            if previous:
                row.update({"is_verified": 1, "last_checked_at": previous.last_checked_at})
            self.append(row.pop("group"), row)
        self.dns_zone_file = zone_file

    @frappe.whitelist()
    def verify_dns_records(self) -> dict:
        self.assert_saved()
        """Resolves every record on public resolvers; the domain is verified when all mandatory ones match."""

        checked_at = now()
        inconclusive = 0
        for row in self.dns_rows():
            verified = verify_dns_record(row.fqdn, row.record_type, self.expected_value(row))
            if verified is None:
                inconclusive += 1  # resolver trouble says nothing about the record: keep its state
                continue
            row.is_verified = cint(verified)
            row.last_checked_at = checked_at

        was_live = self.is_live()
        self.is_verified = int(self.compute_is_verified())
        self.last_verified_at = checked_at
        if self.stalwart_id and was_live != self.is_live():
            # Cluster first: a failed push must not leave the local flag ahead of Stalwart.
            sync.push_update(self, "domains", {"isEnabled": self.is_live()})
        self.save_records()
        return {"inconclusive": inconclusive, "is_verified": bool(self.is_verified), **self.records_payload()}

    def compute_is_verified(self) -> bool:
        """SPF and DMARC must verify, plus at least one DKIM selector.

        Stalwart rotates DKIM keys; the retiring selector keeps signatures valid while the owner
        publishes the new one, so a single verified selector is enough to stay live. MX is the
        owner's choice: a domain may send through the cluster and keep receiving elsewhere.
        """

        rows = self.authentication_records
        if not rows:
            return False
        by_category: dict[str, list] = {}
        for row in rows:
            by_category.setdefault(row.category, []).append(row)
        for category in ("SPF", "DMARC"):
            if not by_category.get(category) or not all(r.is_verified for r in by_category[category]):
                return False
        return any(r.is_verified for r in by_category.get("DKIM", []))

    def dns_rows(self) -> list[Document]:
        """Every record row across the group tables, in group order."""

        return [row for group in GROUPS for row in self.get(group["key"])]

    def records_payload(self) -> dict:
        return {"groups": group_summaries(), "records": [r.to_api() for r in self.dns_rows()]}

    @staticmethod
    def expected_value(row: Document) -> str:
        """What the resolver's answer must render as; SRV answers carry all four fields."""

        if row.record_type == "SRV":
            return f"{row.priority} {row.weight} {row.port} {row.value}"
        return row.value

    def save_records(self) -> None:
        self.flags.skip_push = True
        self.save(ignore_permissions=True)
        self.flags.skip_push = False

    def assert_saved(self) -> None:
        """Refuses to act on a form with unsaved edits to pushed fields.

        The desk sends the form as it is; saving it here with pushes skipped would commit those
        edits locally without the cluster ever hearing of them, and no later save would push them.
        """

        if self.is_new():
            return
        fields = [*PUSHED_FIELDS, "egress_pool", "publish_client_discovery_records"]
        stored = frappe.db.get_value(self.doctype, self.name, fields, as_dict=True) or {}
        for field in fields:
            if (stored.get(field) or "") != (self.get(field) or ""):
                frappe.throw(_("Save the domain before running this action."))

    # --- helpers -----------------------------------------------------------------------

    def get_site(self) -> Document:
        return frappe.get_cached_doc("Suite Site", self.site)

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster or self.get_site().cluster)

    def to_api(self, with_records: bool = True) -> dict:
        payload = {
            "domain": self.domain_name,
            "enabled": bool(self.enabled),
            "description": self.description,
            "catch_all_address": self.catch_all_address,
            "sub_addressing": bool(self.sub_addressing),
            "publish_client_discovery_records": bool(self.publish_client_discovery_records),
            "is_verified": bool(self.is_verified),
            "last_verified_at": utc_iso(self.last_verified_at),
            "created_at": utc_iso(self.creation),
        }
        if with_records:
            records = self.records_payload()
            payload["dns_record_groups"] = records["groups"]
            payload["dns_records"] = records["records"]
        return payload


def refresh_all_domains() -> None:
    """Daily: pick up rotated DKIM selectors and any zone changes."""

    for name in frappe.get_all("Mail Domain", {"stalwart_id": ["is", "set"]}, pluck="name"):
        _refresh(name)


def refresh_rotating_domains() -> None:
    """Hourly: domains whose keys are mid-rotation change selectors within days."""

    for name in frappe.get_all("Mail Domain", {"stalwart_id": ["is", "set"], "is_verified": 1}, pluck="name"):
        domain = frappe.get_doc("Mail Domain", name)
        try:
            signatures = sync.client_for(domain).dkim_signatures.get_all_by_domain(domain.stalwart_id)
        except Exception:
            continue
        if any(s.get("stage") in ("pending", "retiring") for s in signatures):
            _refresh(name)


def verify_unverified_domains() -> None:
    """Hourly: unverified domains, plus verified ones with a mandatory row still unverified (rotations)."""

    # Disabled domains stay unverified on purpose: verification resumes once they are enabled.
    names = set(
        frappe.get_all(
            "Mail Domain", {"is_verified": 0, "enabled": 1, "stalwart_id": ["is", "set"]}, pluck="name"
        )
    )
    names.update(
        frappe.get_all(
            "Mail Domain DNS Record",
            {"is_mandatory": 1, "is_verified": 0, "parenttype": "Mail Domain"},
            pluck="parent",
            distinct=True,
        )
    )
    for name in sorted(names):
        stalwart_id, enabled = frappe.db.get_value("Mail Domain", name, ["stalwart_id", "enabled"])
        if stalwart_id and enabled:
            _run_isolated(
                name, lambda: frappe.get_doc("Mail Domain", name).verify_dns_records(), "DNS verification"
            )


def _refresh(name: str) -> None:
    _run_isolated(name, lambda: frappe.get_doc("Mail Domain", name).refresh_dns_records(), "DNS refresh")


def _run_isolated(name: str, action, label: str) -> None:
    """One domain per transaction; a failure rolls its partial work back instead of committing it."""

    try:
        action()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title=f"[Suite Cloud] {label} failed for {name}")
        return
    if not frappe.in_test:
        frappe.db.commit()
