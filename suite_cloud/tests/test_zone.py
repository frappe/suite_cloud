from frappe.tests import UnitTestCase

from suite_cloud.cluster.zone import build_domain_records, parse_zone_file
from suite_cloud.tests.fake_stalwart import FakeStalwart

ZONE = """
example.com. 3600 IN MX 10 mail.blr.example.test.
example.com. 3600 IN TXT "v=spf1 mx ra=postmaster -all"
v1-rsa-20260101._domainkey.example.com. 3600 IN TXT "v=DKIM1; k=rsa; " "p=MIIBIjANBg"
_dmarc.example.com. 3600 IN TXT "v=DMARC1; p=reject; rua=mailto:postmaster@example.com"
_smtp._tls.example.com. 3600 IN TXT "v=TLSRPTv1; rua=mailto:postmaster@example.com"
example.com. 3600 IN CAA 0 issue "letsencrypt.org"
mta-sts.example.com. 3600 IN CNAME mail.blr.example.test.
_mta-sts.example.com. 3600 IN TXT "v=STSv1; id=1"
autoconfig.example.com. 3600 IN CNAME mail.blr.example.test.
_imaps._tcp.example.com. 3600 IN SRV 0 1 993 mail.blr.example.test.
other.org. 3600 IN MX 10 mail.blr.example.test.
"""


class TestZone(UnitTestCase):
    def test_parse_zone_file(self) -> None:
        records = parse_zone_file(ZONE)
        self.assertEqual(records[0].name, "example.com")
        self.assertEqual(records[0].type, "MX")
        self.assertEqual(records[0].rdata, "10 mail.blr.example.test.")
        self.assertEqual(records[2].rdata, '"v=DKIM1; k=rsa; " "p=MIIBIjANBg"')

    def test_build_domain_records(self) -> None:
        rows = build_domain_records("example.com", ZONE, spf_include="spf.blr.example.test")
        by_category = {(r["category"], r["host"]): r for r in rows}

        mx = by_category[("Receiving", "@")]
        self.assertEqual(
            (mx["record_type"], mx["priority"], mx["value"]), ("MX", 10, "mail.blr.example.test")
        )
        self.assertEqual(by_category[("Sending", "@")]["value"], "v=spf1 include:spf.blr.example.test -all")
        dkim = by_category[("DKIM", "v1-rsa-20260101._domainkey")]
        self.assertEqual(dkim["value"], "v=DKIM1; k=rsa; p=MIIBIjANBg")  # quoted chunks joined
        self.assertEqual(by_category[("DMARC", "_dmarc")]["is_mandatory"], 1)
        self.assertEqual(by_category[("TLS Reporting", "_smtp._tls")]["is_mandatory"], 0)
        self.assertEqual(by_category[("Other", "@")]["record_type"], "CAA")
        self.assertNotIn(("MTA-STS", "mta-sts"), by_category)
        self.assertNotIn(("Auto-config", "autoconfig"), by_category)
        self.assertFalse(any(r["host"] == "other.org" for r in rows))
        self.assertTrue(all(r["is_mandatory"] for r in rows[:4]))  # mandatory rows first

    def test_client_discovery_records_are_opt_in(self) -> None:
        rows = build_domain_records("example.com", ZONE, spf_include="spf.x", include_client_discovery=True)
        categories = {r["category"] for r in rows}
        self.assertIn("MTA-STS", categories)
        self.assertIn("Auto-config", categories)
        srv = next(r for r in rows if r["record_type"] == "SRV")
        self.assertEqual((srv["host"], srv["value"]), ("_imaps._tcp", "0 1 993 mail.blr.example.test"))

    def test_fake_stalwart_zone_parses(self) -> None:
        fake = FakeStalwart()
        domain = fake._create("Domain", {"name": "acme.com"}, fake.objects["Domain"])
        rows = build_domain_records("acme.com", domain["dnsZoneFile"], spf_include="spf.x")
        self.assertEqual(sum(r["category"] == "DKIM" for r in rows), 2)
