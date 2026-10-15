# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from suite_cloud.mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.mail.stalwart import get_account_client
from suite_cloud.mail.stalwart.directory import DISK_QUOTA, GB, Account
from suite_cloud.mail.tenancy import account_credentials, quotas, sync
from suite_cloud.mail.tenancy.account_credentials import API_KEYS, APP_PASSWORDS
from suite_cloud.mail.tenancy.addresses import (
    assert_address_available,
    assert_domain_live,
    get_site_domain,
    validate_email_address,
)
from suite_cloud.mail.tenancy.quotas import QuotaHolder
from suite_cloud.mail.tenancy.usage import used_disk_by_name
from suite_cloud.utils import alias_payloads, child_rows, utc_iso

MIN_PASSWORD_LENGTH = 8


class MailAccount(QuotaHolder, Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.mail.doctype.mail_address_alias.mail_address_alias import MailAddressAlias
        from suite_cloud.mail.doctype.mail_credential.mail_credential import MailCredential
        from suite_cloud.mail.doctype.mail_group_member.mail_group_member import MailGroupMember
        from suite_cloud.mail.doctype.mail_quota.mail_quota import MailQuota

        aliases: DF.Table[MailAddressAlias]
        api_keys: DF.Table[MailCredential]
        app_passwords: DF.Table[MailCredential]
        cluster: DF.Link | None
        description: DF.Data | None
        disable_receiving: DF.Check
        display_name: DF.Data | None
        domain: DF.Link | None
        email: DF.Data
        enabled: DF.Check
        groups: DF.TableMultiSelect[MailGroupMember]
        locale: DF.Data | None
        new_password: DF.Password | None
        quotas: DF.Table[MailQuota]
        site: DF.Link | None
        stalwart_id: DF.Data | None
        time_zone: DF.Data | None
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
        if self.is_new() and not self.flags.adopting:
            assert_domain_live(domain)
        # Stalwart wants BCP 47 tags; POSIX-style names are a common slip.
        self.locale = (self.locale or "en-US").replace("_", "-")

        site = frappe.get_cached_doc("Suite Site", self.site)
        if self.is_new() and not self.flags.adopting:
            site.assert_can_add_account()
            account_credentials.apply_default_limits(self)
        quotas.validate(self)
        site.validate_quota_of(self)
        assert_address_available(self.email, exclude=None if self.is_new() else (self.doctype, self.name))
        sync.validate_aliases(self)
        self.validate_groups()
        if not self.flags.adopting:
            account_credentials.validate(self)
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
        if self.flags.skip_push:  # adopted: the cluster has it, credentials and all
            return
        sync.push_create(self, "accounts", self.stalwart_payload(self.flags.password))
        try:
            account_credentials.push(self, before=None)
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
        if quotas.changed(before, self):
            patch["quotas"] = self.quota_map()
        if sync.aliases_changed(before, self):
            patch["aliases"] = sync.aliases_payload(self)
        if sorted(r.group for r in before.groups) != sorted(r.group for r in self.groups):
            patch["memberGroupIds"] = sync.group_ids_payload(self)
        if bool(before.disable_receiving) != bool(self.disable_receiving):
            # Read before anything is sent and sent with the rest: the save reaches the cluster as
            # one update, so a refusal cannot leave half of it behind.
            patch["permissions"] = sync.receiving_permissions(self, "accounts")
        if patch:
            sync.push_update(self, "accounts", patch)
        account_credentials.push(self, before)
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
            disabled_permissions=sync.disabled_permissions(self),
            aliases=sync.aliases(self),
            description=self.display_name or None,
            locale=self.locale or "en-US",
            time_zone=self.time_zone or None,
            quotas=self.quota_map(),
        )

    def push_enabled(self) -> None:
        """Disabled accounts keep receiving mail but lose every other permission via a cluster role."""

        client = sync.client_for(self)
        if self.enabled:
            client.accounts.set_roles(self.stalwart_id, [])
            return

        role = client.roles.find_by_description(DISABLED_ROLE_DESCRIPTION)
        if not role:
            frappe.throw(
                _("The mail server is missing the {0} role. Contact the Suite Cloud operators.").format(
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

    # --- Suite Cloud credentials ---------------------------------------------------------------

    def mint_app_password(self) -> str:
        """Replaces the Suite site's login; the previous one is revoked once the save commits."""

        return account_credentials.rotate_site_login(self, APP_PASSWORDS)

    @frappe.whitelist()
    def rotate_app_password(self) -> str:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return self.mint_app_password()

    @frappe.whitelist()
    def rotate_api_key(self) -> str:
        """Mints the account's Suite Cloud API key, revokes the previous one, and returns it once."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        return account_credentials.rotate_site_login(self, API_KEYS)

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

    def onload(self) -> None:
        self.flags.with_usage = True  # the form shows the live figure
        account_credentials.refresh(self)

    @property
    def used_disk_bytes(self) -> int | None:
        """The virtual field: the cluster is asked only when a caller opted in.

        Every serialisation reads virtual fields, including the Deleted Document snapshot a delete
        takes and any as_dict(), so an unconditional lookup would cost a cluster call each time.
        """

        if not self.flags.with_usage:
            return None
        return self.fetch_used_disk_bytes()

    def fetch_used_disk_bytes(self) -> int | None:
        """One cluster call; None until the account exists there."""

        return used_disk_by_name([self]).get(self.name)

    def to_api(
        self,
        with_usage: bool = False,
        mailing_lists: list[str] | None = None,
        used_disk_bytes: int | None = None,
    ) -> dict:
        """``with_usage`` costs a cluster round trip, so single reads ask for it while a list page
        passes ``used_disk_bytes`` and ``mailing_lists`` looked up for the whole page at once."""

        return account_payload(
            self,
            aliases=alias_payloads(self.aliases),
            groups=[g.group for g in self.groups],
            quotas=self.quota_map(),
            mailing_lists=self.mailing_list_names() if mailing_lists is None else mailing_lists,
            used_disk_bytes=self.fetch_used_disk_bytes() if with_usage else used_disk_bytes,
        )

    def addresses(self) -> list[str]:
        return [self.email, *[a.alias_email for a in self.aliases]]

    def mailing_list_names(self) -> list[str]:
        """Lists that deliver to any of the account's addresses, primary or alias."""

        return frappe.get_all(
            "Mailing List Recipient",
            {"site": self.site, "email": ["in", self.addresses()]},
            pluck="mailing_list",
            distinct=True,
            order_by="mailing_list asc",
        )


def validate_password(password: str | None) -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        frappe.throw(_("Password must be at least {0} characters.").format(MIN_PASSWORD_LENGTH))


def generate_password() -> str:
    return secrets.token_urlsafe(18)


def account_payload(
    row,
    aliases: list[dict],
    groups: list[str],
    quotas: dict[str, int],
    mailing_lists: list[str],
    used_disk_bytes: int | None,
) -> dict:
    """The API shape of an account, from a document or a query row plus its related rows."""

    return {
        "email": row.email,
        "domain": row.domain,
        "enabled": bool(row.enabled),
        "disable_receiving": bool(row.disable_receiving),
        "display_name": row.display_name,
        "description": row.description,
        "disk_quota_gb": round(cint(quotas.get(DISK_QUOTA)) / GB, 6),
        "quotas": quotas,
        "used_disk_bytes": used_disk_bytes,
        "locale": row.locale,
        "time_zone": row.time_zone,
        "aliases": aliases,
        "groups": groups,
        "mailing_lists": mailing_lists,
        "created_at": utc_iso(row.creation),
    }


ACCOUNT_FIELDS = [
    "name",
    "email",
    "domain",
    "site",
    "cluster",
    "stalwart_id",
    "enabled",
    "disable_receiving",
    "display_name",
    "description",
    "locale",
    "time_zone",
    "creation",
]


def account_payloads(names: list[str], with_usage: bool = True) -> list[dict]:
    """A page of accounts in a handful of queries, whatever the page size.

    One query each for the accounts, their aliases, groups and quotas, one for the mailing lists
    that deliver to any of their addresses, and one cluster call for usage.
    """

    if not names:
        return []
    rows = frappe.get_all("Mail Account", filters={"name": ["in", names]}, fields=ACCOUNT_FIELDS)
    by_name = {row.name: row for row in rows}
    rows = [by_name[n] for n in names if n in by_name]
    aliases = child_rows(
        "Mail Address Alias", "Mail Account", names, ["alias_email", "enabled", "description"]
    )
    groups = child_rows("Mail Group Member", "Mail Account", names, ["group"])
    quotas = child_rows("Mail Quota", "Mail Account", names, ["quota", "value"])
    addresses = {row.name: [row.email, *[a.alias_email for a in aliases[row.name]]] for row in rows}
    lists = mailing_lists_by_address(rows[0].site, addresses) if rows else {}
    usage = used_disk_by_name(rows) if with_usage else {}
    return [
        account_payload(
            row,
            aliases=alias_payloads(aliases[row.name]),
            groups=[g.group for g in groups[row.name]],
            quotas={q.quota: cint(q.value) for q in quotas[row.name]},
            mailing_lists=lists.get(row.name, []),
            used_disk_bytes=usage.get(row.name),
        )
        for row in rows
    ]


def mailing_lists_by_address(site: str, addresses: dict[str, list[str]]) -> dict[str, list[str]]:
    """``{account name: [list addresses]}`` given each account's addresses, in one query."""

    by_address = {address: name for name, found in addresses.items() for address in found}
    if not by_address:
        return {}
    rows = frappe.get_all(
        "Mailing List Recipient",
        {"site": site, "email": ["in", list(by_address)]},
        ["email", "mailing_list"],
    )
    lists: dict[str, set[str]] = {name: set() for name in addresses}
    for row in rows:
        lists[by_address[row.email]].add(row.mailing_list)
    return {name: sorted(found) for name, found in lists.items()}
