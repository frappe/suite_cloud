from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import dns.exception
import dns.resolver
from frappe.tests import UnitTestCase

from suite_cloud.dns import resolver

CLOUDFLARE, GOOGLE_2, GOOGLE_1, QUAD9 = resolver.NAMESERVERS
IP = "165.232.179.70"


class TestDnsResolver(UnitTestCase):
    def test_a_stale_does_not_exist_is_outvoted(self) -> None:
        # Right after a record is created, some resolver caches still hold "does not exist".
        answers = {CLOUDFLARE: dns.resolver.NXDOMAIN(), GOOGLE_2: [IP]}
        with fake_resolvers(answers) as asked:
            self.assertTrue(resolver.verify_dns_record("n1.c1.example.test", "A", IP))
        self.assertEqual(asked, [CLOUDFLARE, GOOGLE_2])  # stops at the first resolver that agrees

    def test_a_record_missing_everywhere_fails(self) -> None:
        with fake_resolvers(everywhere(dns.resolver.NXDOMAIN())):
            self.assertFalse(resolver.verify_dns_record("n1.c1.example.test", "A", IP))

        answers = {**everywhere(["10.0.0.1"]), QUAD9: dns.exception.Timeout()}
        with fake_resolvers(answers):
            self.assertFalse(resolver.verify_dns_record("n1.c1.example.test", "A", IP))

    def test_no_resolver_answering_says_nothing(self) -> None:
        with fake_resolvers(everywhere(dns.exception.Timeout())):
            self.assertIsNone(resolver.verify_dns_record("n1.c1.example.test", "A", IP))
            self.assertIsNone(resolver.verify_ptr_record(IP, "n1.c1.example.test"))

    def test_mx_compares_the_exchange(self) -> None:
        mx = SimpleNamespace(exchange=SimpleNamespace(to_text=lambda: "Mail.C1.example.test."))
        with fake_resolvers({CLOUDFLARE: [mx]}):
            self.assertTrue(resolver.verify_dns_record("c1.example.test", "MX", "mail.c1.example.test"))

    def test_ptr_is_verified_by_any_resolver(self) -> None:
        answers = {CLOUDFLARE: dns.resolver.NoAnswer(), GOOGLE_2: ["n1.c1.example.test."]}
        with fake_resolvers(answers):
            self.assertTrue(resolver.verify_ptr_record(IP, "N1.c1.example.test"))
        with fake_resolvers(everywhere(["other.example.test."])):
            self.assertFalse(resolver.verify_ptr_record(IP, "n1.c1.example.test"))
        self.assertIsNone(resolver.verify_ptr_record("not-an-ip", "n1.c1.example.test"))


def everywhere(answer) -> dict:
    return {nameserver: answer for nameserver in resolver.NAMESERVERS}


@contextmanager
def fake_resolvers(answers: dict):
    """Each nameserver answers from ``answers`` (values, or an exception to raise); others time out.

    Yields the nameservers asked, in order.
    """

    asked: list[str] = []

    def resolve(self, name, rdtype):
        nameserver = self.nameservers[0]
        asked.append(nameserver)
        answer = answers.get(nameserver, dns.exception.Timeout())
        if isinstance(answer, Exception):
            raise answer
        return [SimpleNamespace(to_text=lambda v=v: v) if isinstance(v, str) else v for v in answer]

    with patch.object(dns.resolver.Resolver, "resolve", resolve):
        yield asked
