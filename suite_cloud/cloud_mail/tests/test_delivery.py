"""Sends mail through a real Stalwart, to see where the routes Suite Cloud writes take it."""

import os
import unittest

from frappe.tests import IntegrationTestCase

from suite_cloud.cloud_mail.cluster import egress
from suite_cloud.cloud_mail.stalwart.directory import Account, Domain, EmailAlias, Group
from suite_cloud.cloud_mail.tests.fixtures import configure_settings
from suite_cloud.cloud_mail.tests.real_stalwart import RealStalwart, pinned_stalwart

SPLIT = "split.example.org"  # relays: some of its mailboxes live on another server
WHOLE = "whole.example.org"
SENDER = "sender@outside.example.net"
NOT_HELD = f"nobody@{SPLIT}"


class TestSplitDelivery(IntegrationTestCase):
    # The fake keeps the route it is sent but cannot follow it, so the other tests pin a rule's
    # wording and these its meaning: a rule that is well formed and wrong only shows in where the
    # mail goes.

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        binary, missing = pinned_stalwart()
        if not binary:
            if os.environ.get("CI"):
                raise AssertionError(f"CI must provide the pinned Stalwart for this check: {missing}")
            raise unittest.SkipTest(missing)

        configure_settings()
        cls.server = RealStalwart(binary)
        cls.addClassCleanup(cls.server.stop)
        cls.server.start()
        directory = cls.server.management
        split = directory.domains.create_id(Domain(name=SPLIT, dkim_algorithms=(), allow_relaying=True))
        whole = directory.domains.create_id(Domain(name=WHOLE, dkim_algorithms=()))
        directory.accounts.create_id(
            Account(name="alice", domain_id=split, aliases=[EmailAlias("sales", split)])
        )
        directory.groups.create_id(Group(name="team", domain_id=split))
        directory.accounts.create_id(Account(name="bob", domain_id=whole))

    def setUp(self) -> None:
        self.server.sink.clear()

    def test_a_relaying_domain_keeps_what_it_holds_and_lets_the_rest_out(self) -> None:
        self.server.route(egress.local_rules([SPLIT]))
        held = [f"alice@{SPLIT}", f"sales@{SPLIT}", f"team@{SPLIT}", f"alice+tag@{SPLIT}", f"bob@{WHOLE}"]
        for recipient in [*held, NOT_HELD]:
            self.server.send(SENDER, recipient)
        self.server.settle()

        # Only the address no account, alias or group answers to left the server; a held one that
        # had been sent away would be in the first set, and one that found no mailbox in the second.
        self.assertEqual(self.server.sink.relayed(), {NOT_HELD})
        self.assertEqual(self.server.sink.bounced(), set())

    def test_the_plain_local_rule_bounces_what_a_relaying_domain_does_not_hold(self) -> None:
        # What the route did before: Stalwart takes the message because the domain relays, then
        # finds no mailbox for it. Also the proof that a bounce is seen when there is one.
        self.server.route(egress.local_rules([]))
        self.server.send(SENDER, NOT_HELD)
        self.server.settle()

        self.assertEqual(self.server.sink.relayed(), set())
        self.assertEqual(self.server.sink.bounced(), {NOT_HELD})

    def test_a_domain_that_does_not_relay_keeps_every_address(self) -> None:
        # An address nobody holds is only ever queued as the recipient of a bounce notice. Under a
        # domain that does not relay it must fail here: sent out, it would come straight back
        # through that domain's MX.
        self.server.route(egress.local_rules([SPLIT]))
        self.server.send(f"nobody@{WHOLE}", f"reject-me@{SPLIT}")
        self.server.settle()

        self.assertEqual(self.server.sink.refused, [f"reject-me@{SPLIT}"])  # relayed, and bounced back
        self.assertEqual(self.server.sink.messages, [])  # the notice to nobody@whole stayed
