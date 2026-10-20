# Telemetry

Module `suite_cloud.api.telemetry`. The agent on each Stalwart or SFU node pushes the node's logs, traces and metrics here as JSON. Suite Cloud stages every accepted batch as a parquet file, copies the files into Frappe Insights' DuckDB every five minutes and deletes rows older than the retention in **Suite Cloud Settings > Telemetry** daily. Operators read the data in the Insights dashboards Suite Cloud ships.

| Method | Verb | Purpose |
| --- | --- | --- |
| `telemetry.events` | POST | Stages a batch of log and trace events |
| `telemetry.metrics` | POST | Stages a batch of metric samples |

## Authentication

Every agent sends the telemetry secret from **Suite Cloud Settings > Telemetry** as a bearer token:

```http
Authorization: Bearer <telemetry secret>
```

The secret is accepted only on these two methods and runs them as a user without any role, so it opens nothing else. An empty secret turns the API off: every request is refused with a 401. To rotate, change the setting and give the agents the new value. There is no overlap window: an agent still sending the old secret gets a 401 until it is updated.

## Request

Both methods take the same envelope and a list of records. A batch is checked whole: a bad value anywhere refuses the whole batch and stages nothing.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `source` | string | Required. What produced the data, such as `stalwart` or `sfu`: 1 to 64 lowercase letters, digits, `-` or `_` |
| `host_ip` | string | Required. The node's IPv4 or IPv6 address, stored in its normalized form |
| `hostname` | string | Optional. The node's hostname, at most 253 characters |
| `batch_id` | string | Optional. A UUID the agent gives the batch, see [retries](#retries). Suite Cloud generates a UUIDv7 when it is left out |
| `records` | array | Required. 1 to 5000 records |

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
| `value` | number | Required. The counter total, the gauge reading, or the histogram's last observation |
| `count` | integer | Optional. A histogram's number of observations |
| `sum` | number | Optional. A histogram's sum of observations |
| `labels` | object | Optional. The metric's labels, stored as JSON text |

Unknown fields in a record are ignored, so an agent may add fields before Suite Cloud learns them.

For Stalwart, the agent forwards Stalwart's own event names (`delivery.delivered`, `auth.failed`, `smtp.connection-start`), metric names (`server.memory`, `queue.count`, `delivery.total-time`) and the keys of its event data (`remoteIp`, `domain`, `queueId`) unchanged. The shipped dashboards filter on those names.

```http
POST /api/v2/method/suite_cloud.api.telemetry.events
Authorization: Bearer <telemetry secret>
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
| 400 | `InvalidInputError` | A required parameter left out, a bad address, source, hostname, batch id or timestamp, more than 5000 records, or a bad field; the message names the record, such as `records[12].timestamp must carry a UTC offset.` |
| 401 | `AuthenticationError` | The secret is wrong or not set |
| 403 | `PermissionError` | A verb other than POST, or a caller that is not a telemetry agent |
| 417 | `FrappeTypeError` | A parameter of the wrong JSON type, such as `records` sent as a string |
| 429 | `TooManyRequestsError` | The node sent more than 300 requests in the current minute |
| 500 | | The batch could not be written, for example because the disk is full; nothing was staged |

## Retries

An agent retries on 429 and on any 5xx or network failure, with a backoff. It does not retry a 400, 401, 403 or 417: the same request would fail again.

Give every batch a `batch_id` and keep it across retries. A retry of a batch Suite Cloud already staged replaces the staged file, and a batch already loaded into Insights is not loaded twice, so a request whose answer was lost in transit is safe to send again.

Each node may make 300 requests per minute, counted per `host_ip` in fixed one-minute windows.
