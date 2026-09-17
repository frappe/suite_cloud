from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.cloud_mail.cluster.plan import DISABLED_ROLE_DESCRIPTION
from suite_cloud.cloud_mail.stalwart import forget_sessions
from suite_cloud.cloud_mail.tenancy.addresses import get_site_domain
from suite_cloud.cloud_mail.tests.fake_stalwart import FakeError, FakeStalwart
from suite_cloud.cloud_mail.tests.fixtures import (
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
        # Only the fixture sites: the dev site holds real accounts, and deleting those even
        # inside the rolled-back transaction serialises each one, which asks the live cluster.
        for doctype in ("Mail Account", "Mail Group", "Mailing List", "Mail Domain"):
            for name in frappe.get_all(doctype, {"site": ["like", "%.frappe.test"]}, pluck="name"):
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

    def make_account(self, email: str, password: str = "secret-pw", disk_quota_gb=None, **fields):
        account = frappe.get_doc(
            {"doctype": "Mail Account", "email": email, "site": self.site.name, **fields}
        )
        if disk_quota_gb is not None:
            account.set_disk_quota_gb(disk_quota_gb)
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
        # Stalwart generates and holds the key, under a fixed selector, and never rotates it.
        self.assertEqual(live["dkimManagement"]["algorithms"], {"Dkim1RsaSha256": True})
        self.assertEqual(live["dkimManagement"]["selectorTemplate"], "frappemail-{algorithm}")
        self.assertGreater(live["dkimManagement"]["rotateAfter"], 50 * 365 * 24 * 60 * 60 * 1000)
        self.assertEqual(live["dnsManagement"], {"@type": "Manual"})
        self.assertEqual(live["reportAddressUri"], "mailto:postmaster@acme.com")

        # Rows land in the table of their group; authentication rows are the mandatory ones.
        auth = [(r.category, r.host, r.is_mandatory) for r in domain.authentication_records]
        self.assertEqual(
            auth, [("SPF", "@", 1), ("DKIM", "frappemail-rsa._domainkey", 1), ("DMARC", "_dmarc", 1)]
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

    def test_adoption_records_what_the_cluster_holds_without_pushing(self) -> None:
        from suite_cloud.cloud_mail.tenancy.adopt import adopt_directory

        gb = 1024**3
        domain_id = self.fake._add(
            "Domain",
            {
                "name": "legacy.com",
                "isEnabled": True,
                "description": "Legacy",
                "subAddressing": {"@type": "Disabled"},
                "allowRelaying": True,
                "catchAllAddress": "inbox@legacy.com",
            },
        )
        self.fake._add(
            "DkimSignature", {"domainId": domain_id, "selector": "v1-rsa-20260101", "stage": "active"}
        )
        group_id = self.fake._add(
            "Account",
            {
                "@type": "Group",
                "name": "team",
                "domainId": domain_id,
                "description": "Team",
                "quotas": {"maxDiskQuota": 2 * gb},
                "aliases": {"0": {"name": "crew", "domainId": domain_id, "enabled": True}},
            },
        )
        disabled_role = self.fake.find("Role", description=DISABLED_ROLE_DESCRIPTION)["id"]
        self.fake._add(
            "Account",
            {
                "@type": "User",
                "name": "alice",
                "domainId": domain_id,
                "description": "Alice",
                "locale": "de-DE",
                "timeZone": "Europe/Berlin",
                "quotas": {"maxDiskQuota": gb, "maxEmails": 500},
                "aliases": {
                    "0": {"name": "ally", "domainId": domain_id, "enabled": False, "description": "old"}
                },
                "memberGroupIds": {group_id: True},
                "roles": {"@type": "User"},
            },
        )
        self.fake._add(
            "Account",
            {
                "@type": "User",
                "name": "bob",
                "domainId": domain_id,
                "roles": {"@type": "Custom", "roleIds": {disabled_role: True}},
            },
        )
        self.fake._add("Account", {"@type": "User", "name": "admin", "roles": {"@type": "Admin"}})
        self.fake._add(
            "MailingList",
            {
                "name": "all",
                "domainId": domain_id,
                "recipients": {"alice@legacy.com": True, "ext@example.org": True},
            },
        )

        calls = len(self.fake.calls)
        report = adopt_directory(self.site.name)

        self.assertEqual(
            report["adopted"],
            {
                "Mail Domain": ["legacy.com"],
                "Mail Group": ["team@legacy.com"],
                "Mail Account": ["alice@legacy.com", "bob@legacy.com"],
                "Mailing List": ["all@legacy.com"],
            },
        )
        self.assertEqual(report["skipped"], {})
        # Reads only: the cluster already has everything.
        self.assertEqual([c[0] for c in self.fake.calls[calls:] if c[0].endswith("/set")], [])

        domain = frappe.get_doc("Mail Domain", "legacy.com")
        self.assertEqual(
            (
                domain.stalwart_id,
                domain.enabled,
                domain.is_verified,
                domain.sub_addressing,
                domain.allow_relaying,
            ),
            (domain_id, 1, 1, 0, 1),
        )
        self.assertEqual(domain.catch_all_address, "inbox@legacy.com")
        self.assertTrue(domain.authentication_records)  # the published zone was read in
        group = frappe.get_doc("Mail Group", "team@legacy.com")
        self.assertEqual((group.stalwart_id, group.allotted_disk_gb()), (group_id, 2))
        self.assertEqual([a.alias_email for a in group.aliases], ["crew@legacy.com"])
        alice = frappe.get_doc("Mail Account", "alice@legacy.com")
        self.assertEqual(
            (alice.display_name, alice.locale, alice.time_zone, alice.enabled),
            ("Alice", "de-DE", "Europe/Berlin", 1),
        )
        self.assertEqual(alice.quota_map(), {"maxDiskQuota": gb, "maxEmails": 500})
        self.assertEqual(
            [(a.alias_email, a.enabled, a.description) for a in alice.aliases],
            [("ally@legacy.com", 0, "old")],
        )
        self.assertEqual([g.group for g in alice.groups], ["team@legacy.com"])
        bob = frappe.get_doc("Mail Account", "bob@legacy.com")
        self.assertEqual((bob.enabled, bob.allotted_disk_gb()), (0, self.site.default_disk_quota_gb))
        self.assertEqual(
            frappe.get_doc("Mailing List", "all@legacy.com").recipient_emails(),
            ["alice@legacy.com", "ext@example.org"],
        )
        self.assertFalse(frappe.db.exists("Mail Account", "admin@legacy.com"))

        # A second run finds everything already recorded.
        again = adopt_directory(self.site.name)
        self.assertEqual(again["adopted"], {})
        self.assertEqual(
            {dt: [r[0] for r in rows] for dt, rows in again["skipped"].items()},
            {
                "Mail Domain": ["legacy.com"],
                "Mail Group": ["team@legacy.com"],
                "Mail Account": ["alice@legacy.com", "bob@legacy.com"],
                "Mailing List": ["all@legacy.com"],
            },
        )
        self.assertTrue(
            all(reason == "already exists" for rows in again["skipped"].values() for _, reason in rows)
        )

        # A cluster shared with another site cannot be adopted: nothing says whose objects they are.
        make_site(self.cluster, "other.frappe.test")
        self.assertRaisesRegex(frappe.ValidationError, "also serves", adopt_directory, self.site.name)

    def test_mail_never_routes_into_another_sites_domain(self) -> None:
        domain = self.make_domain()
        other = make_site(self.cluster, "other.frappe.test")
        theirs = frappe.get_doc(
            {"doctype": "Mail Domain", "domain_name": "other.com", "site": other.name, "is_verified": 1}
        )
        theirs.insert()

        mailing_list = frappe.get_doc(
            {"doctype": "Mailing List", "email": "all@acme.com", "site": self.site.name}
        )
        mailing_list.insert()
        # External addresses and the site's own are fine; an address under another site's domain is not.
        self.assertEqual(mailing_list.add_recipients(["ext@example.org"]), ["ext@example.org"])
        self.assertRaisesRegex(
            frappe.DoesNotExistError,
            "not available",
            mailing_list.add_recipients,
            ["ceo@other.com", "x@example.org"],
        )
        self.assertEqual(mailing_list.recipient_emails(), ["ext@example.org"])
        row = frappe.get_doc(
            {"doctype": "Mailing List Recipient", "mailing_list": mailing_list.name, "email": "hr@other.com"}
        )
        self.assertRaisesRegex(frappe.DoesNotExistError, "not available", row.insert)

        domain.catch_all_address = "inbox@other.com"
        self.assertRaisesRegex(frappe.DoesNotExistError, "not available", domain.save)
        domain.reload()
        domain.catch_all_address = "not an address"
        self.assertRaisesRegex(frappe.ValidationError, "not a valid email", domain.save)
        domain.reload()
        domain.catch_all_address = "Inbox@Acme.com"
        domain.save()
        self.assertEqual(domain.catch_all_address, "inbox@acme.com")

    def test_hourly_refresh_only_touches_domains_that_need_it(self) -> None:
        from suite_cloud.cloud_mail.doctype.mail_domain.mail_domain import refresh_rotating_domains

        domain = self.make_domain()
        selectors = sorted(
            r.host.split("._domainkey")[0] for r in domain.authentication_records if r.category == "DKIM"
        )
        self.assertTrue(selectors)

        calls = len(self.fake.calls)
        refresh_rotating_domains()  # every key stored and active: one signatures query, no zone read
        self.assertEqual(
            [c[0] for c in self.fake.calls[calls:]], ["x:DkimSignature/query", "x:DkimSignature/get"]
        )

        # A key that was still generating at creation is missing from the stored rows.
        frappe.db.delete("Mail Domain DNS Record", {"parent": domain.name, "category": "DKIM"})
        refresh_rotating_domains()
        domain.reload()
        restored = sorted(
            r.host.split("._domainkey")[0] for r in domain.authentication_records if r.category == "DKIM"
        )
        self.assertEqual(restored, selectors)

        # A rotation in progress refreshes too.
        signature = self.fake.find("DkimSignature", domainId=domain.stalwart_id)
        signature["stage"] = "retiring"
        calls = len(self.fake.calls)
        refresh_rotating_domains()
        self.assertIn("x:Domain/get", [c[0] for c in self.fake.calls[calls:]])

    def test_domain_updates_push_and_refresh_keeps_verification(self) -> None:
        domain = self.make_domain()
        domain.routing_records[0].is_verified = 1
        domain.save_records()

        domain.description = "Main"
        domain.catch_all_address = "Catch@Acme.com"
        domain.allow_relaying = 1
        domain.publish_client_discovery_records = 1
        domain.save()
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(live["description"], "Main")
        self.assertEqual(live["catchAllAddress"], "catch@acme.com")
        self.assertTrue(live["allowRelaying"])  # split delivery for addresses that live elsewhere

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

    def test_record_actions_refuse_unsaved_pushed_edits(self) -> None:
        domain = self.make_domain()
        domain.enabled = 0  # edited on the form, not saved
        self.assertRaisesRegex(frappe.ValidationError, "Save the domain", domain.verify_dns_records)
        self.assertRaisesRegex(frappe.ValidationError, "Save the domain", domain.refresh_dns_records)
        self.assertTrue(self.fake.find("Domain", name="acme.com")["isEnabled"])

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

    def test_hourly_verification_leaves_a_skipped_domain_alone(self) -> None:
        from suite_cloud.cloud_mail.doctype.mail_domain.mail_domain import domains_due_for_verification

        # Verified by hand with no record resolved: the hourly check would take it offline again.
        by_hand = self.make_domain()
        unverified = self.make_domain("other.com", is_verified=0)
        self.assertLessEqual({by_hand.name, unverified.name}, set(domains_due_for_verification()))

        for domain in (by_hand, unverified):
            domain.skip_scheduled_verification = 1
            domain.save()
        self.assertFalse({by_hand.name, unverified.name} & set(domains_due_for_verification()))

        # The action is not the schedule: it still resolves the records of a skipped domain.
        with patch(
            "suite_cloud.cloud_mail.doctype.mail_domain.mail_domain.verify_dns_record", return_value=False
        ):
            self.assertFalse(by_hand.verify_dns_records()["is_verified"])

    def test_reserved_names_cover_every_cluster_zone(self) -> None:
        self.assertRaisesRegex(
            frappe.ValidationError, "reserved", self.make_domain, "mail.other.example.test"
        )

    def test_domain_name_collision_is_neutral(self) -> None:
        self.make_domain()
        other = make_site(self.cluster, "other.frappe.test")
        doc = frappe.get_doc({"doctype": "Mail Domain", "domain_name": "acme.com", "site": other.name})
        self.assertRaisesRegex(frappe.DuplicateEntryError, "not available", doc.insert)

    def test_replace_dkim_keys_keeps_the_selector_and_drops_verification(self) -> None:
        domain = self.make_domain()
        for row in domain.dns_rows():
            row.is_verified = 1
        domain.save_records()
        before = self.fake.find("DkimSignature", domainId=domain.stalwart_id)
        old_value = next(r.value for r in domain.authentication_records if r.category == "DKIM")

        calls = len(self.fake.calls)
        domain.replace_dkim_keys()

        # The old key is gone and a new one signs under the same selector, via manual management
        # and back: Stalwart generates nothing for a domain whose keys merely vanished.
        after = self.fake.find("DkimSignature", domainId=domain.stalwart_id)
        self.assertNotEqual(before["id"], after["id"])
        self.assertEqual((after["selector"], after["stage"]), ("frappemail-rsa", "active"))
        management = [
            a["update"][domain.stalwart_id]["dkimManagement"]["@type"]
            for name, a in self.fake.calls[calls:]
            if name == "x:Domain/set" and a.get("update")
        ]
        self.assertEqual(management, ["Manual", "Automatic"])
        domain.reload()
        dkim_row = next(r for r in domain.authentication_records if r.category == "DKIM")
        self.assertEqual(dkim_row.host, "frappemail-rsa._domainkey")
        self.assertNotEqual(dkim_row.value, old_value)
        self.assertFalse(dkim_row.is_verified)  # the owner has to publish the new value
        self.assertTrue(domain.authentication_records[0].is_verified)  # SPF keeps its state
        self.assertTrue(domain.is_verified)  # liveness only changes on a verification run

    def test_replacement_cut_short_reports_a_keyless_domain_and_reruns(self) -> None:
        from suite_cloud.cloud_mail.stalwart.errors import StalwartKeylessDomainError

        domain = self.make_domain()
        real_set = FakeStalwart._set
        refusals = []

        def refuse_automatic(fake, type, args, account_id, refs):
            wanted = [p.get("dkimManagement", {}).get("@type") for p in (args.get("update") or {}).values()]
            if type == "Domain" and "Automatic" in wanted and len(refusals) < 3:
                refusals.append(1)
                raise FakeError("serverFail", "busy")
            return real_set(fake, type, args, account_id, refs)

        # Both attempts refused: the old keys are gone, the operator is told, the domain is manual.
        with patch.object(FakeStalwart, "_set", refuse_automatic):
            self.assertRaisesRegex(
                StalwartKeylessDomainError, "run Replace DKIM Keys again", domain.replace_dkim_keys
            )
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(live["dkimManagement"], {"@type": "Manual"})
        self.assertEqual([s for s in self.fake.all("DkimSignature") if s["domainId"] == live["id"]], [])
        self.assertEqual(len(refusals), 2)

        # A second run skips the manual switch, survives one more refusal and regenerates the key.
        with patch.object(FakeStalwart, "_set", refuse_automatic):
            domain.replace_dkim_keys()
        self.assertEqual(len(refusals), 3)
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(live["dkimManagement"]["@type"], "Automatic")
        signatures = [s["selector"] for s in self.fake.all("DkimSignature") if s["domainId"] == live["id"]]
        self.assertEqual(signatures, ["frappemail-rsa"])

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
            ["frappemail-rsa._domainkey"],
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
        self.assertEqual(selectors, ["frappemail-ed25519._domainkey", "frappemail-rsa._domainkey"])

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
        self.assertEqual(account.allotted_disk_gb(), self.site.default_disk_quota_gb)
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
        account.set_disk_quota_gb(2)
        account.append("quotas", {"quota": "maxEmails", "value": 5000})
        account.aliases = []
        account.append("aliases", {"alias_email": "robert@acme.com", "enabled": 0})
        account.append("groups", {"group": "sales@acme.com"})
        account.save()

        live = self.fake.get("Account", account.stalwart_id)
        self.assertEqual(live["description"], "Bob")
        self.assertEqual(live["quotas"], {"maxDiskQuota": 2 * 1024**3, "maxEmails": 5000})
        # The whole map travels, so dropping a row lifts that limit on the cluster.
        account.quotas = [row for row in account.quotas if row.quota == "maxDiskQuota"]
        account.save()
        self.assertEqual(
            self.fake.get("Account", account.stalwart_id)["quotas"], {"maxDiskQuota": 2 * 1024**3}
        )
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
        frappe.db.after_commit.run()  # the old credential goes only once the new one is committed
        stored = self.fake.objects[f"AppPassword:{account.stalwart_id}"]
        self.assertEqual(
            [c["description"] for c in stored.values() if c["description"] == "Suite Cloud"], ["Suite Cloud"]
        )

        key = account.rotate_api_key()
        self.assertTrue(key.startswith("apikey-"))
        self.assertIn(key, self.fake.tokens)
        frappe.db.after_commit.run()
        self.assertNotEqual(account.rotate_api_key(), key)
        frappe.db.after_commit.run()
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

    def test_other_quotas_are_known_positive_and_listed_once(self) -> None:
        self.make_account("quota@acme.com")

        def save_with(rows: list[dict]):
            account = frappe.get_doc("Mail Account", "quota@acme.com")  # a refused save leaves it stale
            account.set("quotas", rows)
            account.save()

        self.assertRaisesRegex(
            frappe.ValidationError,
            "listed twice",
            save_with,
            [{"quota": "maxSieveScripts", "value": 3}, {"quota": "maxSieveScripts", "value": 4}],
        )
        self.assertRaisesRegex(
            frappe.ValidationError,
            "above 0",
            save_with,
            [{"quota": "maxDiskQuota", "value": 1024**3}, {"quota": "maxEmails", "value": 0}],
        )
        # The disk row is the one row that cannot go: without it the account has no quota.
        self.assertRaisesRegex(
            frappe.ValidationError, "Disk Quota", save_with, [{"quota": "maxEmails", "value": 5}]
        )
        self.assertRaisesRegex(
            frappe.ValidationError, "not a quota", save_with, [{"quota": "maxNope", "value": 5}]
        )

        group = frappe.get_doc("Mail Group", "sales@acme.com")
        group.append("quotas", {"quota": "maxEmails", "value": 100})
        group.save()
        self.assertEqual(self.fake.get("Account", group.stalwart_id)["quotas"]["maxEmails"], 100)

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
        group.set_disk_quota_gb(1)
        group.save()
        self.assertEqual(self.fake.get("Account", group.stalwart_id)["quotas"], {"maxDiskQuota": 1024**3})
        first = self.make_account("q1@acme.com")  # 5 + 1 of 8
        self.assertRaisesRegex(frappe.ValidationError, "2.0 GB of its 8", self.make_account, "q2@acme.com")
        second = self.make_account("q2@acme.com", disk_quota_gb=2)  # exactly full
        first.reload()
        first.set_disk_quota_gb(6)
        self.assertRaisesRegex(frappe.ValidationError, "total disk quota", first.save)
        first.reload()
        first.set_disk_quota_gb(4)  # shrinking is always fine
        first.save()
        usage = frappe.get_doc("Suite Site", self.site.name).to_api()["usage"]
        self.assertEqual(usage["allocated_disk_gb"], 7)  # 4 + 2 accounts, 1 group
        group.reload()
        group.set_disk_quota_gb(0)
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
