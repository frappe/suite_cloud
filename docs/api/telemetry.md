# Telemetry

Module `suite_cloud.api.telemetry`. The [Suite Cloud Agent](../agent.md) on each Stalwart node, egress gateway or SFU node pushes the node's logs, traces and metrics here as JSON. Suite Cloud stages every accepted batch as a parquet file, copies the files into Frappe Insights' DuckDB every five minutes and deletes rows older than the retention in **Suite Cloud Settings > Agent & Telemetry** daily. Operators read the data in the Insights dashboards Suite Cloud ships. The loader copies the event data keys the dashboards read (`remoteIp`, `listenerId`, `domain`, `hostname`, `details` or `reason`, `code`, `accountName` or `accountId`, `from`, `to`, `elapsed`) into columns of the `events` table, with the SMTP code and the sender and recipient domains, so a chart does not parse the JSON of every row. Each dashboard opens on yesterday and today; the DMARC and TLS dashboards, which read daily reports, open on the last week.

| Method | Verb | Purpose |
| --- | --- | --- |
| `telemetry.events` | POST | Stages a batch of log and trace events |
| `telemetry.metrics` | POST | Stages a batch of metric samples |
| `telemetry.quota.limits` | POST | A page of the caller's cluster's sending limits |
| `telemetry.quota.usage` | POST | Stores the recipients the caller counted per account |

## Authentication

Every agent sends its cluster's **Agent Key** and **Agent Secret**, from the Stalwart Cluster record, as a bearer token:

```http
Authorization: Bearer <agent key>:<agent secret>
```

The token is accepted only on these methods and runs them as a user without any role, so it opens nothing else. It also holds the caller to its cluster: `host_ip` must be one of that cluster's nodes or gateways, and a batch for any other address is a 403. With **Enable Telemetry** off in **Suite Cloud Settings > Agent & Telemetry**, every request is refused with a 401. **Rotate Agent Secret** on the cluster replaces the secret at once and reinstalls the cluster's agents; an agent still sending the old secret gets a 401 and retries until its new config arrives, so nothing is lost.

## Request

