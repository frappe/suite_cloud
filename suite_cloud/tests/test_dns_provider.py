from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from suite_cloud.dns.provider import DNSProvider


class TestMXRecords(UnitTestCase):
    """Lexicon drops an MX record's priority, so MX records take their own way to the provider."""

    def test_digitalocean_gets_the_priority_and_a_dotted_target(self) -> None:
        provider = DNSProvider("DigitalOcean", "example.com", token="do-token")
        response = MagicMock()
        with patch("requests.post", return_value=response) as post:
            self.assertTrue(provider.create_dns_record("MX", "acme", "mail.c1.example.net", 300, 10))

        self.assertEqual(
            post.call_args.args[0], "https://api.digitalocean.com/v2/domains/example.com/records"
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"type": "MX", "name": "acme", "data": "mail.c1.example.net.", "priority": 10, "ttl": 300},
        )
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer do-token"})
        response.raise_for_status.assert_called_once()

    def test_cloudflare_gets_the_priority_as_text(self) -> None:
        provider = DNSProvider("Cloudflare", "example.com", token="cf-token")
        with patch("lexicon.client.Client") as client:
            provider.create_dns_record("MX", "acme", "mail.c1.example.net", 300, 10)

        self.assertEqual(client.call_args.args[0]["priority"], "10")

    def test_other_providers_refuse_rather_than_drop_the_priority(self) -> None:
        provider = DNSProvider("Hetzner", "example.com", token="h-token")
        self.assertRaises(
            NotImplementedError, provider.create_dns_record, "MX", "acme", "mail.example.net", 300, 10
        )

    def test_other_record_types_still_go_through_lexicon(self) -> None:
        provider = DNSProvider("DigitalOcean", "example.com", token="do-token")
        with patch("lexicon.client.Client") as client, patch("requests.post") as post:
            provider.create_dns_record("TXT", "acme", "v=spf1 -all", 300)

        self.assertEqual(client.call_args.args[0]["type"], "TXT")
        post.assert_not_called()
