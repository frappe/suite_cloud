"""The field names of the Stalwart release a cluster or gateway runs.

Stalwart 1.0 renamed fields of objects Suite Cloud writes. A 0.16 data store cannot be upgraded
to 1.x in place, and each cluster and gateway pins its own release, so 0.16 and 1.x servers run
side by side until 0.16 support ends. Payloads are built with the 0.16 names and renamed here
for a server on 1.x.
"""

from typing import TYPE_CHECKING

from suite_cloud.utils import get_config

if TYPE_CHECKING:
    from frappe.model.document import Document

# 0.16 name -> 1.x name, by object type. None: the field has no 1.x counterpart and is left out.
RENAMED: dict[str, dict[str, str | None]] = {
    "Bootstrap": {"serverHostname": "defaultHostname"},
    "Domain": {"isEnabled": "enable"},
    "EmailAlias": {"enabled": "enable"},
    "NetworkListener": {"tlsImplicit": "implicitTls"},
    "MtaRouteRelay": {"address": "host"},
    "MtaMilter": {
        "hostname": "host",
        "timeoutConnect": "connectTimeout",
        "timeoutCommand": "commandTimeout",
        "timeoutData": "dataTimeout",
    },
    "MetricsOtelHttp": {"endpoint": "url"},
    "PostgreSqlStore": {"timeout": "connectTimeout"},
    # MySQL's idleTimeout sets wait_timeout, how long an idle connection stays open: not a timeout.
    "MySqlStore": {"timeout": None},
    "RedisStore": {"timeout": "connectTimeout"},
    "RedisClusterStore": {"timeout": "connectTimeout"},
    "RedisSentinelStore": {"timeout": "connectTimeout"},
    "S3Store": {"accessKey": "accessKeyId"},
    "S3StoreCustomRegion": {"customEndpoint": "url", "customRegion": "region"},
    "MeilisearchStore": {"maxRetries": "maxPolls"},
}

# Events Stalwart 1.x no longer has: a WebHook that names one is refused.
REMOVED_EVENTS = frozenset(
    {
        "ai.api-error",
        "arc.sealer-not-found",
        "dns.build-error",
        "store.cache-entry-too-large",
        "telemetry.alert-event",
    }
)


def is_v1(target: Document | str | None) -> bool:
    """Whether a cluster or gateway, or a release such as v1.1.1, speaks the 1.x schema. Without a
    release of its own, a server gets the one Suite Cloud installs on a new cluster."""

    version = target if isinstance(target, str | None) else getattr(target, "stalwart_version", None)
    version = version or get_config("stalwart_version")
    return int(version.removeprefix("v").split(".")[0]) >= 1


def field(object_type: str, name: str, v1: bool) -> str:
    """A field's name on the server, from its 0.16 name."""

    return (RENAMED.get(object_type, {}).get(name) or name) if v1 else name


def adapt(object_type: str, value: dict, v1: bool) -> dict:
    """``value``, built with the 0.16 names, as the server's release names its fields."""

    if not v1:
        return value
    renamed = RENAMED.get(object_type, {})
    adapted = {}
    for key, item in value.items():
        if key in renamed:
            if renamed[key] is None:
                continue
            key = renamed[key]
        adapted[key] = item
    return adapted


def events(names: tuple[str, ...], v1: bool) -> tuple[str, ...]:
    """The events of ``names`` that the server's release has."""

    return tuple(name for name in names if name not in REMOVED_EVENTS) if v1 else names
