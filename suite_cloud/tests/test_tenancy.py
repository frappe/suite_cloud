from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.stalwart import forget_sessions
from suite_cloud.tenancy.addresses import get_site_domain
from suite_cloud.tests.fake_stalwart import FakeStalwart
from suite_cloud.tests.fixtures import (
    activate_cluster,
    clear_request_cache,
    configure_settings,
    make_cluster,
    make_site,
)


class TenancyTestCase(IntegrationTestCase):
    """A cluster, a fake Stalwart behind it and a site; directory docs push to the fake."""

    def setUp(self) -> None:
        frappe.flags.do_not_enqueue = True
        configure_settings()
        self.cluster = activate_cluster(make_cluster())
        self.fake = FakeStalwart(
            base_url=self.cluster.base_url, admin_password=self.cluster.get_password("admin_password")
        )
        self.fake.add_token("test-token")
        # What the cluster plan sets at bootstrap: MX records point at the ingress hostname.
        self.fake.singletons["SystemSettings"] = {
            "mailExchangers": {"0": {"hostname": self.cluster.hostname, "priority": 10}}
        }
        self.fake._add(
            "Role", {"description": DISABLED_ROLE_DESCRIPTION, "enabledPermissions": {"emailReceive": True}}
        )
        self._install = self.fake.install()
        self._install.__enter__()
        self.addCleanup(self._install.__exit__, None, None, None)
        forget_sessions(self.cluster)
        clear_request_cache()
        self.site = make_site(self.cluster)

    def tearDown(self) -> None:
        for doctype in ("Mail Account", "Mail Group", "Mailing List", "Mail Domain"):
            for name in frappe.get_all(doctype, pluck="name"):
                frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_on_trash=True)
        frappe.flags.do_not_enqueue = False

    def make_domain(self, name: str = "acme.com", **fields):
        domain = frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": name, "site": self.site.name, **fields}
        )
        domain.insert()
        return domain

    def make_account(self, email: str, password: str = "secret-pw", **fields):
        account = frappe.get_doc(
            {"doctype": "Mail Account", "email": email, "site": self.site.name, **fields}
        )
        account.flags.password = password
        account.insert()
        return account


class TestSuiteSite(TenancyTestCase):
    def test_site_gets_credentials_and_service_user(self) -> None:
        self.assertEqual(len(self.site.api_key), 32)
        self.assertEqual(len(self.site.new_secret), 40)
        self.assertEqual(self.site.get_password("api_secret"), self.site.new_secret)
        self.assertEqual(self.site.user, "suite-site@suite-cloud.internal")
        self.assertEqual(self.site.status, "Active")
        self.assertEqual(self.site.to_api()["jmap_url"], self.cluster.base_url)

    def test_site_needs_an_active_cluster(self) -> None:
        self.cluster.db_set("status", "Pending")
        frappe.clear_document_cache("Stalwart Cluster", self.cluster.name)
        site = frappe.get_doc(
            {"doctype": "Suite Site", "site_name": "other.frappe.test", "cluster": self.cluster.name}
        )
        self.assertRaisesRegex(frappe.ValidationError, "not active", site.insert)

    def test_rotate_suspend_archive(self) -> None:
        old = self.site.get_password("api_secret")
        new = self.site.rotate_secret()
        self.assertNotEqual(old, new)
        self.assertEqual(self.site.get_password("api_secret"), new)

        self.site.suspend()
        self.assertEqual(
            frappe.db.get_value("Suite Site", self.site.name, ["enabled", "status"]), (1, "Suspended")
        )
        self.site.resume()
        self.assertEqual(frappe.db.get_value("Suite Site", self.site.name, "status"), "Active")

        self.make_domain()
        self.make_account("a@acme.com")
        self.site.archive(delete_data=True)
        self.assertEqual(frappe.db.get_value("Suite Site", self.site.name, "status"), "Archived")
        self.assertFalse(frappe.db.exists("Mail Domain", "acme.com"))
        self.assertEqual(self.fake.all("Domain"), [])
        self.assertEqual([a for a in self.fake.all("Account") if a["name"] != "admin"], [])


