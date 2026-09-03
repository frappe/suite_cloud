from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from lexicon.client import Client


class DNSProviderEnum(str, Enum):
    AMAZON_ROUTE53 = "AmazonRoute53"
    DIGITALOCEAN = "DigitalOcean"
    CLOUDFLARE = "Cloudflare"
    HETZNER = "Hetzner"
    LINODE = "Linode"
    NAMECHEAP = "Namecheap"
    GODADDY = "GoDaddy"


DNSProviderLiteral = Literal[
    DNSProviderEnum.AMAZON_ROUTE53,
    DNSProviderEnum.DIGITALOCEAN,
    DNSProviderEnum.CLOUDFLARE,
    DNSProviderEnum.HETZNER,
    DNSProviderEnum.LINODE,
    DNSProviderEnum.NAMECHEAP,
    DNSProviderEnum.GODADDY,
]


DNS_PROVIDER_MAP = {
    DNSProviderEnum.AMAZON_ROUTE53: "route53",
    DNSProviderEnum.DIGITALOCEAN: "digitalocean",
    DNSProviderEnum.CLOUDFLARE: "cloudflare",
    DNSProviderEnum.HETZNER: "hetzner",
    DNSProviderEnum.LINODE: "linode",
    DNSProviderEnum.NAMECHEAP: "namecheap",
    DNSProviderEnum.GODADDY: "godaddy",
}


class DNSProvider:
    """DNS records under the root domain, through Lexicon.

    A host/type pair may legitimately hold several records (round-robin A records), so every
    write and delete is keyed on the record's value as well.
    """

    def __init__(
        self,
        provider: DNSProviderLiteral,
        domain: str,
        access_key: str | None = None,
        access_secret: str | None = None,
        auth_key: str | None = None,
        auth_secret: str | None = None,
        username: str | None = None,
        token: str | None = None,
        client_ip: str | None = None,
        zone_id: str | None = None,
        private_zone: bool = False,
    ) -> None:
        if provider not in DNS_PROVIDER_MAP:
            raise ValueError(f"Unsupported DNS Provider: {provider}")

        self.provider = DNS_PROVIDER_MAP[provider]
        self.domain = domain
        self.__access_key = access_key
        self.__access_secret = access_secret
        self.__auth_key = auth_key
        self.__auth_secret = auth_secret
        self.__username = username
        self.__token = token
        self.client_ip = client_ip
        self.zone_id = zone_id
        self.private_zone = private_zone

    def get_client(self, config: dict) -> Client:
        from lexicon.client import Client

        config.update(
            {
                "provider_name": self.provider,
                "domain": self.domain,
                "auth_access_key": self.__access_key,
                "auth_access_secret": self.__access_secret,
                "auth_key": self.__auth_key,
                "auth_secret": self.__auth_secret,
                "auth_username": self.__username,
                "auth_token": self.__token,
                "auth_client_ip": self.client_ip,
                "zone_id": self.zone_id,
                "private_zone": self.private_zone,
            }
        )
        return Client(config)

    def read_dns_records(self, type: str, host: str | None = None) -> list[dict]:
        config = {"action": "list"}
        if type:
            config["type"] = type
        if host:
            config["name"] = host

        return self.get_client(config).execute()

    def find_dns_record(self, type: str, host: str, value: str) -> dict | None:
        """Returns the provider's record with exactly this value, or None."""

        for record in self.read_dns_records(type=type, host=host):
            if normalize_value(record.get("content")) == normalize_value(value):
                return record
        return None

    def create_dns_record(self, type: str, host: str, value: str, ttl: int, priority: int = 0) -> bool:
        config = {
            "action": "create",
            "type": type,
            "name": host,
            "content": value,
            "ttl": ttl,
            "priority": priority,
        }
        return self.get_client(config).execute()

    def update_dns_record(
        self, record_id: int | str, type: str, host: str, value: str, ttl: int, priority: int = 0
    ) -> bool:
        config = {
            "action": "update",
            "type": type,
            "name": host,
            "content": value,
            "ttl": ttl,
            "priority": priority,
            "identifier": record_id,
        }
        return self.get_client(config).execute()

    def ensure_dns_record(self, type: str, host: str, value: str, ttl: int, priority: int = 0) -> bool:
        """Creates the record unless one with the same value exists (then refreshes its TTL)."""

        if record := self.find_dns_record(type, host, value):
            if int(record.get("ttl") or 0) == int(ttl):
                return True
            return self.update_dns_record(record["id"], type, host, value, ttl, priority)

        return self.create_dns_record(type, host, value, ttl, priority)

    def delete_dns_record(self, type: str, host: str, value: str | None = None) -> bool:
        """Deletes the record with ``value`` (or every record of host/type when value is None)."""

        records = self.read_dns_records(type=type, host=host)
        if value is not None:
            records = [r for r in records if normalize_value(r.get("content")) == normalize_value(value)]

        success = True
        for record in records:
            config = {"action": "delete", "type": type, "name": host, "identifier": record["id"]}
            try:
                self.get_client(config).execute()
            except Exception:
                success = False

        return success


def normalize_value(value: str | None) -> str:
    """Providers quote TXT content and may add a trailing dot to targets."""

    return (value or "").strip().strip('"').rstrip(".").lower()
