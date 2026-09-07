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
ua-auto-config.example.com. 3600 IN CNAME mail.blr.example.test.
_ua-auto-config.example.com. 3600 IN TXT "v=UAAC1; a=sha256; d=abc"
mta-sts.example.com. 3600 IN CNAME mail.blr.example.test.
_mta-sts.example.com. 3600 IN TXT "v=STSv1; id=1"
autoconfig.example.com. 3600 IN CNAME mail.blr.example.test.
_imaps._tcp.example.com. 3600 IN SRV 0 1 993 mail.blr.example.test.
other.org. 3600 IN MX 10 mail.blr.example.test.
v2-rsa-20260401._domainkey.example.com. IN TXT ( ; rotated key, written the way Stalwart does
    "v=DKIM1; k=rsa; h=sha256; p=MIIBIjANBgkq"
    "hkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"
)
"""


class TestZone(UnitTestCase):
    def test_parse_zone_file(self) -> None:
        records = parse_zone_file(ZONE)
        self.assertEqual(records[0].name, "example.com")
        self.assertEqual(records[0].type, "MX")
        self.assertEqual(records[0].rdata, "10 mail.blr.example.test.")
        self.assertEqual(records[2].rdata, '"v=DKIM1; k=rsa; " "p=MIIBIjANBg"')
        multiline = records[-1]
        self.assertEqual(
            (multiline.name, multiline.ttl, multiline.type),
            ("v2-rsa-20260401._domainkey.example.com", None, "TXT"),
        )
        self.assertEqual(
            multiline.rdata, '"v=DKIM1; k=rsa; h=sha256; p=MIIBIjANBgkq" "hkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"'
        )

    def test_build_domain_records(self) -> None:
        rows = build_domain_records("example.com", ZONE, spf_include="spf.blr.example.test")
        by_category = {(r["category"], r["host"]): r for r in rows}

        mx = by_category[("MX", "@")]
        self.assertEqual(
            (mx["record_type"], mx["priority"], mx["value"], mx["group"], mx["is_mandatory"]),
            ("MX", 10, "mail.blr.example.test", "routing_records", 0),
        )
        spf = by_category[("SPF", "@")]
        self.assertEqual(spf["value"], "v=spf1 include:spf.blr.example.test -all")
        self.assertEqual((spf["group"], spf["is_mandatory"]), ("authentication_records", 1))
        dkim = by_category[("DKIM", "v1-rsa-20260101._domainkey")]
        self.assertEqual(dkim["value"], "v=DKIM1; k=rsa; p=MIIBIjANBg")  # quoted chunks joined
        self.assertEqual(
            by_category[("DKIM", "v2-rsa-20260401._domainkey")]["value"],
            "v=DKIM1; k=rsa; h=sha256; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA",
        )
        self.assertEqual(by_category[("DMARC", "_dmarc")]["is_mandatory"], 1)
        tlsrpt = by_category[("TLS-RPT", "_smtp._tls")]
        self.assertEqual((tlsrpt["group"], tlsrpt["is_mandatory"]), ("transport_security_records", 0))
        srv = by_category[("SRV", "_imaps._tcp")]  # targets the cluster host: no certificate needed
        self.assertEqual(
            (srv["group"], srv["priority"], srv["weight"], srv["port"], srv["value"]),
            ("discovery_records", 0, 1, 993, "mail.blr.example.test"),
        )
        self.assertEqual((mx["weight"], mx["port"]), (0, 0))
        self.assertNotIn(("MTA-STS", "mta-sts"), by_category)
        self.assertNotIn(("Autoconfig", "autoconfig"), by_category)
        self.assertNotIn(("UA Auto Config", "ua-auto-config"), by_category)
        self.assertFalse(any(r["record_type"] == "CAA" for r in rows))  # nothing the domain needs
        self.assertFalse(any(r["host"] == "other.org" for r in rows))
        # Group order, authentication first.
        self.assertEqual([r["category"] for r in rows[:5]], ["SPF", "DKIM", "DKIM", "DMARC", "MX"])

    def test_certificate_bound_records_are_opt_in(self) -> None:
        rows = build_domain_records("example.com", ZONE, spf_include="spf.x", include_client_discovery=True)
        by_category = {(r["category"], r["host"]): r for r in rows}
        self.assertEqual(by_category[("MTA-STS", "mta-sts")]["group"], "transport_security_records")
        self.assertEqual(by_category[("MTA-STS", "_mta-sts")]["value"], "v=STSv1; id=1")
        self.assertEqual(by_category[("Autoconfig", "autoconfig")]["group"], "autoconfig_records")
        self.assertEqual(by_category[("UA Auto Config", "ua-auto-config")]["value"], "mail.blr.example.test")
        self.assertEqual(
            by_category[("UA Auto Config", "_ua-auto-config")]["value"], "v=UAAC1; a=sha256; d=abc"
        )

    def test_fake_stalwart_zone_parses(self) -> None:
        fake = FakeStalwart()
        domain = fake._create("Domain", {"name": "acme.com"}, fake.objects["Domain"])
        rows = build_domain_records("acme.com", domain["dnsZoneFile"], spf_include="spf.x")
        self.assertEqual(sum(r["category"] == "DKIM" for r in rows), 2)
