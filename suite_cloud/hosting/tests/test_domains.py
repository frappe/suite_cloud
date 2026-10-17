from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date

from suite_cloud.hosting import capacity, domains, labels, sites
from suite_cloud.hosting.central import site as site_api
from suite_cloud.hosting.labels import LabelNotAvailableError
from suite_cloud.hosting.tests.fakes import PROXY_ZONE, REGION
from suite_cloud.hosting.tests.fixtures import MAIL_ZONE, SITE_ZONE, HostingFixtures


class TestSiteDomains(HostingFixtures, IntegrationTestCase):
    def setUp(self) -> None:
        self.setup_hosting()
        self.vm = self.make_vm()
        self.tenant = self.make_hosted_tenant()

    def claim(self):
        return self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

    def records(self, site) -> set:
        rows = frappe.get_all(
            "DNS Record",
            {"managed_by_doctype": "Suite Site", "managed_by": site.name},
            ["host", "type", "value"],
        )
        return {(r.host, r.type, r.value) for r in rows}

    def test_the_site_domain_is_published_attached_and_primary(self) -> None:
        site = self.claim()

        domain = f"acme.{SITE_ZONE}"
        self.assertEqual(site.primary_domain, domain)
        self.assertEqual(site.url, f"https://{domain}")
        row = site.get_domain(domain)
        self.assertEqual((row.status, row.is_primary, row.managed), ("Active", 1, 1))
        self.assertEqual(
            self.records(site),
            {("acme", "CNAME", f"proxy.{PROXY_ZONE}"), ("_frappe-verification.acme", "TXT", "token-1")},
        )
        self.assertEqual(self.pilot.primary[site.hostname], domain)

    def test_a_site_domain_becomes_primary_only_once_it_serves_https(self) -> None:
        problem = f"https://acme.{SITE_ZONE} does not answer over HTTPS yet: wrong version number"
        with patch("suite_cloud.hosting.domains.get_https_problem", return_value=problem):
            site = self.claim()

        self.assertEqual(site.get_domain(f"acme.{SITE_ZONE}").status, "Active")  # attached
        self.assertFalse(site.primary_domain)  # the site keeps answering on its own host name
        self.assertIn("Site domain: waiting", site.error)
        self.assertIn("HTTPS", site.error)

        sites.provision(site.name)

        self.assertEqual(self.site(site.name).primary_domain, f"acme.{SITE_ZONE}")

    def test_a_domain_without_https_cannot_be_made_primary(self) -> None:
        site = self.claim()
        site.add_domain("shop.example.com")
        site.verify_domain("shop.example.com")

        with patch("suite_cloud.hosting.domains.get_https_problem", return_value="no TLS"):
            self.assertRaisesRegex(
                domains.DomainNotReadyError,
                "no TLS",
                domains.make_primary,
                self.site(site.name),
                "shop.example.com",
            )

    def test_the_site_domain_choice_is_fixed_once_the_domain_is_added(self) -> None:
        site = self.claim()
        site.assign_site_domain = 0
        self.assertRaisesRegex(frappe.ValidationError, "already assigned", site.save)

        pooled = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        pooled.reload()
        pooled.assign_site_domain = 0
        pooled.save()  # nothing assigned yet: the operator may opt the site out

    def test_the_site_domain_is_checked_only_once_the_provider_has_its_records(self) -> None:
        with (
            patch(
                "suite_cloud.suite_cloud.doctype.dns_record.dns_record.get_dns_provider", return_value=None
            ),
            patch("suite_cloud.hosting.domains.verify_dns_record") as verify,
        ):
            site = self.claim()

        verify.assert_not_called()
        self.assertEqual(site.get_domain(f"acme.{SITE_ZONE}").status, "Pending")

        sites.poll_pending_sites()  # the unconfirmed records are pushed again
        sites.poll_pending_sites()  # and found confirmed

        self.assertEqual(self.site(site.name).get_domain(f"acme.{SITE_ZONE}").status, "Active")

    def test_records_not_visible_yet_wait(self) -> None:
        with patch("suite_cloud.hosting.domains.verify_dns_record", return_value=False):
            site = self.claim()

        self.assertFalse(site.primary_domain)
        self.assertEqual(site.get_domain(f"acme.{SITE_ZONE}").status, "Pending")
        self.assertTrue(site.pending_follow_ups())
        self.assertNotIn("add_domain", [call[0] for call in self.pilot.calls])

    def test_stale_records_are_fetched_again_and_republished(self) -> None:
        with patch("suite_cloud.hosting.domains.verify_dns_record", return_value=False):
            site = self.claim()
        row = site.get_domain(f"acme.{SITE_ZONE}")
        frappe.db.set_value("Suite Site Domain", row.name, "records_fetched_at", add_to_date(None, hours=-24))
        self.pilot.token = "token-2"

        domains.ensure_site_domain(self.site(site.name))

        self.assertIn(("_frappe-verification.acme", "TXT", "token-2"), self.records(site))
        self.assertNotIn(("_frappe-verification.acme", "TXT", "token-1"), self.records(site))

    def test_already_attached_counts_as_active(self) -> None:
        site = frappe.get_doc({"doctype": "Suite Site", "region": REGION}).insert()
        self.pilot.attached[site.hostname] = {f"acme.{SITE_ZONE}"}

        site = self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

        self.assertEqual(site.get_domain(f"acme.{SITE_ZONE}").status, "Active")
        self.assertEqual(site.primary_domain, f"acme.{SITE_ZONE}")

    def test_a_manual_domain_is_added_verified_made_primary_and_removed(self) -> None:
        site = self.claim()

        records = site.add_domain("shop.example.com")
        self.assertEqual({r["type"] for r in records}, {"CNAME", "TXT"})  # A records are not offered
        self.assertTrue(self.site(site.name).verify_domain("shop.example.com"))
        self.site(site.name).make_primary("shop.example.com")

        site = self.site(site.name)
        self.assertEqual(site.primary_domain, "shop.example.com")
        self.assertRaisesRegex(frappe.ValidationError, "primary", site.remove_domain, "shop.example.com")
        site.make_primary(f"acme.{SITE_ZONE}")
        self.site(site.name).remove_domain("shop.example.com")
        self.assertIsNone(self.site(site.name).get_domain("shop.example.com"))
        # Records of a domain the tenant publishes are never Suite Cloud's.
        self.assertFalse(any("shop" in host for host, _type, _value in self.records(site)))

    def test_a_custom_primary_domain_is_kept(self) -> None:
        site = self.claim()
        site.add_domain("shop.example.com")
        self.site(site.name).verify_domain("shop.example.com")

        self.site(site.name).make_primary("shop.example.com")
        sites.poll_pending_sites()

        site = self.site(site.name)
        self.assertEqual(site.primary_domain, "shop.example.com")
        self.assertFalse(site.pending_follow_ups())

    def test_a_renewed_token_asks_for_republishing(self) -> None:
        site = self.claim()
        site.add_domain("shop.example.com")
        row = self.site(site.name).get_domain("shop.example.com")
        frappe.db.set_value("Suite Site Domain", row.name, "records_fetched_at", add_to_date(None, hours=-24))
        self.pilot.token = "token-2"

        self.assertRaises(domains.DomainNotReadyError, self.site(site.name).verify_domain, "shop.example.com")

    def test_custom_domains_need_a_letsencrypt_email(self) -> None:
        site = self.claim()
        self.vm.db_set("letsencrypt_email_set", 0)

        self.assertRaisesRegex(
            frappe.ValidationError, "Let's Encrypt", self.site(site.name).add_domain, "shop.example.com"
        )


