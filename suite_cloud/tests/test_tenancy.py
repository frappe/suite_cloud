from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cloud_mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.cloud_mail.stalwart import forget_sessions
from suite_cloud.cloud_mail.tenancy.addresses import get_site_domain
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
        # Verified unless a test says otherwise: only a live domain takes accounts, groups and lists.
        fields.setdefault("is_verified", 1)
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
        domain = self.make_domain(is_verified=0)

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

    def test_verified_set_by_hand_goes_live(self) -> None:
        domain = self.make_domain(is_verified=0)
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

        domain.is_verified = 1
        domain.save()
        self.assertTrue(self.fake.find("Domain", name="acme.com")["isEnabled"])

        domain.is_verified = 0
        domain.save()
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

    def test_disabling_clears_verification(self) -> None:
        domain = self.make_domain(is_verified=0)
        domain.is_verified = 1
        domain.save()
        self.assertTrue(self.fake.find("Domain", name="acme.com")["isEnabled"])

        domain.enabled = 0
        domain.save()
        self.assertFalse(domain.is_verified)
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

        # Enabling brings nothing back on its own: the records have to be verified again.
        domain.enabled = 1
        domain.save()
        self.assertFalse(domain.is_verified)
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

    def test_only_a_live_domain_takes_accounts_groups_and_lists(self) -> None:
        domain = self.make_domain(is_verified=0)
        self.assertRaisesRegex(frappe.ValidationError, "not active", self.make_account, "a@acme.com")
        group = frappe.get_doc({"doctype": "Mail Group", "email": "g@acme.com", "site": self.site.name})
        self.assertRaisesRegex(frappe.ValidationError, "not active", group.insert)
        mailing_list = frappe.get_doc(
            {"doctype": "Mailing List", "email": "l@acme.com", "site": self.site.name}
        )
        self.assertRaisesRegex(frappe.ValidationError, "not active", mailing_list.insert)

        domain.is_verified = 1
        domain.save()
        account = self.make_account("a@acme.com")
        # An existing account keeps working on a domain that later goes dark.
        domain.enabled = 0
        domain.save()
        account.display_name = "Still here"
        account.save()
        self.assertRaisesRegex(frappe.ValidationError, "not active", self.make_account, "b@acme.com")

    def test_domain_goes_live_only_once_verified(self) -> None:
        domain = self.make_domain(is_verified=0)
        self.assertFalse(self.fake.find("Domain", name="acme.com")["isEnabled"])

        for row in domain.dns_rows():
            row.is_verified = 1
        domain.save_records()
        # Simulate a verification pass where every record already resolves.
        with patch(
            "suite_cloud.cloud_mail.doctype.mail_domain.mail_domain.verify_dns_record", return_value=True
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
            "suite_cloud.cloud_mail.doctype.mail_domain.mail_domain.verify_dns_record", side_effect=resolve
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
            patch("suite_cloud.cloud_mail.stalwart.directory.time.sleep") as sleep,
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
        self.assertEqual(account.disk_quota_gb, self.site.default_disk_quota_gb)
        live = self.fake.get("Account", account.stalwart_id)
        self.assertEqual(live["@type"], "User")
        self.assertEqual(live["credentials"]["0"]["secret"], "secret-pw")
        self.assertEqual(live["memberGroupIds"], {self.group.stalwart_id: True})
        self.assertEqual(live["aliases"]["0"]["name"], "ally")
        self.assertEqual(live["aliases"]["0"]["domainId"], self.domain.stalwart_id)
        self.assertEqual(live["quotas"], {"maxDiskQuota": int(self.site.default_disk_quota_gb * 1024**3)})
        self.assertEqual(live["description"], "Alice")
        self.assertEqual(live["roles"], {"@type": "User"})
        self.assertEqual(self.group.to_api()["members"], ["alice@acme.com"])

    def test_account_updates_are_patched(self) -> None:
        account = self.make_account("bob@acme.com")
        account.display_name = "Bob"
        account.description = "internal note"
        account.disk_quota_gb = 2
        account.aliases = []
        account.append("aliases", {"alias_email": "robert@acme.com", "enabled": 0})
        account.append("groups", {"group": "sales@acme.com"})
        account.save()

        live = self.fake.get("Account", account.stalwart_id)
        self.assertEqual(live["description"], "Bob")
        self.assertEqual(live["quotas"], {"maxDiskQuota": 2 * 1024**3})
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

        # An app password is minted on creation and stored encrypted; the API key only on demand.
        first = account.get_password("app_password")
        self.assertTrue(first.startswith("apppassword-"))
        self.assertIsNone(account.get_password("api_key", raise_exception=False))
        second = account.rotate_app_password()
        self.assertNotEqual(first, second)
        self.assertEqual(frappe.get_doc("Mail Account", account.name).show_app_password(), second)
        stored = self.fake.objects[f"AppPassword:{account.stalwart_id}"]
        self.assertEqual(
            [c["description"] for c in stored.values() if c["description"] == "Suite Cloud"], ["Suite Cloud"]
        )

        key = account.rotate_api_key()
        self.assertTrue(key.startswith("apikey-"))
        self.assertIn(key, self.fake.tokens)
        self.assertNotEqual(account.rotate_api_key(), key)
        keys = self.fake.objects[f"ApiKey:{account.stalwart_id}"]
        self.assertEqual(len(keys), 1)

        # A blank reset generates a password; a typed one is pushed as given; none is stored.
        generated = account.reset_password()
        self.assertGreaterEqual(len(generated), 20)
        self.assertEqual(
            self.fake.get("Account", account.stalwart_id)["credentials"]["0"]["secret"], generated
        )
        account.reload()
        account.new_password = "typed-pw-123"
        account.save()
        self.assertEqual(
            self.fake.get("Account", account.stalwart_id)["credentials"]["0"]["secret"], "typed-pw-123"
        )
        self.assertIsNone(account.get_password("new_password", raise_exception=False))

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

    def test_disk_quotas_are_positive_and_within_the_site_total(self) -> None:
        self.assertRaisesRegex(
            frappe.ValidationError, "above 0", self.make_account, "z@acme.com", disk_quota_gb=0
        )

        site = frappe.get_doc("Suite Site", self.site.name)
        site.default_disk_quota_gb = 0
        self.assertRaisesRegex(frappe.ValidationError, "above 0", site.save)
        site.reload()
        site.max_disk_gb = 8
        site.default_disk_quota_gb = 5
        site.save()

        # The fixture group took the site's default quota when it was created; give it 1 GB so
        # it leaves room and still counts in the total.
        group = frappe.get_doc("Mail Group", self.group.name)
        group.disk_quota_gb = 1
        group.save()
        self.assertEqual(self.fake.get("Account", group.stalwart_id)["quotas"], {"maxDiskQuota": 1024**3})
        first = self.make_account("q1@acme.com")  # 5 + 1 of 8
        self.assertRaisesRegex(frappe.ValidationError, "2.0 GB of its 8", self.make_account, "q2@acme.com")
        second = self.make_account("q2@acme.com", disk_quota_gb=2)  # exactly full
        first.reload()
        first.disk_quota_gb = 6
        self.assertRaisesRegex(frappe.ValidationError, "total disk quota", first.save)
        first.reload()
        first.disk_quota_gb = 4  # shrinking is always fine
        first.save()
        usage = frappe.get_doc("Suite Site", self.site.name).to_api()["usage"]
        self.assertEqual(usage["allocated_disk_gb"], 7)  # 4 + 2 accounts, 1 group
        group.disk_quota_gb = 0
        self.assertRaisesRegex(frappe.ValidationError, "above 0", group.save)
        second.delete()

    def test_group_and_mailing_list_limits(self) -> None:
        self.site.db_set({"max_groups": 1, "max_mailing_lists": 0})  # one group exists; lists unlimited
        frappe.clear_document_cache("Suite Site", self.site.name)
        group = frappe.get_doc({"doctype": "Mail Group", "email": "ops@acme.com", "site": self.site.name})
        self.assertRaisesRegex(frappe.ValidationError, "limit of 1 groups", group.insert)
        frappe.get_doc({"doctype": "Mailing List", "email": "news@acme.com", "site": self.site.name}).insert()
        self.site.db_set("max_mailing_lists", 1)
        frappe.clear_document_cache("Suite Site", self.site.name)
        more = frappe.get_doc({"doctype": "Mailing List", "email": "more@acme.com", "site": self.site.name})
        self.assertRaisesRegex(frappe.ValidationError, "mailing lists", more.insert)
        usage = frappe.get_doc("Suite Site", self.site.name).to_api()["usage"]
        self.assertEqual((usage["groups"], usage["mailing_lists"]), (1, 1))

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
                "aliases": [{"alias_email": "everyone@acme.com"}],
            }
        ).insert()
        live = self.fake.get("MailingList", mailing_list.stalwart_id)
        self.assertEqual(live["recipients"], {})
        self.assertEqual(live["aliases"]["0"]["name"], "everyone")

        # Recipients are standalone documents; changes reach the cluster as key patches.
        added = mailing_list.add_recipients(["A@acme.com", "ext@example.org", "a@acme.com"])
        self.assertEqual(added, ["a@acme.com", "ext@example.org"])
        self.assertEqual(
            self.fake.get("MailingList", mailing_list.stalwart_id)["recipients"],
            {"a@acme.com": True, "ext@example.org": True},
        )
        self.assertEqual(mailing_list.add_recipients(["ext@example.org"]), [])  # already there
        self.assertEqual(mailing_list.recipient_count(), 2)

        row = frappe.get_doc(
            "Mailing List Recipient", {"mailing_list": mailing_list.name, "email": "a@acme.com"}
        )
        self.assertEqual(row.site, self.site.name)
        row.enabled = 0
        row.save()
        self.assertEqual(
            self.fake.get("MailingList", mailing_list.stalwart_id)["recipients"], {"ext@example.org": True}
        )
        self.assertRaises(
            frappe.DuplicateEntryError,
            frappe.get_doc(
                {
                    "doctype": "Mailing List Recipient",
                    "mailing_list": mailing_list.name,
                    "email": "a@acme.com",
                }
            ).insert,
        )
        self.assertRaisesRegex(
            frappe.ValidationError,
            "own recipient",
            frappe.get_doc(
                {
                    "doctype": "Mailing List Recipient",
                    "mailing_list": mailing_list.name,
                    "email": "all@acme.com",
                }
            ).insert,
        )

        mailing_list.set_recipients(["b@acme.com", "ext@example.org"])
        self.assertEqual(mailing_list.recipient_emails(), ["b@acme.com", "ext@example.org"])
        self.assertEqual(
            self.fake.get("MailingList", mailing_list.stalwart_id)["recipients"],
            {"b@acme.com": True, "ext@example.org": True},
        )

        self.assertRaises(frappe.DuplicateEntryError, self.make_account, "everyone@acme.com")
        mailing_list.delete()
        self.assertIsNone(self.fake.get("MailingList", mailing_list.stalwart_id))
        self.assertFalse(frappe.db.exists("Mailing List Recipient", {"mailing_list": "all@acme.com"}))
