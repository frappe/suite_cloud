from frappe.tests import UnitTestCase

from suite_cloud.stalwart.client import StalwartClient
from suite_cloud.stalwart.connection import ConnectionInfo, JMAPConnection
from suite_cloud.stalwart.credentials import Credential
from suite_cloud.stalwart.directory import Account, Domain, EmailAlias, Group, MailingList
from suite_cloud.stalwart.errors import StalwartRejectedError, StalwartUnauthorizedError
from suite_cloud.tests.fake_stalwart import FakeStalwart


class TestStalwartClient(UnitTestCase):
    def setUp(self) -> None:
        self.fake = FakeStalwart()
        self._install = self.fake.install()
        self._install.__enter__()
        self.addCleanup(self._install.__exit__, None, None, None)
        self.client = self.admin_client()

    def admin_client(self) -> StalwartClient:
        info = ConnectionInfo(self.fake.base_url, username="admin", password="secret")
        return StalwartClient(JMAPConnection(info))

    def test_session_discovery_scopes_calls_to_the_admin_account(self) -> None:
        self.assertEqual(self.client.domains.account_id, self.fake.admin_id)

    def test_bearer_token_authenticates(self) -> None:
        self.fake.add_token("tok-1")
        client = StalwartClient(JMAPConnection(ConnectionInfo(self.fake.base_url, token="tok-1")))
        self.assertEqual(client.domains.get_all(), [])

    def test_bad_credentials_raise_unauthorized(self) -> None:
        info = ConnectionInfo(self.fake.base_url, username="admin", password="wrong")
        self.assertRaises(StalwartUnauthorizedError, JMAPConnection, info)

    def test_domain_lifecycle(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com", description="Example"))

        self.assertEqual(self.client.domains.find_by_name("example.com")["id"], domain_id)
        self.assertEqual(len(self.client.dkim_signatures.get_all_by_domain(domain_id)), 2)
        self.assertIn("v=spf1", self.client.domains.get_zone_file(domain_id))

        self.client.domains.delete(domain_id)
        self.assertIsNone(self.client.domains.find_by_name("example.com"))
        self.assertEqual(self.client.dkim_signatures.get_all_by_domain(domain_id), [])

    def test_duplicate_domain_is_rejected_with_stalwart_error_type(self) -> None:
        self.client.domains.create_id(Domain(name="dup.com"))

        with self.assertRaises(StalwartRejectedError) as ctx:
            self.client.domains.create_id(Domain(name="dup.com"))

        self.assertEqual(ctx.exception.error_type, "alreadyExists")
        self.assertEqual(ctx.exception.http_status_code, 422)

    def test_account_wire_format_and_patches(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        group_id = self.client.groups.create_id(Group(name="sales", domain_id=domain_id))
        account = self.client.accounts.create(
            Account(
                name="alice",
                domain_id=domain_id,
                password="pw",
                member_group_ids=[group_id],
                aliases=[EmailAlias("ally", domain_id)],
                disk_quota_bytes=2 * 1024**3,
            )
        )

        self.assertEqual(account["emailAddress"], "alice@example.com")
        self.assertEqual(account["credentials"], {"0": {"@type": "Password", "secret": "pw"}})
        self.assertEqual(account["memberGroupIds"], {group_id: True})
        self.assertEqual(account["quotas"], {"maxDiskQuota": 2 * 1024**3})
        self.assertEqual(account["roles"], {"@type": "User"})
        self.assertEqual(self.client.groups.get_member_ids(group_id), [account["id"]])

        self.client.accounts.set_password(account["id"], "new-pw")
        self.client.accounts.set_member_group_ids(account["id"], [])
        self.client.accounts.set_roles(account["id"], ["role-x"])
        stored = self.fake.get("Account", account["id"])
        self.assertEqual(stored["credentials"]["0"]["secret"], "new-pw")
        self.assertEqual(stored["memberGroupIds"], {})
        self.assertEqual(stored["roles"], {"@type": "Custom", "roleIds": {"role-x": True}})

    def test_group_delete_clears_membership(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        group_id = self.client.groups.create_id(Group(name="team", domain_id=domain_id))
        account_id = self.client.accounts.create_id(
            Account(name="bob", domain_id=domain_id, member_group_ids=[group_id])
        )

        self.client.groups.delete(group_id)

        self.assertEqual(self.fake.get("Account", account_id)["memberGroupIds"], {})
        self.assertIsNone(self.fake.get("Account", group_id))

    def test_mailing_list_recipients_are_a_set(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        list_id = self.client.mailing_lists.create_id(
            MailingList(name="all", domain_id=domain_id, recipients=["a@example.com"])
        )
        self.client.mailing_lists.set_recipients(list_id, ["b@example.com", "c@x.org"])

        self.assertEqual(
            self.fake.get("MailingList", list_id)["recipients"], {"b@example.com": True, "c@x.org": True}
        )

    def test_app_password_via_master_user_login(self) -> None:
        domain_id = self.client.domains.create_id(Domain(name="example.com"))
        self.client.accounts.create_id(Account(name="carol", domain_id=domain_id, password="pw"))

        info = ConnectionInfo(self.fake.base_url, username="carol@example.com%admin", password="secret")
        member = StalwartClient(JMAPConnection(info))
        credential_id, secret = member.app_passwords.create_secret(Credential(description="Suite"))

        self.assertTrue(secret.startswith("apppassword-"))
        self.assertNotEqual(member.app_passwords.account_id, self.fake.admin_id)
        self.assertIn(credential_id, self.fake.objects[f"AppPassword:{member.app_passwords.account_id}"])

    def test_api_key_returns_secret_once(self) -> None:
        _, secret = self.client.api_keys.create_secret(Credential(description="suite-cloud"))
        self.assertTrue(secret.startswith("apikey-"))
        self.assertNotIn("secret", self.client.api_keys.get_all()[0])

    def test_plan_apply_is_idempotent_and_resolves_refs(self) -> None:
        plan = [
            {
                "@type": "upsert",
                "object": "ClusterRole",
                "matchOn": ["name"],
                "value": {"full": {"name": "full", "tasks": {"@type": "EnableAll"}}},
            },
            {
                "@type": "upsert",
                "object": "Domain",
                "matchOn": ["name"],
                "value": {"default": {"name": "blr.test"}},
            },
            {"@type": "update", "object": "SystemSettings", "value": {"defaultDomainId": "#default"}},
        ]

        first = self.client.apply(plan)
        second = self.client.apply(plan)

        self.assertEqual(len(self.fake.all("ClusterRole")), 1)
        self.assertEqual(self.fake.singletons["SystemSettings"]["defaultDomainId"], first.ids["default"])
        self.assertEqual(second.unchanged, [first.ids["full"], first.ids["default"]])

        plan[0]["value"]["full"]["description"] = "changed"
        third = self.client.apply(plan)
        self.assertEqual(third.updated, [first.ids["full"], "SystemSettings/singleton"])
        self.assertEqual(self.fake.get("ClusterRole", first.ids["full"])["description"], "changed")

    def test_plan_apply_does_not_resend_secrets(self) -> None:
        plan = [
            {
                "@type": "upsert",
                "object": "MtaRoute",
                "matchOn": ["name"],
                "value": {
                    "r": {
                        "@type": "Relay",
                        "name": "egress-x",
                        "port": 2525,
                        "authSecret": {"@type": "Value", "secret": "pw"},
                    }
                },
            }
        ]
        self.client.apply(plan)
        result = self.client.apply(plan)
        self.assertEqual(result.updated, [])
        self.assertEqual(len(result.unchanged), 1)

    def test_reload_settings_runs_an_action(self) -> None:
        self.client.reload_settings()
        self.assertEqual(self.fake.all("Action")[0]["@type"], "ReloadSettings")