class TestMailDomain(TenancyTestCase):
    def test_domain_is_created_on_stalwart_with_dns_records(self) -> None:
        domain = self.make_domain()

        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(domain.stalwart_id, live["id"])
        # RSA only unless the setting opts into Ed25519: many receivers ignore Ed25519 signatures.
        self.assertEqual(live["dkimManagement"]["algorithms"], {"Dkim1RsaSha256": True})
        self.assertEqual(live["dnsManagement"], {"@type": "Manual"})
        self.assertEqual(live["reportAddressUri"], "mailto:postmaster@acme.com")

        # Rows land in the table of their group; authentication rows are the mandatory ones.
        auth = [(r.category, r.host, r.is_mandatory) for r in domain.authentication_records]
        self.assertEqual(
            auth, [("SPF", "@", 1), ("DKIM", "v1-rsa-20260101._domainkey", 1), ("DMARC", "_dmarc", 1)]
        )
        spf = domain.authentication_records[0]
        self.assertEqual(spf.value, f"v=spf1 include:spf.{self.cluster.default_domain} -all")
        mx = domain.routing_records[0]
        self.assertEqual(
            (mx.category, mx.value, mx.priority, mx.fqdn), ("MX", self.cluster.hostname, 10, "acme.com")
        )
        self.assertEqual(mx.is_mandatory, 0)
        self.assertEqual([r.category for r in domain.transport_security_records], ["TLS-RPT"])
        srv = next(r for r in domain.discovery_records if r.host == "_imaps._tcp")
        self.assertEqual((srv.priority, srv.weight, srv.port, srv.value), (0, 1, 993, self.cluster.hostname))
        # Resolvers answer SRV with all four fields, so that is what verification compares against.
        self.assertEqual(domain.expected_value(srv), f"0 1 993 {self.cluster.hostname}")
        self.assertEqual(domain.autoconfig_records, [])  # certificate-bound: opt-in
        api = domain.to_api()
        self.assertEqual(
            [g["key"] for g in api["dns_record_groups"]][:2], ["authentication_records", "routing_records"]
        )
        self.assertEqual(api["dns_records"][0]["group"], "authentication_records")
        self.assertFalse(domain.is_verified)

    def test_domain_updates_push_and_refresh_keeps_verification(self) -> None:
        domain = self.make_domain()
        domain.routing_records[0].is_verified = 1
        domain.save_records()

        domain.description = "Main"
        domain.catch_all_address = "Catch@Acme.com"
        domain.publish_client_discovery_records = 1
        domain.save()
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(live["description"], "Main")
        self.assertEqual(live["catchAllAddress"], "catch@acme.com")

        # Turning the discovery flag on lists the certificate-bound records from the stored zone.
        self.assertIn("MTA-STS", [r.category for r in domain.transport_security_records])
        self.assertEqual([r.category for r in domain.autoconfig_records], ["Autoconfig", "Autodiscover"])
        self.assertEqual([r.is_verified for r in domain.routing_records], [1])

        domain.publish_client_discovery_records = 0
        domain.save()
        self.assertEqual([r.category for r in domain.transport_security_records], ["TLS-RPT"])
        self.assertEqual(domain.autoconfig_records, [])
        self.assertEqual([r.is_verified for r in domain.routing_records], [1])

    def test_domain_limits_reserved_names_and_ownership(self) -> None:
        self.site.db_set("max_domains", 1)
        frappe.clear_document_cache("Suite Site", self.site.name)
        self.make_domain()
        self.assertRaisesRegex(frappe.ValidationError, "limit", self.make_domain, "second.com")

        self.site.db_set("max_domains", 5)
        frappe.clear_document_cache("Suite Site", self.site.name)
        self.assertRaisesRegex(
            frappe.ValidationError, "reserved", self.make_domain, self.cluster.default_domain
        )
        self.assertRaisesRegex(frappe.ValidationError, "not a valid domain", self.make_domain, "bad_domain")

        other = make_site(self.cluster, "other.frappe.test")
        self.assertRaises(frappe.DoesNotExistError, get_site_domain, other.name, "acme.com")

    def test_domain_goes_live_only_once_verified(self) -> None:
        domain = self.make_domain()
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

        for row in domain.dns_rows():
            row.is_verified = 1
        domain.save_records()
        # Simulate a verification pass where every record already resolves.
        with patch(
            "suite_cloud.suite_cloud.doctype.mail_domain.mail_domain.verify_dns_record", return_value=True
        ):
            result = domain.verify_dns_records()

        self.assertTrue(result["is_verified"])
        self.assertTrue(self.fake.find("Domain", name="acme.com")["isEnabled"])

        domain.enabled = 0
        domain.save()
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

    def test_verification_rule_and_inconclusive_lookups(self) -> None:
        domain = self.make_domain()
        for row in domain.dns_rows():
            row.is_verified = 1
        domain.save_records()

        def resolve(fqdn, type, value):
            return None if "_domainkey" in fqdn else True  # DKIM lookups time out

        with patch(
            "suite_cloud.suite_cloud.doctype.mail_domain.mail_domain.verify_dns_record", side_effect=resolve
        ):
            result = domain.verify_dns_records()
        self.assertTrue(result["is_verified"])  # DKIM rows kept their verified state
        self.assertEqual(result["inconclusive"], 1)  # the one DKIM row

        # One verified DKIM selector is enough after a rotation adds an unpublished one.
        domain.append(
            "authentication_records",
            {
                "category": "DKIM",
                "record_type": "TXT",
                "host": "v2._domainkey",
                "value": "v=DKIM1",
                "is_mandatory": 1,
            },
        )
        self.assertTrue(domain.compute_is_verified())
        for row in domain.authentication_records:
            if row.category == "DKIM":
                row.is_verified = 0
        self.assertFalse(domain.compute_is_verified())

        # MX is the owner's choice: a sending-only domain verifies without it.
        for row in domain.authentication_records:
            row.is_verified = 1
        domain.routing_records[0].is_verified = 0
        self.assertTrue(domain.compute_is_verified())

    def test_reserved_names_cover_every_cluster_zone(self) -> None:
        self.assertRaisesRegex(
            frappe.ValidationError, "reserved", self.make_domain, "mail.other.example.test"
        )

    def test_domain_name_collision_is_neutral(self) -> None:
        self.make_domain()
        other = make_site(self.cluster, "other.frappe.test")
        doc = frappe.get_doc({"doctype": "Mail Domain", "domain_name": "acme.com", "site": other.name})
        self.assertRaisesRegex(frappe.DuplicateEntryError, "not available", doc.insert)

    def test_domain_creation_waits_for_dkim_keys_still_being_generated(self) -> None:
        # Stalwart generates the RSA key after the domain exists; the first zone read misses it.
        real_zone_file = FakeStalwart._zone_file
        reads = []

        def lagging_zone_file(fake, domain):
            zone = real_zone_file(fake, domain)
            if reads or domain["id"] not in fake.objects["Domain"]:  # creation renders it too
                return zone
            reads.append(domain["id"])
            return "\n".join(line for line in zone.splitlines() if "_domainkey" not in line) + "\n"

        with (
            patch.object(FakeStalwart, "_zone_file", lagging_zone_file),
            patch("suite_cloud.stalwart.directory.time.sleep") as sleep,
        ):
            domain = self.make_domain()

        sleep.assert_called_once()
        self.assertEqual(
            [r.host for r in domain.authentication_records if r.category == "DKIM"],
            ["v1-rsa-20260101._domainkey"],
        )
        self.assertIn("_domainkey", domain.dns_zone_file)

    def test_ed25519_signing_is_opt_in_and_applies_to_domains_added_afterwards(self) -> None:
        before = self.make_domain()
        configure_settings(sign_with_ed25519=1)
        self.addCleanup(configure_settings, sign_with_ed25519=0)
        after = self.make_domain("acme.net")

        live = self.fake.find("Domain", name="acme.net")
        self.assertEqual(
            live["dkimManagement"]["algorithms"], {"Dkim1Ed25519Sha256": True, "Dkim1RsaSha256": True}
        )
        selectors = sorted(r.host for r in after.authentication_records if r.category == "DKIM")
        self.assertEqual(selectors, ["v1-ed25519-20260101._domainkey", "v1-rsa-20260101._domainkey"])

        # The earlier domain keeps the keys it was created with; a save does not push algorithms.
        before.description = "renamed"
        before.save()
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(live["dkimManagement"]["algorithms"], {"Dkim1RsaSha256": True})

    def test_domain_delete_blocked_by_aliases_on_it(self) -> None:
        self.make_domain()
        second = self.make_domain("acme.net")
        self.make_account("a@acme.com", aliases=[{"alias_email": "a@acme.net"}])
        self.assertRaisesRegex(frappe.ValidationError, "aliases on acme.net", second.delete)

    def test_domain_delete_requires_empty_directory_and_removes_dkim(self) -> None:
        domain = self.make_domain()
        self.make_account("a@acme.com")
        self.assertRaisesRegex(frappe.ValidationError, "Delete every", domain.delete)

        frappe.delete_doc("Mail Account", "a@acme.com")
        domain.delete()
        self.assertIsNone(self.fake.find("Domain", name="acme.com"))
        self.assertEqual(self.fake.all("DkimSignature"), [])


