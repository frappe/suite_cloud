"""Directory objects: domains, accounts (users and groups), mailing lists, roles, DKIM keys."""

from dataclasses import dataclass, field
from typing import ClassVar

from suite_cloud.stalwart.service import ManagementService, id_set, indexed

DEFAULT_LOCALE = "en_US"
DKIM_ALGORITHMS = ("Dkim1Ed25519Sha256", "Dkim1RsaSha256")
DKIM_SELECTOR_TEMPLATE = "v{version}-{algorithm}-{date-%Y%m%d}"
DAY_MS = 24 * 60 * 60 * 1000
GB = 1024**3


@dataclass
class EmailAlias:
    name: str
    domain_id: str
    enabled: bool = True
    description: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "domainId": self.domain_id,
            "enabled": self.enabled,
            "description": self.description,
        }


def roles_payload(role_ids: list[str] | None) -> dict:
    """Custom roles replace the built-in User role, so an empty list means the default."""

    if role_ids:
        return {"@type": "Custom", "roleIds": id_set(role_ids)}
    return {"@type": "User"}


def quotas_payload(disk_quota_bytes: int | None) -> dict:
    return {"maxDiskQuota": int(disk_quota_bytes)} if disk_quota_bytes else {}


@dataclass
class Account:
    name: str
    domain_id: str
    password: str | None = None
    member_group_ids: list[str] | None = None
    role_ids: list[str] | None = None
    aliases: list[EmailAlias] | None = None
    description: str | None = None
    locale: str = DEFAULT_LOCALE
    time_zone: str | None = None
    disk_quota_bytes: int | None = None

    def to_dict(self) -> dict:
        credentials = {"0": {"@type": "Password", "secret": self.password}} if self.password else {}
        return {
            "@type": "User",
            "name": self.name,
            "domainId": self.domain_id,
            "credentials": credentials,
            "memberGroupIds": id_set(self.member_group_ids),
            "roles": roles_payload(self.role_ids),
            "permissions": {"@type": "Inherit"},
            "quotas": quotas_payload(self.disk_quota_bytes),
            "aliases": indexed(self.aliases),
            "description": self.description,
            "locale": self.locale or DEFAULT_LOCALE,
            "timeZone": self.time_zone,
            "encryptionAtRest": {"@type": "Disabled"},
        }


@dataclass
class Group:
    """Groups share ``x:Account`` with users; membership lives on each member account."""

    name: str
    domain_id: str
    description: str | None = None
    aliases: list[EmailAlias] | None = None

    def to_dict(self) -> dict:
        return {
            "@type": "Group",
            "name": self.name,
            "domainId": self.domain_id,
            "permissions": {"@type": "Inherit"},
            "quotas": {},
            "aliases": indexed(self.aliases),
            "description": self.description,
        }


@dataclass
class Domain:
    name: str
    description: str | None = None
    is_enabled: bool = True
    aliases: list[str] | None = None
    dkim_algorithms: tuple[str, ...] = DKIM_ALGORITHMS
    certificate_management: dict | None = None
    dns_management: dict | None = None
    catch_all_address: str | None = None
    sub_addressing: bool = True
    allow_relaying: bool = False
    report_address_uri: str | None = "mailto:postmaster"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "aliases": id_set(self.aliases),
            "isEnabled": self.is_enabled,
            "description": self.description,
            "certificateManagement": self.certificate_management or {"@type": "Manual"},
            "dkimManagement": dkim_management_payload(self.dkim_algorithms),
            "dnsManagement": self.dns_management or {"@type": "Manual"},
            "catchAllAddress": self.catch_all_address,
            "subAddressing": {"@type": "Enabled" if self.sub_addressing else "Disabled"},
            "allowRelaying": self.allow_relaying,
            "reportAddressUri": self.report_address_uri,
        }


def dkim_management_payload(algorithms: tuple[str, ...] | None) -> dict:
    """Automatic DKIM: Stalwart generates and rotates keys; Manual when no algorithm is wanted."""

    if not algorithms:
        return {"@type": "Manual"}

    return {
        "@type": "Automatic",
        "algorithms": id_set(algorithms),
        "selectorTemplate": DKIM_SELECTOR_TEMPLATE,
        "rotateAfter": 90 * DAY_MS,
        "retireAfter": 7 * DAY_MS,
        "deleteAfter": 30 * DAY_MS,
    }


@dataclass
class MailingList:
    name: str
    domain_id: str
    description: str | None = None
    aliases: list[EmailAlias] | None = None
    recipients: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "domainId": self.domain_id,
            "description": self.description,
            "aliases": indexed(self.aliases),
            "recipients": id_set(self.recipients),
        }


@dataclass
class Role:
    description: str
    role_ids: list[str] = field(default_factory=list)
    enabled_permissions: list[str] = field(default_factory=list)
    disabled_permissions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "roleIds": id_set(self.role_ids),
            "enabledPermissions": id_set(self.enabled_permissions),
            "disabledPermissions": id_set(self.disabled_permissions),
        }


