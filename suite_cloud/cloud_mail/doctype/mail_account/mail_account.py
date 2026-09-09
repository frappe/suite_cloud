# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt
from frappe.utils.password import set_encrypted_password

from suite_cloud.cloud_mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.cloud_mail.stalwart import get_account_client
from suite_cloud.cloud_mail.stalwart.credentials import Credential
from suite_cloud.cloud_mail.stalwart.directory import GB, Account
from suite_cloud.cloud_mail.tenancy import sync
from suite_cloud.cloud_mail.tenancy.addresses import (
    assert_address_available,
    assert_domain_live,
    get_site_domain,
    validate_email_address,
)

CREDENTIAL_DESCRIPTION = "Suite Cloud"
# Stored credentials: (field, service attribute on the account client).
STORED_CREDENTIALS = {"app_password": "app_passwords", "api_key": "api_keys"}
MIN_PASSWORD_LENGTH = 8


class MailAccount(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.cloud_mail.doctype.mail_address_alias.mail_address_alias import MailAddressAlias
        from suite_cloud.cloud_mail.doctype.mail_group_member.mail_group_member import MailGroupMember

        aliases: DF.Table[MailAddressAlias]
        api_key: DF.Password | None
        app_password: DF.Password | None
        cluster: DF.Link | None
        description: DF.Data | None
        disk_quota_gb: DF.Float
        display_name: DF.Data | None
        domain: DF.Link | None
        email: DF.Data
        enabled: DF.Check
        groups: DF.TableMultiSelect[MailGroupMember]
        locale: DF.Data | None
        new_password: DF.Password | None
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
        if self.is_new():
            assert_domain_live(domain)
        # Stalwart wants BCP 47 tags; POSIX-style names are a common slip.
        self.locale = (self.locale or "en-US").replace("_", "-")

        site = frappe.get_cached_doc("Suite Site", self.site)
        if self.is_new():
            site.assert_can_add_account()
        site.validate_quota_of(self)
        assert_address_available(self.email, exclude=(self.doctype, self.name))
        sync.validate_aliases(self)
        self.validate_groups()
        self.take_password()

    def take_password(self) -> None:
        """A password typed into the form is pushed to the cluster and never stored here."""

        if self.new_password and not self.is_dummy_password(self.new_password):
            validate_password(self.new_password)
            self.flags.password = self.new_password
        self.new_password = None

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
        try:
            self.mint_credential("app_password")
            if not self.enabled:
                self.push_enabled()
        except Exception:
            sync.push_destroy(self, "accounts")  # the insert rolls back; the account must not survive
            raise

    def on_update(self) -> None:
        if self.is_new() or not self.stalwart_id or self.flags.skip_push:
            return
        before = self.get_doc_before_save()
        if not before:
            return

        patch = {}
        if before.display_name != self.display_name:
            patch["description"] = self.display_name or None
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
        if self.flags.password:
            self.set_password(self.flags.password)

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
            description=self.display_name or None,
            locale=self.locale or "en-US",
            time_zone=self.time_zone or None,
            disk_quota_bytes=self.disk_quota_bytes(),
        )

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
        validate_password(password)
        sync.client_for(self).accounts.set_password(self.stalwart_id, password)

    @frappe.whitelist()
    def reset_password(self, password: str | None = None) -> str:
        """Sets a new password on the cluster and returns it; a blank one is generated."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        password = password or generate_password()
        self.set_password(password)
        return password

    def create_app_password(self, description: str) -> str:
        """Returns the generated secret; it is never stored on this side."""

        _, secret = self.account_client().app_passwords.create_secret(
            Credential(description=description or "Suite")
        )
        return secret

    # --- stored credentials ------------------------------------------------------------------------

    @frappe.whitelist()
    def rotate_app_password(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return self.mint_credential("app_password")

    @frappe.whitelist()
    def show_app_password(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return self.get_password("app_password")

    @frappe.whitelist()
    def rotate_api_key(self) -> str:
        """The API key exists only on demand; the first rotation creates it."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return self.mint_credential("api_key")

    @frappe.whitelist()
    def show_api_key(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return self.get_password("api_key")

    def mint_credential(self, field: str) -> str:
        """Creates the account's Suite Cloud credential of this kind, stores it, revokes the old one."""

        service = getattr(self.account_client(), STORED_CREDENTIALS[field])
        old_ids = [c["id"] for c in service.get_all() if c.get("description") == CREDENTIAL_DESCRIPTION]
        _, secret = service.create_secret(Credential(description=CREDENTIAL_DESCRIPTION))
        self.store_secret(field, secret)
        if old_ids:
            service.delete(old_ids)
        return secret

    def store_secret(self, field: str, secret: str) -> None:
        # Mirrors what Document.save does for Password fields, without a full save.
        set_encrypted_password(self.doctype, self.name, secret, field)
        self.set(field, "*" * len(secret))
        self.db_set(field, self.get(field), update_modified=False)

    def account_client(self):
        """Acts as the account itself (master-user login): app passwords and API keys need that."""

        cluster = frappe.get_cached_doc("Stalwart Cluster", self.cluster)
        return get_account_client(cluster, self.email)

    def set_enabled(self, enabled: bool) -> None:
        if bool(self.enabled) == bool(enabled):
            return
        self.enabled = int(bool(enabled))
        self.save(ignore_permissions=True)

    # --- helpers -----------------------------------------------------------------------------

    @property
    def used_disk_bytes(self) -> int | None:
        """Read from the cluster on access, one call per account; None until the account exists there."""

        if not self.stalwart_id:
            return None
        account = sync.client_for(self).accounts.get(self.stalwart_id, properties=["usedDiskQuota"])
        return cint((account or {}).get("usedDiskQuota"))

    def to_api(self, with_usage: bool = False) -> dict:
        """``with_usage`` costs a cluster round trip, so lists leave it out and single reads include it."""

        return {
            "email": self.email,
            "domain": self.domain,
            "enabled": bool(self.enabled),
            "display_name": self.display_name,
            "description": self.description,
            "disk_quota_gb": flt(self.disk_quota_gb),
            "used_disk_bytes": self.used_disk_bytes if with_usage else None,
            "locale": self.locale,
            "time_zone": self.time_zone,
            "aliases": [
                {"email": a.alias_email, "enabled": bool(a.enabled), "description": a.description}
                for a in self.aliases
            ],
            "groups": [g.group for g in self.groups],
            "mailing_lists": self.mailing_list_names(),
            "created_at": self.creation,
        }

    def mailing_list_names(self) -> list[str]:
        """Lists that deliver to any of the account's addresses, primary or alias."""

        addresses = [self.email, *[a.alias_email for a in self.aliases]]
        return frappe.get_all(
            "Mailing List Recipient",
            {"site": self.site, "email": ["in", addresses]},
            pluck="mailing_list",
            distinct=True,
            order_by="mailing_list asc",
        )


def validate_password(password: str | None) -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        frappe.throw(_("Password must be at least {0} characters.").format(MIN_PASSWORD_LENGTH))


def generate_password() -> str:
    return secrets.token_urlsafe(18)
