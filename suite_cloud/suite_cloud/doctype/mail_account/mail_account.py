# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from suite_cloud.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.stalwart import get_account_client
from suite_cloud.stalwart.credentials import Credential
from suite_cloud.stalwart.directory import GB, Account
from suite_cloud.tenancy import sync
from suite_cloud.tenancy.addresses import (
    assert_address_available,
    get_site_domain,
    validate_email_address,
)


class MailAccount(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.suite_cloud.doctype.mail_address_alias.mail_address_alias import MailAddressAlias
        from suite_cloud.suite_cloud.doctype.mail_group_member.mail_group_member import MailGroupMember

        aliases: DF.Table[MailAddressAlias]
        cluster: DF.Link | None
        description: DF.Data | None
        disk_quota_gb: DF.Float
        display_name: DF.Data | None
        domain: DF.Link | None
        email: DF.Data
        enabled: DF.Check
        groups: DF.TableMultiSelect[MailGroupMember]
        locale: DF.Data | None
        site: DF.Link | None
        stalwart_id: DF.Data | None
        time_zone: DF.Data | None
        used_disk_bytes: DF.Int
    # end: auto-generated types

    # --- lifecycle --------------------------------------------------------------

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
        self.locale = self.locale or "en_US"

        site = frappe.get_cached_doc("Suite Site", self.site)
        if self.is_new():
            site.assert_can_add_account()
            if self.disk_quota_gb is None:
                self.disk_quota_gb = site.default_disk_quota_gb
        assert_address_available(self.email, exclude=(self.doctype, self.name))
        sync.validate_aliases(self)
        self.validate_groups()

    def validate_groups(self) -> None:
        seen = set()
        for row in self.groups:
            if row.group in seen:
                frappe.throw(_("Group {0} is listed twice.").format(row.group))
            seen.add(row.group)
            if frappe.db.get_value("Mail Group", row.group, "site") != self.site:
                frappe.throw(_("Group {0} belongs to another site.").format(row.group))

    def after_insert(self) -> None:
        sync.push_create(self, "accounts", self.stalwart_payload(self.flags.password))
        if not self.enabled:
            self.push_enabled()

    def on_update(self) -> None:
        if self.is_new() or not self.stalwart_id or self.flags.skip_push:
            return
        before = self.get_doc_before_save()
        if not before:
            return

        patch = {}
        if before.description != self.description or before.display_name != self.display_name:
            patch["description"] = self.stalwart_description()
        if before.locale != self.locale:
            patch["locale"] = self.locale
        if before.time_zone != self.time_zone:
            patch["timeZone"] = self.time_zone
        if flt(before.disk_quota_gb) != flt(self.disk_quota_gb):
            patch["quotas"] = {"maxDiskQuota": self.disk_quota_bytes()} if self.disk_quota_bytes() else {}
        if sync.aliases_changed(before, self):
            patch["aliases"] = sync.aliases_payload(self)
        if sorted(r.group for r in before.groups) != sorted(r.group for r in self.groups):
            patch["memberGroupIds"] = sync.group_ids_payload(self)
        if patch:
            sync.push_update(self, "accounts", patch)
        if bool(before.enabled) != bool(self.enabled):
            self.push_enabled()

    def on_trash(self) -> None:
        sync.push_destroy(self, "accounts")

    # --- Stalwart ------------------------------------------------------------------

    def stalwart_payload(self, password: str | None) -> Account:
        return Account(
            name=self.email.split("@", 1)[0],
            domain_id=sync.domain_stalwart_id(self.domain),
            password=password or frappe.generate_hash(length=24),
            member_group_ids=sync.group_ids(self),
            aliases=sync.aliases(self),
            description=self.stalwart_description(),
            locale=self.locale or "en_US",
            time_zone=self.time_zone or None,
            disk_quota_bytes=self.disk_quota_bytes(),
        )

    def stalwart_description(self) -> str | None:
        return self.display_name or self.description or None

    def disk_quota_bytes(self) -> int | None:
        return int(flt(self.disk_quota_gb) * GB) if flt(self.disk_quota_gb) > 0 else None

    def push_enabled(self) -> None:
        """Disabled accounts keep receiving mail but lose every other permission via a cluster role."""

        client = sync.client_for(self)
        if self.enabled:
            client.accounts.set_roles(self.stalwart_id, [])
            return

        role = client.roles.find_by_description(DISABLED_ROLE_DESCRIPTION)
        if not role:
            frappe.throw(
                _("The cluster is missing the {0} role; sync its configuration.").format(
                    DISABLED_ROLE_DESCRIPTION
                )
            )
        client.accounts.set_roles(self.stalwart_id, [role["id"]])

    # --- actions -------------------------------------------------------------------------

    def set_password(self, password: str) -> None:
        if not password or len(password) < 8:
            frappe.throw(_("Password must be at least 8 characters."))
        sync.client_for(self).accounts.set_password(self.stalwart_id, password)

    def create_app_password(self, description: str) -> str:
        """Returns the generated secret; it is never stored on this side."""

        cluster = frappe.get_cached_doc("Stalwart Cluster", self.cluster)
        client = get_account_client(cluster, self.email)
        _, secret = client.app_passwords.create_secret(Credential(description=description or "Suite"))
        return secret

    def set_enabled(self, enabled: bool) -> None:
        if bool(self.enabled) == bool(enabled):
            return
        self.enabled = int(bool(enabled))
        self.save(ignore_permissions=True)

    # --- helpers -----------------------------------------------------------------------------

    def to_api(self) -> dict:
        return {
            "email": self.email,
            "domain": self.domain,
            "enabled": bool(self.enabled),
            "display_name": self.display_name,
            "description": self.description,
            "disk_quota_gb": flt(self.disk_quota_gb),
            "used_disk_bytes": self.used_disk_bytes,
            "locale": self.locale,
            "time_zone": self.time_zone,
            "aliases": [
                {"email": a.alias_email, "enabled": bool(a.enabled), "description": a.description}
                for a in self.aliases
            ],
            "groups": [g.group for g in self.groups],
            "created_at": self.creation,
        }
