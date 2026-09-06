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
    verified_ownership,
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
        self._ownership = verified_ownership()
        self._ownership.start()
        self.addCleanup(self._ownership.stop)
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
        self.assertEqual(live["dkimManagement"]["@type"], "Automatic")
        self.assertEqual(live["dnsManagement"], {"@type": "Manual"})
        self.assertEqual(live["reportAddressUri"], "mailto:postmaster@acme.com")

        categories = [(r.category, r.host, r.is_mandatory) for r in domain.dns_records]
        self.assertIn(("Receiving", "@", 1), categories)
        self.assertIn(("Sending", "@", 1), categories)
        self.assertIn(("DMARC", "_dmarc", 1), categories)
        self.assertEqual(sum(1 for c in categories if c[0] == "DKIM"), 2)
        self.assertNotIn("MTA-STS", [c[0] for c in categories])
        spf = next(r for r in domain.dns_records if r.category == "Sending")
        self.assertEqual(spf.value, f"v=spf1 include:spf.{self.cluster.default_domain} -all")
        mx = next(r for r in domain.dns_records if r.category == "Receiving")
        self.assertEqual((mx.value, mx.priority, mx.fqdn), (self.cluster.hostname, 10, "acme.com"))
        self.assertFalse(domain.is_verified)

    def test_domain_updates_push_and_refresh_keeps_verification(self) -> None:
        domain = self.make_domain()
        next(r for r in domain.dns_records if r.category == "Receiving").is_verified = 1
        domain.save_records()

        domain.description = "Main"
        domain.catch_all_address = "Catch@Acme.com"
        domain.publish_client_discovery_records = 1
        domain.save()
        live = self.fake.find("Domain", name="acme.com")
        self.assertEqual(live["description"], "Main")
        self.assertEqual(live["catchAllAddress"], "catch@acme.com")

        domain.refresh_dns_records()
        self.assertIn("MTA-STS", [r.category for r in domain.dns_records])
        self.assertEqual([r.is_verified for r in domain.dns_records if r.category == "Receiving"], [1])

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

        for row in domain.dns_records:
            row.is_verified = 1
        domain.save_records()
        # Simulate a verification pass where every record already resolves.
        from unittest.mock import patch

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
        from unittest.mock import patch

        domain = self.make_domain()
        for row in domain.dns_records:
            row.is_verified = 1
        domain.save_records()

        def resolve(fqdn, type, value):
            return None if "_domainkey" in fqdn else True  # DKIM lookups time out

        with patch(
            "suite_cloud.suite_cloud.doctype.mail_domain.mail_domain.verify_dns_record", side_effect=resolve
        ):
            result = domain.verify_dns_records()
        self.assertTrue(result["is_verified"])  # DKIM rows kept their verified state
        self.assertEqual(result["inconclusive"], 2)

        # One verified DKIM selector is enough after a rotation adds an unpublished one.
        domain.append(
            "dns_records",
            {
                "category": "DKIM",
                "record_type": "TXT",
                "host": "v2._domainkey",
                "value": "v=DKIM1",
                "is_mandatory": 1,
            },
        )
        self.assertTrue(domain.compute_is_verified())
        for row in domain.dns_records:
            if row.category == "DKIM":
                row.is_verified = 0
        self.assertFalse(domain.compute_is_verified())

    def test_reserved_names_cover_every_cluster_zone(self) -> None:
        self.assertRaisesRegex(
            frappe.ValidationError, "reserved", self.make_domain, "mail.other.example.test"
        )

    def test_domain_name_collision_is_neutral(self) -> None:
        self.make_domain()
        other = make_site(self.cluster, "other.frappe.test")
        doc = frappe.get_doc({"doctype": "Mail Domain", "domain_name": "acme.com", "site": other.name})
        self.assertRaisesRegex(frappe.DuplicateEntryError, "not available", doc.insert)

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
