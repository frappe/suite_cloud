# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import ipaddress
from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, now, now_datetime

from suite_cloud.mail.stalwart import get_client
from suite_cloud.mail.stalwart.errors import StalwartError
from suite_cloud.mail.tenancy.usage import USAGE_PROPERTIES
from suite_cloud.utils import get_config, utc_iso

DIRECTORY_DOCTYPES = ("Mail Account", "Mail Group", "Mailing List", "Mail Domain")
REPORT_DOCTYPES = ("DMARC Report", "TLS Report")
MAILBOX_DOCTYPES = ("Mail Account", "Mail Group")  # the documents whose disk usage the tenant sums
DISK_USAGE_CACHE_TTL = 300  # seconds a tenant's summed disk usage is served without asking the cluster


class MailNotConfiguredError(frappe.ValidationError):
    """The tenant has no Stalwart cluster yet, so there is no mail to act on."""

    http_status_code = 422


class SuiteTenant(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        archived_at: DF.Datetime | None
        contact_email: DF.Data | None
        egress_pool: DF.Link | None
        enabled: DF.Check
        max_accounts: DF.Int
        max_disk_gb: DF.Float
        max_domains: DF.Int
        max_groups: DF.Int
        max_mailing_lists: DF.Int
        secret: DF.Password | None
        stalwart_cluster: DF.Link | None
        title: DF.Data
        status: DF.Literal["Active", "Suspended", "Archived"]
        user: DF.Link | None
        used_disk_bytes: DF.Int
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def autoname(self) -> None:
        self.name = str(uuid7())

    def before_insert(self) -> None:
        self.status = "Active"
        self.new_secret = self.generate_secret()

    def validate(self) -> None:
        self.title = (self.title or "").strip()
        if self.contact_email:
            self.contact_email = self.contact_email.strip().lower()
            if not frappe.utils.validate_email_address(self.contact_email):
                frappe.throw(_("{0} is not a valid email address.").format(self.contact_email))
        self.allowed_ips = "\n".join(str(n) for n in parse_networks(self.allowed_ips))

        self.user = get_config("tenant_service_user")
        if not self.user:
            frappe.throw(_("The tenant service user is missing; run bench migrate."))
        self.validate_limits()
        self.validate_stalwart_cluster()

    def validate_stalwart_cluster(self) -> None:
        """The cluster may be set, changed or cleared only while nothing of the tenant lives on it."""

        if not self.is_new() and self.has_value_changed("stalwart_cluster") and self.has_mail_data():
            # Ids, mailboxes and reports live on the old cluster; moving them is a migration.
            frappe.throw(
                _("Tenant {0} has mail data on cluster {1}; the cluster cannot be changed.").format(
                    self.name, self.get_doc_before_save().stalwart_cluster
                )
            )
        if not self.stalwart_cluster:
            self.egress_pool = None
            return
        if self.has_value_changed("stalwart_cluster"):
            cluster = self.get_cluster()
            if not cluster.enabled or cluster.status != "Active":
                frappe.throw(_("Cluster {0} is not active.").format(self.stalwart_cluster))
        if (
            self.egress_pool
            and frappe.db.get_value("Egress IP Pool", self.egress_pool, "cluster") != self.stalwart_cluster
        ):
            frappe.throw(_("Egress pool {0} belongs to another cluster.").format(self.egress_pool))

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if before and before.egress_pool != self.egress_pool:
            from suite_cloud.mail.cluster import egress

            for name in {before.stalwart_cluster, self.stalwart_cluster} - {None}:
                egress.resync_cluster(frappe.get_cached_doc("Stalwart Cluster", name))

    def on_trash(self) -> None:
        if self.status != "Archived":
            frappe.throw(_("Archive the tenant before deleting it."))
        if frappe.db.exists("Mail Domain", {"tenant": self.name}):
            frappe.throw(_("Delete the tenant's mail domains first."))

    # --- secrets --------------------------------------------------------------

    def generate_secret(self) -> str:
        secret = frappe.generate_hash(length=40)
        self.secret = secret
        return secret

    @frappe.whitelist()
    def adopt_directory(self) -> dict:
        """Records the domains, accounts, groups and lists the cluster already holds for this tenant."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        from suite_cloud.mail.tenancy.adopt import adopt_directory

        return adopt_directory(self.name)

    @frappe.whitelist()
    def rotate_secret(self) -> str:
        """Returns the new secret once; it is stored encrypted and never shown again."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        secret = self.generate_secret()
        self.save(ignore_permissions=True)
        return secret

    # --- state ----------------------------------------------------------------

    @frappe.whitelist()
    def suspend(self) -> None:
        # The key keeps authenticating so the tenant gets a 403 naming the suspension, not a bare 401.
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        if self.status == "Archived":
            frappe.throw(_("An archived tenant cannot be suspended."))
        self.db_set({"status": "Suspended"})

    @frappe.whitelist()
    def resume(self) -> None:
        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        if self.status == "Archived":
            frappe.throw(_("An archived tenant cannot be resumed."))
        self.db_set({"enabled": 1, "status": "Active"})

    @frappe.whitelist()
    def archive(self, delete_data: bool = False) -> None:
        """Locks the tenant out; with ``delete_data`` every directory object is removed from Stalwart too."""

        frappe.only_for(("System Manager", "Suite Cloud Manager"))
        self.db_set({"enabled": 0, "status": "Archived", "archived_at": now()})
        if delete_data:
            if frappe.flags.do_not_enqueue:
                purge_directory(self.name)
            else:
                frappe.enqueue(
                    purge_directory,
                    tenant=self.name,
                    queue="long",
                    job_id=f"purge-tenant:{self.name}",
                    deduplicate=True,
                    enqueue_after_commit=True,
                )

    # --- limits -----------------------------------------------------------------

    def validate_limits(self) -> None:
        for field in ("max_domains", "max_accounts", "max_groups", "max_mailing_lists"):
            if cint(self.get(field)) < 0:
                frappe.throw(
                    _("{0} cannot be negative; 0 means unlimited.").format(self.meta.get_label(field))
                )
        if flt(self.max_disk_gb) < 0:
            frappe.throw(_("Total Disk Quota cannot be negative; 0 means unlimited."))

    def domain_count(self) -> int:
        return frappe.db.count("Mail Domain", {"tenant": self.name})

    def account_count(self) -> int:
        return frappe.db.count("Mail Account", {"tenant": self.name})

    def group_count(self) -> int:
        return frappe.db.count("Mail Group", {"tenant": self.name})

    def mailing_list_count(self) -> int:
        return frappe.db.count("Mailing List", {"tenant": self.name})

    def assert_can_add_domain(self) -> None:
        self.lock()
        self.assert_within_limit(self.max_domains, self.domain_count(), _("domains"))

    def assert_can_add_account(self) -> None:
        self.lock()
        self.assert_within_limit(self.max_accounts, self.account_count(), _("accounts"))

    def assert_can_add_group(self) -> None:
        self.lock()
        self.assert_within_limit(self.max_groups, self.group_count(), _("groups"))

    def assert_can_add_mailing_list(self) -> None:
        self.lock()
        self.assert_within_limit(self.max_mailing_lists, self.mailing_list_count(), _("mailing lists"))

    def has_mail_data(self) -> bool:
        """Whether any directory object or report is recorded for the tenant."""

        return any(
            frappe.db.exists(doctype, {"tenant": self.name})
            for doctype in DIRECTORY_DOCTYPES + REPORT_DOCTYPES
        )

    def onload(self) -> None:
        self.flags.with_usage = True  # the form shows the figure

    @property
    def used_disk_bytes(self) -> int | None:
        """The virtual field: every serialisation reads it, so only the form pays for the lookup."""

        return self.get_used_disk_bytes() if self.flags.with_usage else None

    def get_used_disk_bytes(self) -> int | None:
        """A cluster that cannot answer costs the figure, not the form or the tenant profile."""

        try:
            return self.get_disk_usage()["used_disk_bytes"]
        except StalwartError as e:
            frappe.log_error(
                title="[Suite Cloud] Disk usage lookup failed", message=str(e), defer_insert=True
            )
            return None

    def get_disk_usage(self, fresh: bool = False) -> dict:
        """``{used_disk_bytes, fetched_at}`` across the tenant's accounts and groups, kept for
        ``DISK_USAGE_CACHE_TTL`` seconds; ``fresh`` asks the cluster now."""

        key = f"suite_cloud:disk-usage:{self.name}"
        if not fresh and (usage := frappe.cache.get_value(key, expires=True)):
            return usage

        usage = {"used_disk_bytes": self.fetch_used_disk_bytes(), "fetched_at": utc_iso(now_datetime())}
        frappe.cache.set_value(key, usage, expires_in_sec=DISK_USAGE_CACHE_TTL)
        return usage

    def fetch_used_disk_bytes(self) -> int:
        """One cluster call per ``maxObjectsInGet`` ids, all on the tenant's cluster. A cluster that
        cannot answer raises: a partial sum must never be returned or cached."""

        ids = []
        for doctype in MAILBOX_DOCTYPES:
            ids += frappe.get_all(
                doctype, {"tenant": self.name, "stalwart_id": ["is", "set"]}, pluck="stalwart_id"
            )
        if not ids:
            return 0  # building a client already fetches the session
        objects = get_client(self.get_cluster()).accounts.get_many(ids, properties=USAGE_PROPERTIES)
        return sum(cint(o.get("usedDiskQuota")) for o in objects)

    def lock(self) -> None:
        """Serialises this tenant's limit checks: two requests must not both see room for the last slot."""

        if frappe.db.exists("Suite Tenant", self.name):
            frappe.db.get_value("Suite Tenant", self.name, "name", for_update=True)

    def assert_within_limit(self, limit: int, current: int, what: str) -> None:
        """A limit of 0 means unlimited."""

        if limit and current >= limit:
            frappe.throw(_("Tenant {0} has reached its limit of {1} {2}.").format(self.name, limit, what))

    # --- helpers ----------------------------------------------------------------

    def assert_mail_configured(self) -> None:
        if not self.stalwart_cluster:
            frappe.throw(
                _("Tenant {0} is not set up for Mail yet.").format(self.name), MailNotConfiguredError
            )

    def get_cluster(self) -> Document:
        self.assert_mail_configured()
        return frappe.get_cached_doc("Stalwart Cluster", self.stalwart_cluster)

    def allows_ip(self, ip: str | None) -> bool:
        """Whether a request from ``ip`` may use the tenant's secret. No list means any address."""

        networks = parse_networks(self.allowed_ips)
        if not networks:
            return True
        try:
            address = ipaddress.ip_address((ip or "").strip())
        except ValueError:
            return False
        return any(address in network for network in networks)

    def to_api(self) -> dict:
        cluster = self.get_cluster() if self.stalwart_cluster else None
        return {
            "tenant": self.name,
            "cluster": self.stalwart_cluster,
            "status": self.status,
            "title": self.title,
            "contact_email": self.contact_email,
            "enabled": bool(self.enabled),
            "allowed_ips": self.allowed_ips.split("\n") if self.allowed_ips else [],
            "jmap_url": cluster.base_url if cluster else None,
            "mail_hostname": cluster.hostname if cluster else None,
            "limits": {
                "max_domains": self.max_domains,
                "max_accounts": self.max_accounts,
                "max_groups": self.max_groups,
                "max_mailing_lists": self.max_mailing_lists,
                "max_disk_gb": self.max_disk_gb,
            },
            "usage": {
                "domains": self.domain_count(),
                "accounts": self.account_count(),
                "groups": self.group_count(),
                "mailing_lists": self.mailing_list_count(),
                "used_disk_bytes": self.get_used_disk_bytes(),
            },
        }


def parse_networks(text: str | None) -> list:
    """Addresses or CIDR ranges, one per line; a single address is its own /32 or /128."""

    networks = []
    for line in (text or "").splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            frappe.throw(_("{0} is not an IP address or CIDR range.").format(value))
    return networks


def purge_directory(tenant: str) -> None:
    """Deletes an archived tenant's directory one document at a time.

    Each delete is its own transaction: every document destroys its Stalwart object inside its
    delete, so a failure half-way must not resurrect rows whose objects are already gone.
    """

    for doctype in DIRECTORY_DOCTYPES:
        for name in frappe.get_all(doctype, {"tenant": tenant}, pluck="name"):
            frappe.delete_doc(doctype, name, ignore_permissions=True)
            if not frappe.in_test:
                frappe.db.commit()
