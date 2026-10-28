"""The telemetry API: the agents on the nodes push batches of events and metrics.

An agent sends ``Authorization: Bearer <agent key>:<agent secret>``, its cluster's credentials. The
``authenticate`` auth hook finds the cluster, checks the secret and runs the request as the
telemetry user, which holds no role, so the token opens nothing but these methods, and only for
that cluster's own servers. A batch is validated whole, staged as one parquet file and acknowledged;
the warehouse loads it later. Nothing here touches the site database; the cache notes which host
sent a batch last, for the agent status on the node forms.
"""

import ipaddress
import math
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import NoReturn
from uuid import UUID, uuid7

import frappe
import orjson
from frappe import _
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password

from suite_cloud.api.tenant import InvalidInputError, assert_arguments, throttle
from suite_cloud.telemetry import EVENTS, METRICS, TELEMETRY_USER, TelemetryTable
from suite_cloud.telemetry.agent.status import record_contact
from suite_cloud.telemetry.spool import Spool
from suite_cloud.utils import get_config

API_PATHS = ("/api/v2/method/suite_cloud.api.telemetry.", "/api/method/suite_cloud.api.telemetry.")
DEFAULT_REQUESTS_PER_MINUTE = 300  # the settings' defaults, used until the site has them
DEFAULT_RECORDS_PER_REQUEST = 5000
HOSTNAME_LIMIT = 253
SOURCE_PATTERN = re.compile(r"[a-z0-9_-]{1,64}")
METRIC_TYPES = ("counter", "gauge", "histogram")
SERVER_DOCTYPES = ("Stalwart Node", "Egress Gateway")
INTEGER_LIMIT = 2**63  # the metrics table stores counts as 64-bit integers


