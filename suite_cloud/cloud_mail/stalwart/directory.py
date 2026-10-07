"""Directory objects: domains, accounts (users and groups), mailing lists, roles, DKIM keys."""

import re
import time
from dataclasses import dataclass, field
from typing import ClassVar

from suite_cloud.cloud_mail.stalwart.errors import StalwartError, StalwartKeylessDomainError
from suite_cloud.cloud_mail.stalwart.service import ManagementService, id_set, indexed

# Stalwart locales are BCP 47 tags (en-US), not POSIX names (en_US).
DEFAULT_LOCALE = "en-US"
DKIM_ED25519 = "Dkim1Ed25519Sha256"
DKIM_RSA = "Dkim1RsaSha256"
# Stalwart's own default when a domain is created without naming its algorithms.
DKIM_ALGORITHMS = (DKIM_ED25519, DKIM_RSA)
# One fixed selector per key type: frappemail-rsa and frappemail-ed25519. A template is shared by
# every key of a domain, so the algorithm has to be part of it; nothing else varies.
DKIM_SELECTOR_TEMPLATE = "frappemail-{algorithm}"
DAY_MS = 24 * 60 * 60 * 1000
# Keys are never rotated: a rotation would ask every domain owner to publish a new selector.
# Stalwart only rotates domains under automatic DNS management anyway; this pins the rest.
DKIM_ROTATE_AFTER_MS = 100 * 365 * DAY_MS
DKIM_KEY_WAIT_SECONDS = 15
DKIM_KEY_POLL_SECONDS = 0.5
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


# Stalwart's "Email: Receive emails": without it, mail for the account bounces back to its sender.
RECEIVE_PERMISSION = "emailReceive"


def permissions_payload(disabled: list[str] | None) -> dict:
    """Permissions taken away from what the account's roles grant; none means it keeps them all."""

    if disabled:
        return {"@type": "Merge", "enabledPermissions": {}, "disabledPermissions": id_set(disabled)}
    return {"@type": "Inherit"}


def with_permission_disabled(permissions: dict | None, permission: str, disabled: bool) -> dict:
    """``permissions`` with one permission disabled or no longer disabled, the rest as it was.

    An account may carry grants and denials of its own, set by hand on the cluster, or a list
    that replaces what its roles grant; none of that is this change's to touch. A merge left with
    nothing to merge goes back to plain inheritance.
    """

    permissions = permissions or {}
    kind = permissions.get("@type") or "Inherit"
    enabled = dict(permissions.get("enabledPermissions") or {})
    denied = dict(permissions.get("disabledPermissions") or {})
    if disabled:
        denied[permission] = True
    else:
        denied.pop(permission, None)

    if kind == "Inherit":
        kind = "Merge"  # inheritance holds no lists of its own
    if kind == "Merge" and not enabled and not denied:
        return {"@type": "Inherit"}
    return {"@type": kind, "enabledPermissions": enabled, "disabledPermissions": denied}


# Stalwart's ``StorageQuota`` enum: what an account or group may hold. Disk space is in bytes,
# the rest are counts. An absent key means the cluster's default (usually no limit).
STORAGE_QUOTAS = {
    "maxDiskQuota": "Maximum disk space allocated (bytes)",
    "maxEmails": "Maximum number of emails",
    "maxMailboxes": "Maximum number of mailboxes",
    "maxEmailSubmissions": "Maximum number of email submissions",
    "maxEmailIdentities": "Maximum number of email identities",
    "maxParticipantIdentities": "Maximum number of participant identities",
    "maxSieveScripts": "Maximum number of Sieve scripts",
    "maxPushSubscriptions": "Maximum number of push subscriptions",
    "maxCalendars": "Maximum number of calendars",
    "maxCalendarEvents": "Maximum number of calendar events",
    "maxCalendarEventNotifications": "Maximum number of calendar event notifications",
    "maxAddressBooks": "Maximum number of address books",
    "maxContactCards": "Maximum number of contact cards",
    "maxFiles": "Maximum number of files",
    "maxFolders": "Maximum number of folders",
    "maxMaskedAddresses": "Maximum number of masked email addresses",
    "maxAppPasswords": "Maximum number of app passwords",
    "maxApiKeys": "Maximum number of API keys",
    "maxPublicKeys": "Maximum number of public keys",
}
DISK_QUOTA = "maxDiskQuota"


def quotas_payload(disk_quota_bytes: int | None, other: dict[str, int] | None = None) -> dict:
    """The full ``quotas`` map: disk space from the quota field, everything else from ``other``."""

    payload = {k: int(v) for k, v in (other or {}).items() if int(v) > 0}
    if disk_quota_bytes:
        payload[DISK_QUOTA] = int(disk_quota_bytes)
    return payload