class TestSharedZone(HostingFixtures, IntegrationTestCase):
    """Sites and mail in one zone: the mail domain takes acme, the site acme.sites."""

    def setUp(self) -> None:
        self.setup_hosting(site_dns_zone=MAIL_ZONE, tenant_mail_zone=MAIL_ZONE)
        self.make_vm()
        self.tenant = self.make_hosted_tenant()

    def test_the_site_domain_goes_under_sites(self) -> None:
        self.assertEqual(labels.site_domain("acme"), f"acme.sites.{MAIL_ZONE}")
        self.assertEqual(labels.mail_domain("acme"), f"acme.{MAIL_ZONE}")

        site = self.site(capacity.claim_site(self.tenant.name, "acme", REGION))

        self.assertEqual(site.primary_domain, f"acme.sites.{MAIL_ZONE}")
        hosts = set(
            frappe.get_all("DNS Record", {"managed_by": site.name, "dns_zone": MAIL_ZONE}, pluck="host")
        )
        self.assertEqual(hosts, {"acme.sites", "_frappe-verification.acme.sites"})

    def test_labels_taken_under_sites_are_not_offered(self) -> None:
        self.assertRaises(LabelNotAvailableError, labels.assert_label_available, "sites")
        capacity.claim_site(self.tenant.name, "acme", REGION)

        frappe.db.set_value("Suite Site", {"label": "acme"}, "status", "Dropped")  # its records remain
        self.assertRaises(LabelNotAvailableError, labels.assert_label_available, "acme")


class TestSiteClient(IntegrationTestCase):
    """The real client, with Pilot and the network replaced."""

    def site(self, primary_domain: str | None = None):
        pilot = frappe._dict(site_login_url=lambda name: f"https://{name}/app?sid=abc")
        site = frappe._dict(hostname="suite-1.example.com", primary_domain=primary_domain)
        site.get_virtual_machine = lambda: frappe._dict(get_pilot_client=lambda: pilot)
        return site

    def test_the_client_calls_the_primary_domain_once_there_is_one(self) -> None:
        self.assertEqual(
            site_api.get_site_client(self.site("acme.example.com")).url, "https://acme.example.com"
        )
        self.assertEqual(site_api.get_site_client(self.site()).url, "https://suite-1.example.com")

    def test_a_redirect_is_not_followed(self) -> None:
        client = site_api.get_site_client(self.site())
        response = requests.Response()
        response.status_code = 301
        response.headers["Location"] = "https://acme.example.com/api/v2/document/User/x"

        with patch.object(client.session, "request", return_value=response) as send:
            self.assertRaisesRegex(
                site_api.SiteUnavailableError, "redirected to https://acme", client.get_doc, "User", "x"
            )
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