Both methods take the same envelope and a list of records. A batch is checked whole: a bad value anywhere refuses the whole batch and stages nothing.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `source` | string | Required. What produced the data, such as `stalwart` or `sfu`: 1 to 64 lowercase letters, digits, `-` or `_` |
| `host_ip` | string | Required. The node's IPv4 or IPv6 address, stored in its normalized form |
| `hostname` | string | Optional. The node's hostname, at most 253 characters |
| `batch_id` | string | Optional. A UUID the agent gives the batch, see [retries](#retries). Suite Cloud generates a UUIDv7 when it is left out |
| `records` | array | Required. 1 to **Telemetry Records per Request** (5000 by default) records |

An event record:

| Field | Type | Meaning |
| --- | --- | --- |
| `timestamp` | string | Required. RFC 3339 with a UTC offset, such as `2026-10-20T09:59:00Z`; stored in UTC |
| `level` | string | Required. `trace`, `debug`, `info`, `warn` or `error` as the source names it, at most 16 characters |
| `event` | string | Required. The event name, at most 128 characters |
| `span_id` | string or integer | Optional. The span the event belongs to, stored as text |
| `data` | object | Optional. The event's own fields, stored as JSON text |

A metric record:

| Field | Type | Meaning |
| --- | --- | --- |
| `timestamp` | string | Required. As above |
| `name` | string | Required. The metric name, at most 128 characters |
| `type` | string | Required. `counter`, `gauge` or `histogram` |
| `value` | number | Required. The counter total, the gauge reading, or the histogram's mean |
| `count` | integer | Optional. A histogram's number of observations |
| `sum` | number | Optional. A histogram's sum of observations |
| `labels` | object | Optional. The metric's labels, stored as JSON text |

Unknown fields in a record are ignored, so an agent may add fields before Suite Cloud learns them.

### Machine metrics

The agent also reports the machine it runs on, under the same `source` as the product on it, with these names. All are gauges the agent has already normalized, so a dashboard needs no counter arithmetic.

| Name | Value | Labels |
| --- | --- | --- |
| `host.cpu.percent` | Busy share of all cores, 0 to 100 | |
| `host.load.1m`, `host.load.5m`, `host.load.15m` | Load averages | |
| `host.memory.total.bytes`, `host.memory.used.bytes` | Memory size and what is in use (total minus available) | |
| `host.swap.used.bytes` | Swap in use | |
| `host.disk.total.bytes`, `host.disk.used.bytes` | Size and use of one mount | `mount` |
| `host.disk.read.bytes_per_second`, `host.disk.write.bytes_per_second` | Throughput of one device over the sample interval | `device` |
| `host.network.receive.bytes_per_second`, `host.network.transmit.bytes_per_second` | Throughput of one interface over the sample interval | `interface` |
| `host.uptime.seconds` | Seconds since the machine booted | |

The shipped **Machines** Insights workbook reads these for every source.

For Stalwart, the agent reads Stalwart's Prometheus endpoint for the metrics, under Stalwart's own names (`server_memory` on the endpoint is `server.memory` here), and sends a histogram's mean since Stalwart started as its `value`, with `<name>.p50`, `<name>.p95` and `<name>.p99` gauges for the percentiles of the observations between two scrapes. It forwards the event names as they stand in Stalwart's log file (`delivery.delivered`, `auth.error`, `smtp.ehlo`, `smtp.dkim-pass`), the metric names (`server.memory`, `queue.count`, `delivery.total-time`) and the keys of the event data (`remoteIp`, `to`, `hostname`, `details`, `elapsed`) unchanged. The shipped dashboards filter on those names; the log at `info` level holds no connection, `auth.failed` or `spam.classify` events, so the dashboards read EHLO, `auth.error` and `message-ingest.spam` instead.

```http
POST /api/v2/method/suite_cloud.api.telemetry.events
Authorization: Bearer <agent key>:<agent secret>
Content-Type: application/json

{
  "source": "stalwart",
  "host_ip": "203.0.113.5",
  "hostname": "n1.c1.frappemail.com",
  "batch_id": "0199f7e0-5b1a-7c2d-8e3f-4a5b6c7d8e9f",
  "records": [
    {
      "timestamp": "2026-10-20T09:59:00Z",
      "level": "info",
      "event": "delivery.delivered",
      "span_id": 42,
      "data": {"queueId": 1234, "domain": "example.com", "remoteIp": "198.51.100.7", "elapsed": 412}
    }
  ]
}
```

## Response

An accepted batch answers 200 with the batch id and the number of records staged:

```json
{"data": {"batch_id": "0199f7e0-5b1a-7c2d-8e3f-4a5b6c7d8e9f", "accepted": 1}}
```

| Status | Type | Meaning |
| --- | --- | --- |
| 400 | `InvalidInputError` | A required parameter left out, a bad address, source, hostname, batch id or timestamp, more records than the setting allows, or a bad field; the message names the record, such as `records[12].timestamp must carry a UTC offset.` |
| 401 | `AuthenticationError` | The key or secret is wrong, or telemetry is off |
| 403 | `PermissionError` | A verb other than POST, a caller that is not a telemetry agent, or a `host_ip` that is not a server of the agent's cluster |
| 417 | `FrappeTypeError` | A parameter of the wrong JSON type, such as `records` sent as a string |
| 429 | `TooManyRequestsError` | The node sent more requests in the current minute than **Telemetry Requests per Minute** allows |
| 500 | | The batch could not be written, for example because the disk is full; nothing was staged |

## Retries

An agent retries on 429 and on any 5xx or network failure, with a backoff. It does not retry a 400, 401, 403 or 417: the same request would fail again.

Give every batch a `batch_id` and keep it across retries. A retry of a batch Suite Cloud already staged replaces the staged file, and a batch already loaded into Insights is not loaded twice, so a request whose answer was lost in transit is safe to send again.

Each node may make **Telemetry Requests per Minute** requests (300 by default), counted per `host_ip` in fixed one-minute windows. Both limits are in **Suite Cloud Settings > Agent & Telemetry**; a larger batch costs one request, and a batch is validated whole, so a change needs no release.

## Sending limits

The Agent on a Stalwart node uses two more methods to enforce the [sending limits](../agent.md#sending-limits). The caller only reads and reports the accounts of its own cluster, and `host_ip` must be one of that cluster's Active or Draining Stalwart Nodes; any other address is a 403. Both calls count against **Telemetry Requests per Minute**.

`telemetry.quota.limits` takes `host_ip` and `since_version`, and answers the limits changed after it, oldest first, at most 500 per page:

```json
{"data": {"limits": [{"account": "jane@acme.example", "daily": 500, "monthly": 10000, "version": "2026-10-26 10:15:02.123456"}], "next_version": "<cursor>", "has_more": false}}
```

`daily` and `monthly` are recipients per UTC day and month; 0 means no limit. `version` is the account's modified time, so an older entry never replaces a newer one. Send `next_version` as the next `since_version`, and an empty one to read everything again. A save is served once it is 5 seconds old.

`telemetry.quota.usage` takes `host_ip` and `records`, each `{"account", "period", "sent", "refused"}`, where `period` is `d:YYYYMMDD` or `m:YYYYMM`, `sent` the recipients counted so far and `refused` the messages refused over a limit, 0 when left out. Suite Cloud keeps the higher of the stored and the reported count, since several nodes report the same counter and it only grows within its period. Accounts of other clusters, and periods other than today, yesterday and their months, are dropped. It answers `{"data": {"accepted": n}}`.

## Alerts

Every five minutes Suite Cloud checks the loaded data and keeps a **Telemetry Alert** record per condition, open until it clears:

| Alert | Condition |
| --- | --- |
| Silent Host | A node that reported in the last day sent nothing for **Silent Host After (Minutes)**, 30 by default. The alert stays open until a newer batch proves the node is back, even once the retention has deleted its last rows; resolve it by hand when the node was taken out of service, and it stays resolved until the node reports again |
| Error Rate | More than **Error Rate Alert (%)** of a node's events in the last hour are `error` level, 5 by default, counted only over hours with at least 100 events |

**Telemetry Alert Recipients** get a mail when an alert opens and when it resolves; empty, the records are kept and nobody is mailed.