class TestMailAccount(TenancyTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.domain = self.make_domain()
        self.group = frappe.get_doc(
            {"doctype": "Mail Group", "email": "sales@acme.com", "site": self.site.name}
        ).insert()

    def test_account_wire_payload(self) -> None:
        account = self.make_account(
            "Alice@Acme.com",
            display_name="Alice",
            aliases=[{"alias_email": "ally@acme.com"}],
            groups=[{"group": "sales@acme.com"}],
        )

        self.assertEqual(account.name, "alice@acme.com")
        self.assertEqual(account.disk_quota_gb, 1)
        live = self.fake.get("Account", account.stalwart_id)
        self.assertEqual(live["@type"], "User")
        self.assertEqual(live["credentials"]["0"]["secret"], "secret-pw")
        self.assertEqual(live["memberGroupIds"], {self.group.stalwart_id: True})
        self.assertEqual(live["aliases"]["0"]["name"], "ally")
        self.assertEqual(live["aliases"]["0"]["domainId"], self.domain.stalwart_id)
        self.assertEqual(live["quotas"], {"maxDiskQuota": 1024**3})
        self.assertEqual(live["description"], "Alice")
        self.assertEqual(live["roles"], {"@type": "User"})
        self.assertEqual(self.group.to_api()["members"], ["alice@acme.com"])

    def test_account_updates_are_patched(self) -> None:
        account = self.make_account("bob@acme.com")
        account.display_name = "Bob"
        account.disk_quota_gb = 0
        account.aliases = []
        account.append("aliases", {"alias_email": "robert@acme.com", "enabled": 0})
        account.append("groups", {"group": "sales@acme.com"})
        account.save()

        live = self.fake.get("Account", account.stalwart_id)
        self.assertEqual(live["description"], "Bob")
        self.assertEqual(live["quotas"], {})
        self.assertEqual(live["aliases"]["0"]["enabled"], False)
        self.assertEqual(live["memberGroupIds"], {self.group.stalwart_id: True})

        account.set_password("another-pw")
        self.assertEqual(
            self.fake.get("Account", account.stalwart_id)["credentials"]["0"]["secret"], "another-pw"
        )

        account.set_enabled(False)
        live = self.fake.get("Account", account.stalwart_id)
        role = self.fake.find("Role", description=DISABLED_ROLE_DESCRIPTION)
        self.assertEqual(live["roles"], {"@type": "Custom", "roleIds": {role["id"]: True}})
        account.set_enabled(True)
        self.assertEqual(self.fake.get("Account", account.stalwart_id)["roles"], {"@type": "User"})

        secret = account.create_app_password("Suite")
        self.assertTrue(secret.startswith("apppassword-"))

        account.delete()
        self.assertIsNone(self.fake.get("Account", account.stalwart_id))

    def test_address_uniqueness_and_ownership(self) -> None:
        self.make_account("carol@acme.com", aliases=[{"alias_email": "cc@acme.com"}])
        self.assertRaises(frappe.DuplicateEntryError, self.make_account, "cc@acme.com")
        self.assertRaises(
            frappe.DuplicateEntryError,
            self.make_account,
            "d@acme.com",
            aliases=[{"alias_email": "carol@acme.com"}],
        )
        self.assertRaisesRegex(
            frappe.ValidationError,
            "already the primary",
            self.make_account,
            "e@acme.com",
            aliases=[{"alias_email": "e@acme.com"}],
        )

        other = make_site(self.cluster, "other.frappe.test")
        self.assertRaises(frappe.DoesNotExistError, self.make_account, "x@acme.com", site=other.name)
        self.assertRaisesRegex(
            frappe.ValidationError,
            "does not belong",
            self.make_account,
            "f@acme.com",
            aliases=[{"alias_email": "f@nowhere.com"}],
        )

        self.site.db_set("max_accounts", 1)
        frappe.clear_document_cache("Suite Site", self.site.name)
        self.assertRaisesRegex(frappe.ValidationError, "limit", self.make_account, "g@acme.com")

    def test_group_delete_clears_membership(self) -> None:
        account = self.make_account("dave@acme.com", groups=[{"group": "sales@acme.com"}])
        self.group.delete()
        self.assertFalse(frappe.db.exists("Mail Group Member", {"group": "sales@acme.com"}))
        self.assertIsNone(self.fake.get("Account", self.group.stalwart_id))
        self.assertEqual(self.fake.get("Account", account.stalwart_id)["memberGroupIds"], {})


class TestMailingList(TenancyTestCase):
    def test_mailing_list_recipients_and_aliases(self) -> None:
        self.make_domain()
        mailing_list = frappe.get_doc(
            {
                "doctype": "Mailing List",
                "email": "all@acme.com",
                "site": self.site.name,
                "recipients": [{"email": "a@acme.com"}, {"email": "ext@example.org"}],
                "aliases": [{"alias_email": "everyone@acme.com"}],
            }
        ).insert()

        live = self.fake.get("MailingList", mailing_list.stalwart_id)
        self.assertEqual(live["recipients"], {"a@acme.com": True, "ext@example.org": True})
        self.assertEqual(live["aliases"]["0"]["name"], "everyone")

        mailing_list.recipients = []
        mailing_list.append("recipients", {"email": "b@acme.com"})
        mailing_list.save()
        self.assertEqual(
            self.fake.get("MailingList", mailing_list.stalwart_id)["recipients"], {"b@acme.com": True}
        )

        self.assertRaises(frappe.DuplicateEntryError, self.make_account, "everyone@acme.com")
        mailing_list.delete()
        self.assertIsNone(self.fake.get("MailingList", mailing_list.stalwart_id))