@dataclass
class Account:
    name: str
    domain_id: str
    password: str | None = None
    member_group_ids: list[str] | None = None
    role_ids: list[str] | None = None
    disabled_permissions: list[str] | None = None
    aliases: list[EmailAlias] | None = None
    description: str | None = None
    locale: str = DEFAULT_LOCALE
    time_zone: str | None = None
    disk_quota_bytes: int | None = None
    quotas: dict[str, int] | None = None

    def to_dict(self) -> dict:
        credentials = {"0": {"@type": "Password", "secret": self.password}} if self.password else {}
        return {
            "@type": "User",
            "name": self.name,
            "domainId": self.domain_id,
            "credentials": credentials,
            "memberGroupIds": id_set(self.member_group_ids),
            "roles": roles_payload(self.role_ids),
            "permissions": permissions_payload(self.disabled_permissions),
            "quotas": quotas_payload(self.disk_quota_bytes, self.quotas),
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
    disabled_permissions: list[str] | None = None
    aliases: list[EmailAlias] | None = None
    disk_quota_bytes: int | None = None
    quotas: dict[str, int] | None = None

    def to_dict(self) -> dict:
        return {
            "@type": "Group",
            "name": self.name,
            "domainId": self.domain_id,
            "permissions": permissions_payload(self.disabled_permissions),
            "quotas": quotas_payload(self.disk_quota_bytes, self.quotas),
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
    """Automatic DKIM: Stalwart generates and holds the keys, under fixed selectors and without
    rotation; Manual when no algorithm is wanted."""

    if not algorithms:
        return {"@type": "Manual"}

    return {
        "@type": "Automatic",
        "algorithms": id_set(algorithms),
        "selectorTemplate": DKIM_SELECTOR_TEMPLATE,
        "rotateAfter": DKIM_ROTATE_AFTER_MS,
        # Stalwart's defaults, stated so a sync sees the live object as equal.
        "retireAfter": 7 * DAY_MS,
        "deleteAfter": 30 * DAY_MS,
    }


def count_dkim_selectors(zone_file: str) -> int:
    return len(re.findall(r"^\S+\._domainkey\.", zone_file, re.MULTILINE))


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
        "permissions",
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
            # No password row to patch into: replace the whole map (patch paths need existing parents).
            credentials = {
                **credentials,
                str(len(credentials)): {"@type": "Password", "secret": new_password},
            }
            self.update(account_id, {"credentials": credentials})
        else:
            self.update(account_id, {f"credentials/{row}/secret": new_password})

    def changed_permissions(self, account_id: str, permission: str, disabled: bool) -> dict:
        """The account's permissions with one of them disabled or no longer disabled, the others
        intact. A tagged union like roles: read here, then sent back whole in an update."""

        current = (self.get(account_id, properties=["permissions"]) or {}).get("permissions")
        return with_permission_disabled(current, permission, disabled)

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

    def get_zone_file(self, domain_id: str, expected_dkim_keys: int = 0) -> str:
        """The zone Stalwart expects the owner to publish.

        Stalwart generates DKIM keys after the domain exists, RSA taking noticeably longer than
        Ed25519, so a read straight after creation can miss a selector. With ``expected_dkim_keys``
        the read is repeated for a short while until that many selectors appear; on timeout the
        latest zone is returned and the scheduled refresh picks the rest up later.
        """

        deadline = time.monotonic() + DKIM_KEY_WAIT_SECONDS
        while True:
            zone_file = (self.get(domain_id, properties=["dnsZoneFile"]) or {}).get("dnsZoneFile") or ""
            if count_dkim_selectors(zone_file) >= expected_dkim_keys or time.monotonic() >= deadline:
                return zone_file
            time.sleep(DKIM_KEY_POLL_SECONDS)

    def replace_dkim_keys(self, domain_id: str, algorithms: tuple[str, ...]) -> None:
        """Deletes every key of the domain and has Stalwart generate fresh ones, same selectors.

        For a leaked signing key. Stalwart generates keys when a domain comes under automatic
        management, not when its signatures vanish (checked on v0.16.20), so the domain is taken
        through manual management and back. The policy is set anew on the way, so a domain
        created under another template lands on the fixed selectors too.

        Three requests with no transaction around them, ordered so that nothing is lost before
        the first succeeds and a run cut short anywhere can be repeated: a domain already under
        manual management skips straight to the delete, and the step that generates the new keys
        is tried twice before the domain is reported keyless.
        """

        live = self.get(domain_id, properties=["id", "dkimManagement"]) or {}
        if (live.get("dkimManagement") or {}).get("@type") != "Manual":
            self.update(domain_id, {"dkimManagement": {"@type": "Manual"}})  # the old keys still sign
        dkim = DkimSignatureService(self.connection)
        if signature_ids := [s["id"] for s in dkim.get_all_by_domain(domain_id)]:
            dkim.delete(signature_ids)

        failure = None
        for _attempt in range(2):
            try:
                self.update(domain_id, {"dkimManagement": dkim_management_payload(algorithms)})
                return
            except StalwartError as e:
                failure = e
        raise StalwartKeylessDomainError(
            f"The keys were deleted but new ones could not be requested ({failure}); "
            "run Replace DKIM Keys again to recover the domain.",
            self.type,
        ) from failure

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

    def find_by_name(self, name: str, domain_id: str) -> dict | None:
        # MailingList/query only documents text and tenant filters; match locally instead.
        return self.find_local(name=name, domainId=domain_id)

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
        # Role/query only documents a text filter; roles are few, so match locally.
        return self.find_local(description=description)


class DmarcReportService(ManagementService):
    """DMARC aggregate reports other receivers sent about the cluster's domains.

    Stalwart intercepts them on arrival (``ReportSettings.inboundReportAddresses``, ``postmaster@*``
    by default), parses them and keeps them for ``DataRetention.holdMtaReportsFor``; the object
    carries the whole parsed report, so nothing here ever reads mail.
    """

    type = "DmarcExternalReport"


class TlsReportService(ManagementService):
    """TLS aggregate reports (RFC 8460) other senders sent about delivering to the cluster's domains.

    Intercepted, parsed and kept the same way as the DMARC reports.
    """

    type = "TlsExternalReport"