# --- services ------------------------------------------------------------------


class AccountService(ManagementService):
    type = "Account"
    default_properties: ClassVar[list[str]] = [
        "@type",
        "id",
        "name",
        "description",
        "emailAddress",
        "aliases",
        "domainId",
        "locale",
        "memberGroupIds",
        "quotas",
        "roles",
        "timeZone",
        "usedDiskQuota",
    ]

    def find_by_name(self, name: str, domain_id: str, properties: list[str] | None = None) -> dict | None:
        return self.find({"name": name, "domainId": domain_id}, properties=properties or ["id"])

    def set_password(self, account_id: str, new_password: str) -> None:
        """Replaces the primary password, leaving app passwords and API keys intact."""

        credentials = (self.get(account_id, properties=["credentials"]) or {}).get("credentials") or {}
        row = next((idx for idx, c in credentials.items() if c.get("@type") == "Password"), None)
        if row is None:
            row = str(len(credentials))
            self.update(account_id, {f"credentials/{row}": {"@type": "Password", "secret": new_password}})
        else:
            self.update(account_id, {f"credentials/{row}/secret": new_password})

    def set_roles(self, account_id: str, role_ids: list[str]) -> None:
        # roles is a tagged union; its @type cannot be patched by sub-path, so the whole field goes.
        self.update(account_id, {"roles": roles_payload(role_ids)})

    def set_member_group_ids(self, account_id: str, group_ids: list[str]) -> None:
        self.update(account_id, {"memberGroupIds": id_set(group_ids)})

    def set_aliases(self, account_id: str, aliases: list[EmailAlias]) -> None:
        self.update(account_id, {"aliases": indexed(aliases)})

    def set_disk_quota(self, account_id: str, disk_quota_bytes: int | None) -> None:
        self.update(account_id, {"quotas": quotas_payload(disk_quota_bytes)})


class GroupService(AccountService):
    def get_all_groups(self, properties: list[str] | None = None) -> list[dict]:
        return self.get_all(filter={"@type": "Group"}, properties=properties)

    def get_member_ids(self, group_id: str) -> list[str]:
        return [m["id"] for m in self.get_all(filter={"memberGroupIds": group_id}, properties=["id"])]

    def delete(self, ids: str | list[str]) -> None:
        """Clears membership first: Stalwart keeps dangling group ids on members otherwise."""

        ids = [ids] if isinstance(ids, str) else list(ids)
        for group_id in ids:
            for member_id in self.get_member_ids(group_id):
                self.update(member_id, {f"memberGroupIds/{group_id}": None})

        super().delete(ids)


class DomainService(ManagementService):
    type = "Domain"
    default_properties: ClassVar[list[str]] = ["id", "name", "description", "isEnabled", "createdAt"]

    def find_by_name(self, name: str, properties: list[str] | None = None) -> dict | None:
        return self.find({"name": name}, properties=properties or ["id", "name"])

    def get_zone_file(self, domain_id: str) -> str:
        return (self.get(domain_id, properties=["dnsZoneFile"]) or {}).get("dnsZoneFile") or ""

    def delete(self, ids: str | list[str]) -> None:
        """Deletes domains, first removing the DKIM signatures that would block the delete."""

        ids = [ids] if isinstance(ids, str) else list(ids)
        dkim = DkimSignatureService(self.connection)
        for domain_id in ids:
            if signature_ids := [s["id"] for s in dkim.get_all_by_domain(domain_id)]:
                dkim.delete(signature_ids)

        super().delete(ids)


class DkimSignatureService(ManagementService):
    type = "DkimSignature"
    default_properties: ClassVar[list[str]] = ["id", "selector", "domainId", "stage", "nextTransitionAt"]

    def get_all_by_domain(self, domain_id: str, properties: list[str] | None = None) -> list[dict]:
        return self.get_all(filter={"domainId": domain_id}, properties=properties)


class MailingListService(ManagementService):
    type = "MailingList"
    default_properties: ClassVar[list[str]] = [
        "id",
        "name",
        "emailAddress",
        "domainId",
        "recipients",
        "description",
        "aliases",
    ]

    def find_by_name(self, name: str, domain_id: str, properties: list[str] | None = None) -> dict | None:
        return self.find({"name": name, "domainId": domain_id}, properties=properties or ["id"])

    def set_recipients(self, list_id: str, recipients: list[str]) -> None:
        self.update(list_id, {"recipients": id_set(recipients)})

    def set_aliases(self, list_id: str, aliases: list[EmailAlias]) -> None:
        self.update(list_id, {"aliases": indexed(aliases)})


class RoleService(ManagementService):
    type = "Role"
    default_properties: ClassVar[list[str]] = [
        "id",
        "description",
        "roleIds",
        "enabledPermissions",
        "disabledPermissions",
    ]

    def find_by_description(self, description: str) -> dict | None:
        return self.find({"description": description}, properties=["id", "description"])
