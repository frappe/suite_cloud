# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt

from suite_cloud.cloud_mail.stalwart.directory import DISK_QUOTA, GB, Group
from suite_cloud.cloud_mail.tenancy import quotas, sync
from suite_cloud.cloud_mail.tenancy.addresses import (
    assert_address_available,
    assert_domain_live,
    get_site_domain,
    validate_email_address,
)
from suite_cloud.cloud_mail.tenancy.quotas import QuotaHolder
from suite_cloud.cloud_mail.tenancy.usage import used_disk_by_name
from suite_cloud.utils import alias_payloads, child_rows, utc_iso


class MailGroup(QuotaHolder, Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.cloud_mail.doctype.mail_address_alias.mail_address_alias import MailAddressAlias

        aliases: DF.Table[MailAddressAlias]
        cluster: DF.Link | None
        description: DF.Data | None
        domain: DF.Link | None
        email: DF.Data
        site: DF.Link | None
        stalwart_id: DF.Data | None
    # end: auto-generated types

    def autoname(self) -> None:
        # Naming runs before validate, so the address is normalised here too.
        self.email = validate_email_address(self.email)
        self.name = self.email

    def validate(self) -> None:
        self.email = validate_email_address(self.email)
        domain = get_site_domain(self.site, self.email.split("@", 1)[1]) if self.site else None
        if domain is None:
            domain = frappe.get_cached_doc("Mail Domain", self.email.split("@", 1)[1])
        self.domain = domain.name
        self.site = domain.site
        self.cluster = domain.cluster
        if self.is_new():
            assert_domain_live(domain)
        site = frappe.get_cached_doc("Suite Site", self.site)
        if self.is_new():
            site.assert_can_add_group()
        quotas.validate(self)
        site.validate_quota_of(self)
        assert_address_available(self.email, exclude=(self.doctype, self.name))
        sync.validate_aliases(self)

    def after_insert(self) -> None:
        sync.push_create(self, "groups", self.stalwart_payload())

    def on_update(self) -> None:
        if self.is_new() or not self.stalwart_id or self.flags.skip_push:
            return
        before = self.get_doc_before_save()
        if not before:
            return
        patch = {}
        if before.description != self.description:
            patch["description"] = self.description
        if quotas.changed(before, self):
            patch["quotas"] = self.quota_map()
        if sync.aliases_changed(before, self):
            patch["aliases"] = sync.aliases_payload(self)
        if patch:
            sync.push_update(self, "groups", patch)

    def on_trash(self) -> None:
        # Members reference the group by name; the Stalwart side clears memberGroupIds itself.
        frappe.db.delete("Mail Group Member", {"group": self.name})
        sync.push_destroy(self, "groups")

    def stalwart_payload(self) -> Group:
        return Group(
            name=self.email.split("@", 1)[0],
            domain_id=sync.domain_stalwart_id(self.domain),
            description=self.description or None,
            aliases=sync.aliases(self),
            quotas=self.quota_map(),
        )

    def member_emails(self) -> list[str]:
        return frappe.get_all(
            "Mail Group Member", {"group": self.name, "parenttype": "Mail Account"}, pluck="parent"
        )

    def to_api(self, with_usage: bool = False, used_disk_bytes: int | None = None) -> dict:
        """``with_usage`` asks the cluster; a list page passes usage fetched for the whole page."""

        if with_usage:
            used_disk_bytes = used_disk_by_name([self]).get(self.name)
        return group_payload(
            self,
            aliases=alias_payloads(self.aliases),
            quotas=self.quota_map(),
            members=self.member_emails(),
            used_disk_bytes=used_disk_bytes,
        )


def group_payload(
    row, aliases: list[dict], quotas: dict[str, int], members: list[str], used_disk_bytes
) -> dict:
    return {
        "email": row.email,
        "domain": row.domain,
        "description": row.description,
        "disk_quota_gb": round(cint(quotas.get(DISK_QUOTA)) / GB, 6),
        "quotas": quotas,
        "used_disk_bytes": used_disk_bytes,
        "aliases": aliases,
        "members": sorted(members),
        "created_at": utc_iso(row.creation),
    }


GROUP_FIELDS = ["name", "email", "domain", "site", "cluster", "stalwart_id", "description", "creation"]


def group_payloads(names: list[str], with_usage: bool = True) -> list[dict]:
    """A page of groups in a handful of queries: groups, aliases, quotas, members, one usage call."""

    if not names:
        return []
    rows = frappe.get_all("Mail Group", filters={"name": ["in", names]}, fields=GROUP_FIELDS)
    by_name = {row.name: row for row in rows}
    rows = [by_name[n] for n in names if n in by_name]
    aliases = child_rows("Mail Address Alias", "Mail Group", names, ["alias_email", "enabled", "description"])
    quotas = child_rows("Mail Quota", "Mail Group", names, ["quota", "value"])
    members: dict[str, list[str]] = {name: [] for name in names}
    for member in frappe.get_all(
        "Mail Group Member", {"group": ["in", names], "parenttype": "Mail Account"}, ["group", "parent"]
    ):
        members[member.group].append(member.parent)
    usage = used_disk_by_name(rows) if with_usage else {}
    return [
        group_payload(
            row,
            aliases=alias_payloads(aliases[row.name]),
            quotas={q.quota: cint(q.value) for q in quotas[row.name]},
            members=members[row.name],
            used_disk_bytes=usage.get(row.name),
        )
        for row in rows
    ]