def authenticate() -> None:
    """Frappe auth hook: a bearer token on the telemetry API is an Agent's cluster credentials.

    Elsewhere a bearer token is Frappe's own OAuth business, so the hook leaves it alone.
    """

    scheme, _space, token = frappe.get_request_header("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not frappe.request.path.startswith(API_PATHS):
        return

    cluster = get_agent_cluster(token.strip())
    if not cluster:
        request_ip = getattr(frappe.local, "request_ip", None)
        throttle(f"address:{request_ip or 'unknown'}")  # guessing secrets is paced like everything else
        raise frappe.AuthenticationError(_("Telemetry authentication failed."))

    form_dict = frappe.local.form_dict
    frappe.set_user(TELEMETRY_USER)
    frappe.local.form_dict = form_dict  # set_user clears the request body Frappe already parsed
    frappe.local.agent_cluster = cluster


def get_agent_cluster(token: str) -> str | None:
    """The cluster whose Agent Key and Agent Secret make up ``<key>:<secret>``, while telemetry is
    on; None for anything else."""

    key, separator, secret = token.partition(":")
    if not (separator and key and secret and cint(get_config("enable_telemetry"))):
        return None
    cluster = frappe.db.get_value("Stalwart Cluster", {"agent_key": key}, "name")
    stored = cluster and get_decrypted_password(
        "Stalwart Cluster", cluster, "agent_secret", raise_exception=False
    )
    if not stored or not secrets.compare_digest(stored, secret):
        return None
    return cluster


def telemetry_api(fn: Callable) -> Callable:
    """Only an Agent the auth hook signed in for its cluster may call the method; a missing argument
    is a 400."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if frappe.session.user != TELEMETRY_USER or not getattr(frappe.local, "agent_cluster", None):
            frappe.throw(_("Only telemetry agents may call this method."), frappe.PermissionError)
        assert_arguments(fn, args, kwargs)
        return fn(*args, **kwargs)

    wrapper.is_telemetry_api = True  # the contract test checks every endpoint carries this
    return wrapper


@frappe.whitelist(methods=["POST"])
@telemetry_api
def events(
    source: str, host_ip: str, records: list, hostname: str | None = None, batch_id: str | None = None
) -> dict:
    """Stages a batch of log and trace events: ``{timestamp, level, event, span_id?, data?}`` each."""

    return accept(EVENTS, parse_event, source, host_ip, records, hostname, batch_id)


@frappe.whitelist(methods=["POST"])
@telemetry_api
def metrics(
    source: str, host_ip: str, records: list, hostname: str | None = None, batch_id: str | None = None
) -> dict:
    """Stages a batch of metrics: ``{timestamp, name, type, value, count?, sum?, labels?}`` each."""

    return accept(METRICS, parse_metric, source, host_ip, records, hostname, batch_id)


def accept(
    table: TelemetryTable,
    parse: Callable,
    source: str,
    host_ip: str,
    records: list,
    hostname: str | None,
    batch_id: str | None,
) -> dict:
    envelope = get_envelope(source, host_ip, hostname, batch_id)
    rows = [parse(record) for record in get_records(records)]
    Spool().write(table, table.get_batch(envelope, rows), envelope["batch_id"])
    record_contact(envelope["host_ip"], frappe.get_request_header("User-Agent"))
    return {"batch_id": envelope["batch_id"], "accepted": len(rows)}


def get_envelope(source: str, host_ip: str, hostname: str | None, batch_id: str | None) -> dict:
    """What every row of the batch shares, checked; the sending host is throttled by its address."""

    if not isinstance(source, str) or not SOURCE_PATTERN.fullmatch(source):
        frappe.throw(_("source must be 1 to 64 lowercase letters, digits, - or _."), InvalidInputError)
    try:
        address = ipaddress.ip_address(host_ip)
    except ValueError:
        frappe.throw(_("host_ip must be an IPv4 or IPv6 address."), InvalidInputError)
    hostname = (hostname or "").strip() or None
    if hostname and len(hostname) > HOSTNAME_LIMIT:
        frappe.throw(_("hostname must be at most {0} characters.").format(HOSTNAME_LIMIT), InvalidInputError)
    if batch_id:
        try:
            batch_id = str(UUID(str(batch_id)))
        except ValueError:
            frappe.throw(_("batch_id must be a UUID."), InvalidInputError)

    throttle(f"agent:{address}", get_limit("agent_requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE))
    assert_cluster_server(str(address))
    return {
        "batch_id": batch_id or str(uuid7()),
        "received_at": datetime.now(UTC).replace(tzinfo=None),
        "source": source,
        "host_ip": str(address),
        "hostname": hostname,
    }


def assert_cluster_server(address: str) -> None:
    """A batch speaks for one of the caller's cluster's own nodes or gateways, whatever their status."""

    cluster = frappe.local.agent_cluster
    for doctype in SERVER_DOCTYPES:
        if frappe.db.exists(doctype, {"cluster": cluster, "ipv4_address": address}):
            return
    frappe.throw(
        _("host_ip {0} is not a node or gateway of the Agent's cluster.").format(address),
        frappe.PermissionError,
    )


def get_records(records: list) -> list[Record]:
    if not records:
        frappe.throw(_("records must hold at least one record."), InvalidInputError)
    limit = get_limit("agent_records_per_request", DEFAULT_RECORDS_PER_REQUEST)
    if len(records) > limit:
        frappe.throw(
            _("records holds {0} records; send at most {1} in one request.").format(len(records), limit),
            InvalidInputError,
        )
    return [Record(index, record) for index, record in enumerate(records)]


def get_limit(key: str, default: int) -> int:
    """A limit from the settings; a site that has not filled the field yet gets the default."""

    return cint(get_config(key)) or default


def parse_event(record: Record) -> dict:
    return {
        "timestamp": record.timestamp("timestamp"),
        "level": record.text("level", required=True, limit=16),
        "event": record.text("event", required=True, limit=128),
        "span_id": record.text("span_id", limit=64),
        "data": record.json_object("data"),
    }


def parse_metric(record: Record) -> dict:
    return {
        "timestamp": record.timestamp("timestamp"),
        "name": record.text("name", required=True, limit=128),
        "type": record.text("type", required=True, choices=METRIC_TYPES),
        "value": record.number("value", required=True),
        "count": record.number("count", integer=True),
        "sum": record.number("sum"),
        "labels": record.json_object("labels"),
    }


class Record:
    """One record of the request, read field by field; a bad value is a 400 that names it."""

    def __init__(self, index: int, fields: object):
        self.index = index
        if not isinstance(fields, dict):
            self.refuse("", _("must be an object"))
        self.fields = fields

    def refuse(self, key: str, problem: str) -> NoReturn:
        frappe.throw(
            _("records[{0}]{1} {2}.").format(self.index, f".{key}" if key else "", problem), InvalidInputError
        )

    def text(
        self, key: str, required: bool = False, limit: int = 256, choices: tuple[str, ...] | None = None
    ) -> str | None:
        value = self.fields.get(key)
        if value is None or value == "":
            if required:
                self.refuse(key, _("is required"))
            return None
        if isinstance(value, bool) or not isinstance(value, str | int):
            self.refuse(key, _("must be a string"))
        value = str(value)
        if len(value) > limit:
            self.refuse(key, _("must be at most {0} characters").format(limit))
        if choices and value not in choices:
            self.refuse(key, _("must be one of {0}").format(", ".join(choices)))
        return value

    def timestamp(self, key: str) -> datetime:
        """An RFC 3339 instant with its offset, stored as naive UTC."""

        value = self.fields.get(key)
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            self.refuse(key, _("must be an RFC 3339 timestamp"))
        if moment.tzinfo is None:
            self.refuse(key, _("must carry a UTC offset"))
        return moment.astimezone(UTC).replace(tzinfo=None)

    def number(self, key: str, required: bool = False, integer: bool = False) -> int | float | None:
        value = self.fields.get(key)
        if value is None:
            if required:
                self.refuse(key, _("is required"))
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.refuse(key, _("must be a number"))
        try:  # NaN or infinity as an integer, or an integer too large for a double
            number = int(value) if integer else float(value)
        except (OverflowError, ValueError):
            self.refuse(key, _("must be a finite number"))
        if integer and (number != value or not -INTEGER_LIMIT <= number < INTEGER_LIMIT):
            self.refuse(key, _("must fit a 64-bit integer"))
        if not integer and not math.isfinite(number):
            self.refuse(key, _("must be a finite number"))
        return number

    def json_object(self, key: str) -> str | None:
        value = self.fields.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            self.refuse(key, _("must be an object"))
        return orjson.dumps(value).decode()  # a tenth of the standard encoder's time per record
