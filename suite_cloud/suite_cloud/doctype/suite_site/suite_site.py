# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now

from suite_cloud.utils import get_config

DIRECTORY_DOCTYPES = ("Mail Account", "Mail Group", "Mailing List", "Mail Domain")


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
        default_disk_quota_gb: DF.Float
        domain_verification_token: DF.Data | None
        egress_pool: DF.Link | None
        enabled: DF.Check
        fc_reference: DF.Data | None
        max_accounts: DF.Int
        max_domains: DF.Int
        site_name: DF.Data
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

        self.user = get_config("site_service_user")
        if not self.user:
            frappe.throw(_("The site service user is missing; run bench migrate."))

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

    def domain_count(self) -> int:
        return frappe.db.count("Mail Domain", {"site": self.name})

    def account_count(self) -> int:
        return frappe.db.count("Mail Account", {"site": self.name})

    def assert_can_add_domain(self) -> None:
        if self.max_domains and self.domain_count() >= self.max_domains:
            frappe.throw(
                _("Site {0} has reached its limit of {1} domains.").format(self.name, self.max_domains)
            )

    def assert_can_add_account(self) -> None:
        if self.max_accounts and self.account_count() >= self.max_accounts:
            frappe.throw(
                _("Site {0} has reached its limit of {1} accounts.").format(self.name, self.max_accounts)
            )

    # --- helpers ----------------------------------------------------------------

    def get_cluster(self) -> Document:
        return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

    def to_api(self) -> dict:
        cluster = self.get_cluster()
        return {
            "site": self.name,
            "cluster": self.cluster,
            "status": self.status,
            "enabled": bool(self.enabled),
            "jmap_url": cluster.base_url,
            "mail_hostname": cluster.hostname,
            "limits": {
                "max_domains": self.max_domains,
                "max_accounts": self.max_accounts,
                "default_disk_quota_gb": self.default_disk_quota_gb,
            },
            "usage": {"domains": self.domain_count(), "accounts": self.account_count()},
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
