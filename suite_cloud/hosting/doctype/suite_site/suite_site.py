# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from uuid import uuid7

import frappe
from frappe import _
from frappe.model.document import Document

from suite_cloud.hosting import domains, labels, sites
from suite_cloud.hosting.central import site as site_api
from suite_cloud.mail.tenancy.addresses import validate_email_address
from suite_cloud.utils import get_config


class SuiteSite(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        from suite_cloud.hosting.doctype.suite_site_domain.suite_site_domain import SuiteSiteDomain

        activated_at: DF.Datetime | None
        assign_site_domain: DF.Check
        attempts: DF.Int
        domains: DF.Table[SuiteSiteDomain]
        dropped_at: DF.Datetime | None
        email_account_created: DF.Check
        error: DF.SmallText | None
        hostname: DF.Data | None
        label: DF.Data | None
        mail_domain: DF.Link | None
        notification_account: DF.Link | None
        owner_invite: DF.Data | None
        owner_invited_at: DF.Datetime | None
        pilot_task: DF.Data | None
        primary_domain: DF.Data | None
        region: DF.Link
        site_created: DF.Check
        status: DF.Literal[
            "Pending",
            "Creating",
            "Ready",
            "Assigned",
            "Configuring",
            "Active",
            "Failed",
            "Dropping",
            "Dropped",
        ]
        suite_configured: DF.Check
        tenant: DF.Link | None
        virtual_machine: DF.Link | None
    # end: auto-generated types

    def autoname(self) -> None:
        self.name = str(uuid7())

    def before_insert(self) -> None:
        self.status = "Pending"
        if self.virtual_machine:
            self.hostname = sites.new_hostname(self.get_virtual_machine())
        else:
            sites.place(self)  # no server with room: the site waits for one

    def validate(self) -> None:
        self.validate_assignments()

    def validate_assignments(self) -> None:
        """The site domain cannot be opted out of once it was added; unticking would not undo it."""

        before = None if self.is_new() else self.get_doc_before_save()
        if (
            before
            and any(row.managed for row in before.domains)
            and self.has_value_changed("assign_site_domain")
        ):
            frappe.throw(_("Assign Site Domain cannot change: the domain is already assigned."))

    def after_insert(self) -> None:
        sites.enqueue_provision(self.name)

    def on_trash(self) -> None:
        if self.site_created and self.status != "Dropped":
            frappe.throw(_("Drop the site before deleting it."))

    # --- state ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"https://{self.primary_domain or self.hostname}"

    @property
    def site_domain(self) -> str | None:
        return labels.site_domain(self.label) if self.label else None

    @property
    def owner_email(self) -> str:
        """Where the tenant's owner reads mail: their signup address, else their Suite Cloud user's."""

        if request := self.get_request():
            return request.email
        return frappe.db.get_value("User", frappe.db.get_value("Suite Tenant", self.tenant, "user"), "email")

    @property
    def login_email(self) -> str:
        """The owner's account on the site: their own local part at the tenant's mail domain, e.g.
        john@acme.example.com for john@example.com."""

        if not self.mail_domain:
            frappe.throw(_("Site {0} has no mail domain yet.").format(self.name))
        return validate_email_address(f"{labels.login_local_part(self.owner_email)}@{self.mail_domain}")

    @property
    def follow_up_skip_reason(self) -> str | None:
        if (
            self.assign_site_domain
            and self.site_domain
            and not self.get_virtual_machine().letsencrypt_email_set
        ):
            return _("The server has no Let's Encrypt email, so the site domain is skipped.")
        return None

    @property
    def has_site_domain(self) -> bool:
        """Attached, and the site has a primary domain: the site domain at first, though the operator
        may make another one primary later."""

        row = self.get_domain(self.site_domain)
        return bool(row and row.status == "Active" and self.primary_domain)

    def pending_follow_ups(self) -> list[tuple[str, object]]:
        from suite_cloud.mail.tenancy import hosted_domain

        steps = []
        if (
            self.assign_site_domain
            and self.site_domain
            and not self.has_site_domain
            and not self.follow_up_skip_reason
        ):
            steps.append((_("Site domain"), domains.ensure_site_domain))
        if not self.email_account_created:
            steps.append((_("Mail"), hosted_domain.ensure))
        elif not self.owner_invite:
            steps.append((_("Owner invite"), sites.invite_owner))  # sent from the notification account
        return steps

    # --- related --------------------------------------------------------------------

    def get_virtual_machine(self) -> Document:
        if not self.virtual_machine:
            frappe.throw(_("Site {0} has no server yet.").format(self.name))
        return frappe.get_doc("Virtual Machine", self.virtual_machine)

    def get_pilot_client(self):
        return self.get_virtual_machine().get_pilot_client()

    def get_site_client(self):
        return site_api.get_site_client(self)

    def get_domain(self, domain: str):
        domain = (domain or "").strip().lower()
        return next((row for row in self.domains if row.domain == domain), None)

    def get_request(self) -> Document | None:
        """The tenant's signup, if it signed up: it holds the password and time zone."""

        if not self.tenant:
            return None
        name = frappe.db.get_value(
            "Suite Tenant Request", {"tenant": self.tenant}, "name", order_by="creation desc"
        )
        return frappe.get_doc("Suite Tenant Request", name) if name else None

    # --- actions ----------------------------------------------------------------------

    @frappe.whitelist()
    def login_as_administrator(self) -> str:
        """A fresh login link for the operator's browser; nothing is kept."""

        frappe.only_for("System Manager")
        self.assert_created()
        return self.get_pilot_client().site_login_url(self.hostname)

    @frappe.whitelist()
    def resend_owner_invite(self) -> None:
        frappe.only_for("System Manager")
        sites.resend_owner_invite(self)

    @frappe.whitelist()
    def retry(self) -> None:
        frappe.only_for("System Manager")
        sites.retry(self)

    @frappe.whitelist()
    def add_domain(self, domain: str) -> dict:
        frappe.only_for("System Manager")
        self.assert_takes_domains()
        return domains.add_domain(self, domain)

    @frappe.whitelist()
    def verify_domain(self, domain: str) -> bool:
        frappe.only_for("System Manager")
        self.assert_takes_domains()
        return domains.verify_domain(self, domain)

    @frappe.whitelist()
    def make_primary(self, domain: str) -> None:
        frappe.only_for("System Manager")
        domains.make_primary(self, domain)

    @frappe.whitelist()
    def remove_domain(self, domain: str) -> None:
        frappe.only_for("System Manager")
        domains.remove_domain(self, domain)

    @frappe.whitelist()
    def drop_site(self) -> None:
        frappe.only_for("System Manager")
        sites.enqueue_drop(self.name)

    def assert_created(self) -> None:
        if not self.site_created or self.status == "Dropped":
            frappe.throw(_("Site {0} does not exist on its server.").format(self.name))

    def assert_takes_domains(self) -> None:
        """Pilot holds the certificates of custom domains, which needs a Let's Encrypt email."""

        self.assert_created()
        if not self.get_virtual_machine().letsencrypt_email_set:
            frappe.throw(_("The server has no Let's Encrypt email, so it cannot take custom domains."))
