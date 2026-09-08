# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import flt, now

from suite_cloud.utils import get_config

DIRECTORY_DOCTYPES = ("Mail Account", "Mail Group", "Mailing List", "Mail Domain")


# Documents with a mailbox of their own, hence a quota that counts against the site's total.
QUOTA_HOLDERS = ("Mail Account", "Mail Group")


class SuiteSite(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        api_key: DF.Data | None
        api_secret: DF.Password | None
        archived_at: DF.Datetime | None
        cluster: DF.Link
        contact_email: DF.Data | None
        default_disk_quota_gb: DF.Float
        domain_verification_token: DF.Data | None
        egress_pool: DF.Link | None
        enabled: DF.Check
        fc_reference: DF.Data | None
        max_accounts: DF.Int
        max_disk_gb: DF.Float
        max_domains: DF.Int
        max_groups: DF.Int
        max_mailing_lists: DF.Int
        site_name: DF.Data
        title: DF.Data | None
        status: DF.Literal["Active", "Suspended", "Archived"]
        user: DF.Link | None
    # end: auto-generated types

    # --- lifecycle ------------------------------------------------------------

    def autoname(self) -> None:
        self.site_name = (self.site_name or "").strip().lower().rstrip("/")
        self.name = self.site_name

    def before_insert(self) -> None:
        self.status = "Active"
        self.api_key = frappe.generate_hash(length=32)
        self.new_secret = self.generate_secret()
        self.domain_verification_token = frappe.generate_hash(length=32)

    def validate(self) -> None:
        self.site_name = (self.site_name or "").strip().lower().rstrip("/")
        if "/" in self.site_name or " " in self.site_name or "." not in self.site_name:
            frappe.throw(_("Site Name must be the site's domain, e.g. acme.frappe.cloud"))
        self.title = (self.title or "").strip() or self.site_name
        if self.contact_email:
            self.contact_email = self.contact_email.strip().lower()
            frappe.utils.validate_email_address(self.contact_email, throw=True)

        self.user = get_config("site_service_user")
        if not self.user:
            frappe.throw(_("The site service user is missing; run bench migrate."))
        self.validate_disk_quotas()

        if self.is_new():
            cluster = frappe.get_cached_doc("Stalwart Cluster", self.cluster)
            if not cluster.enabled or cluster.status != "Active":
                frappe.throw(_("Cluster {0} is not active.").format(self.cluster))

        if (
            self.egress_pool
            and frappe.db.get_value("Egress IP Pool", self.egress_pool, "cluster") != self.cluster
        ):
            frappe.throw(_("Egress pool {0} belongs to another cluster.").format(self.egress_pool))

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if before and before.egress_pool != self.egress_pool:
            from suite_cloud.cluster import egress

            egress.resync_cluster(self.get_cluster())

    def on_trash(self) -> None:
        if self.status != "Archived":
            frappe.throw(_("Archive the site before deleting it."))
        if frappe.db.exists("Mail Domain", {"site": self.name}):
            frappe.throw(_("Delete the site's mail domains first."))

    # --- secrets --------------------------------------------------------------

    def generate_secret(self) -> str:
        secret = frappe.generate_hash(length=40)
        self.api_secret = secret
        return secret

    @frappe.whitelist()
    def rotate_secret(self) -> str:
        """Returns the new secret once; it is stored encrypted and never shown again."""

        frappe.only_for(("System Manager", "Suite Cloud Manager", "Frappe Cloud"))
        secret = self.generate_secret()
        self.save(ignore_permissions=True)
        return secret

    # --- state ----------------------------------------------------------------

    @frappe.whitelist()
    def suspend(self) -> None:
        # The key keeps authenticating so the site gets a 403 naming the suspension, not a bare 401.
        frappe.only_for(("System Manager", "Suite Cloud Manager", "Frappe Cloud"))
        self.db_set({"status": "Suspended"})

    @frappe.whitelist()
    def resume(self) -> None:
        frappe.only_for(("System Manager", "Suite Cloud Manager", "Frappe Cloud"))
        if self.status == "Archived":
            frappe.throw(_("An archived site cannot be resumed."))
        self.db_set({"enabled": 1, "status": "Active"})

    @frappe.whitelist()
    def archive(self, delete_data: bool = False) -> None:
        """Locks the site out; with ``delete_data`` every directory object is removed from Stalwart too."""

        frappe.only_for(("System Manager", "Suite Cloud Manager", "Frappe Cloud"))
        self.db_set({"enabled": 0, "status": "Archived", "archived_at": now()})
        if delete_data:
            if frappe.flags.do_not_enqueue:
                purge_directory(self.name)
            else:
                frappe.enqueue(
                    purge_directory,
                    site=self.name,
                    queue="long",
                    job_id=f"purge-site:{self.name}",
                    deduplicate=True,
                    enqueue_after_commit=True,
                )

    # --- limits -----------------------------------------------------------------

    def validate_disk_quotas(self) -> None:
        if flt(self.default_disk_quota_gb) <= 0:
            frappe.throw(_("Default Disk Quota must be above 0: every account needs a quota."))
        if flt(self.max_disk_gb) < 0:
            frappe.throw(_("Total Disk Quota cannot be negative; 0 means unlimited."))
        if flt(self.max_disk_gb) and flt(self.default_disk_quota_gb) > flt(self.max_disk_gb):
            frappe.throw(_("Default Disk Quota cannot exceed the site's Total Disk Quota."))

    def domain_count(self) -> int:
        return frappe.db.count("Mail Domain", {"site": self.name})

    def account_count(self) -> int:
        return frappe.db.count("Mail Account", {"site": self.name})

    def group_count(self) -> int:
        return frappe.db.count("Mail Group", {"site": self.name})

    def mailing_list_count(self) -> int:
        return frappe.db.count("Mailing List", {"site": self.name})

    def assert_can_add_domain(self) -> None:
        self.assert_within_limit(self.max_domains, self.domain_count(), _("domains"))

    def assert_can_add_account(self) -> None:
        self.assert_within_limit(self.max_accounts, self.account_count(), _("accounts"))

    def assert_can_add_group(self) -> None:
        self.assert_within_limit(self.max_groups, self.group_count(), _("groups"))

    def assert_can_add_mailing_list(self) -> None:
        self.assert_within_limit(self.max_mailing_lists, self.mailing_list_count(), _("mailing lists"))

    def allocated_disk_gb(self, exclude: tuple[str, str] | None = None) -> float:
        """Sum of the quotas of the site's accounts and groups, optionally leaving one document out."""

        total = 0.0
        for doctype in QUOTA_HOLDERS:
            table = frappe.qb.DocType(doctype)
            query = frappe.qb.from_(table).select(Sum(table.disk_quota_gb)).where(table.site == self.name)
            if exclude and exclude[0] == doctype:
                query = query.where(table.name != exclude[1])
            total += flt(query.run()[0][0])
        return total

    def validate_quota_of(self, doc: Document) -> None:
        """Accounts and groups: a quota above 0 (defaulting to the site's), within the site's total."""

        if doc.is_new() and doc.disk_quota_gb is None:
            doc.disk_quota_gb = self.default_disk_quota_gb
        if flt(doc.disk_quota_gb) <= 0:
            frappe.throw(_("Disk Quota must be above 0 GB."))
        before = doc.get_doc_before_save()
        if doc.is_new() or flt(before.disk_quota_gb) != flt(doc.disk_quota_gb):
            exclude = None if doc.is_new() else (doc.doctype, doc.name)
            self.assert_can_allocate_disk(doc.disk_quota_gb, exclude)

    def assert_can_allocate_disk(self, quota_gb: float, exclude: tuple[str, str] | None = None) -> None:
        """The site's accounts and groups together may not exceed its total disk quota (0 = unlimited)."""

        if not flt(self.max_disk_gb):
            return
        allocated = self.allocated_disk_gb(exclude)
        if allocated + flt(quota_gb) > flt(self.max_disk_gb):
            frappe.throw(
                _("Site {0} has {1} GB of its {2} GB total disk quota left; {3} GB requested.").format(
                    self.name, round(flt(self.max_disk_gb) - allocated, 2), self.max_disk_gb, quota_gb
                )
            )

    def assert_within_limit(self, limit: int, current: int, what: str) -> None:
        """A limit of 0 means unlimited."""

        if limit and current >= limit:
            frappe.throw(_("Site {0} has reached its limit of {1} {2}.").format(self.name, limit, what))

    # --- helpers ----------------------------------------------------------------

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def to_api(self) -> dict:
        cluster = self.get_cluster()
        return {
            "site": self.name,
            "cluster": self.cluster,
            "status": self.status,
            "title": self.title,
            "contact_email": self.contact_email,
            "enabled": bool(self.enabled),
            "jmap_url": cluster.base_url,
            "mail_hostname": cluster.hostname,
            "limits": {
                "max_domains": self.max_domains,
                "max_accounts": self.max_accounts,
                "max_groups": self.max_groups,
                "max_mailing_lists": self.max_mailing_lists,
                "max_disk_gb": self.max_disk_gb,
                "default_disk_quota_gb": self.default_disk_quota_gb,
            },
            "usage": {
                "domains": self.domain_count(),
                "accounts": self.account_count(),
                "groups": self.group_count(),
                "mailing_lists": self.mailing_list_count(),
                "allocated_disk_gb": self.allocated_disk_gb(),
            },
        }


def purge_directory(site: str) -> None:
    """Deletes an archived site's directory one document at a time.

    Each delete is its own transaction: every document destroys its Stalwart object inside its
    delete, so a failure half-way must not resurrect rows whose objects are already gone.
    """

    for doctype in DIRECTORY_DOCTYPES:
        for name in frappe.get_all(doctype, {"site": site}, pluck="name"):
            frappe.delete_doc(doctype, name, ignore_permissions=True)
            if not frappe.in_test:
                frappe.db.commit()
