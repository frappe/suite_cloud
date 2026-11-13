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

    def test_a_ttl_change_keeps_an_mx_record_the_provider_cannot_recreate(self) -> None:
        provider = DNSProvider("Hetzner", "example.com", token="h-token")
        record = {"content": "mail.example.net", "ttl": 600}
        with (
            patch.object(provider, "find_dns_record", return_value=record),
            patch.object(provider, "delete_dns_record") as delete,
        ):
            self.assertRaises(
                NotImplementedError, provider.ensure_dns_record, "MX", "acme", "mail.example.net", 300, 10
            )

        delete.assert_not_called()

    def test_other_record_types_still_go_through_lexicon(self) -> None:
        provider = DNSProvider("DigitalOcean", "example.com", token="do-token")
        with patch("lexicon.client.Client") as client, patch("requests.post") as post:
            provider.create_dns_record("TXT", "acme", "v=spf1 -all", 300)

        self.assertEqual(client.call_args.args[0]["type"], "TXT")
        post.assert_not_called()


class FakeRoute53:
    """Route53's record sets, refusing a DELETE whose TTL or values differ like the real API."""

    def __init__(self, record_sets: dict) -> None:
        self.record_sets = record_sets

    def get_hosted_zone(self, Id: str) -> None:
        pass

    def list_resource_record_sets(self, **kwargs) -> dict:
        record_sets = [
            {"Name": name, "Type": type, **rest} for (name, type), rest in self.record_sets.items()
        ]
        return {"ResourceRecordSets": record_sets, "IsTruncated": False}

    def change_resource_record_sets(self, HostedZoneId: str, ChangeBatch: dict) -> None:
        from botocore.exceptions import ClientError

        for change in ChangeBatch["Changes"]:
            record_set = dict(change["ResourceRecordSet"])
            key = (record_set.pop("Name"), record_set.pop("Type"))
            if change["Action"] != "DELETE":
                self.record_sets[key] = record_set
            elif self.record_sets.get(key) == record_set:
                del self.record_sets[key]
            else:
                error = {"Error": {"Code": "InvalidChangeBatch", "Message": "values do not match"}}
                raise ClientError(error, "ChangeResourceRecordSets")


class TestRoute53Deletes(UnitTestCase):
    """Route53 keeps one record set per host and type, and deletes it only by its exact TTL."""

    key = ("spf.c4.example.com.", "TXT")

    def delete(self, record_sets: dict, value: str) -> None:
        provider = DNSProvider("AmazonRoute53", "example.com", "key", "secret", zone_id="Z1")
        with patch("boto3.client", return_value=FakeRoute53(record_sets)):
            provider.delete_dns_record("TXT", "spf.c4", value)

    def test_a_record_with_a_non_default_ttl_is_deleted(self) -> None:
        record_sets = {self.key: {"TTL": 300, "ResourceRecords": [{"Value": '"v=spf1 -all"'}]}}
        self.delete(record_sets, "v=spf1 -all")
        self.assertEqual(record_sets, {})

    def test_deleting_one_value_keeps_the_others_and_their_ttl(self) -> None:
        values = [{"Value": '"v=spf1 -all"'}, {"Value": '"v=spf1 ip4:192.0.2.1 -all"'}]
        record_sets = {self.key: {"TTL": 300, "ResourceRecords": values}}
        self.delete(record_sets, "v=spf1 -all")
        self.assertEqual(
            record_sets,
            {self.key: {"TTL": 300, "ResourceRecords": [{"Value": '"v=spf1 ip4:192.0.2.1 -all"'}]}},
        )
