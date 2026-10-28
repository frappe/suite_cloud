# Suite Cloud Agent

The Suite Cloud Agent is one Go binary, `suite-cloud-agent`, that runs as a systemd service on every Stalwart node and egress gateway. It tails Stalwart's log files, samples the machine, scrapes Stalwart's Prometheus endpoint and pushes the records to the [telemetry API](api/telemetry.md). On a Stalwart node it also enforces the [sending limits](#sending-limits) of Mail Accounts. Its source is the `agent/` folder of this repository. Suite Cloud builds it on the bench host and installs it with the node's provisioning job.

## Configuration

The agent starts from `/etc/suite-cloud-agent/config.json`. Provisioning writes the file from Suite Cloud Settings and the node record; a change to either reaches the node with the next Provision, Upgrade or Install Agent job, and the agent restarts to read it. Unknown keys are refused, so a typo cannot turn a setting off.

```json
{
  "suite_cloud": {"url": "https://cloud.example.com", "secret": "<agent key>:<agent secret>", "requests_per_minute": 300, "records_per_request": 5000, "max_attempts": 8},
  "node": {"source": "stalwart", "host_ip": "203.0.113.5", "hostname": "n1.c1.example.com"},
  "state_dir": "/var/lib/suite-cloud-agent",
  "logs": [{"name": "stalwart", "path": "/var/log/stalwart/stalwart.*", "format": "stalwart", "ship_existing": false, "delete_after_days": 0}],
  "metrics": {"interval_seconds": 60, "mounts": ["/"], "stalwart": {"url": "https://127.0.0.1:443/metrics/prometheus", "username": "telemetry", "password": "<metrics password>", "tls_server_name": "n1.c1.example.com", "verify_tls": true}},
  "quota": {"hook_port": 8095, "hook_token": "<quota hook token>", "store": {"type": "memory"}, "limits_poll_seconds": 60, "usage_push_seconds": 60}
}
```

| Key | Source | Meaning |
| --- | --- | --- |
| `suite_cloud.url` | **Public URL**, else the site's own URL | Where the batches go |
| `suite_cloud.secret` | **Agent Key** and **Agent Secret** of the cluster (a gateway uses its cluster's) | Sent as the bearer token, `<agent key>:<agent secret>` |
| `suite_cloud.requests_per_minute`, `records_per_request` | **Agent Requests per Minute**, **Agent Records per Request** | The agent keeps to the limits the site enforces, for every request it makes |
| `suite_cloud.max_attempts` | **Agent Max Attempts** | Sends of one batch after server or network failures before it is skipped |
| `suite_cloud.request_timeout_seconds` | 60 | Time allowed for one request |
| `node.source`, `node.host_ip`, `node.hostname` | The node record | `host_ip` is the node's IPv4 address, which the dashboards join on |
| `state_dir` | `/var/lib/suite-cloud-agent` | Where the agent keeps `state.json` |
| `log_level` | `info` | `debug`, `info`, `warn` or `error` |
| `logs[].name` | | Unique name; the key of the log's state |
| `logs[].path` | `/var/log/stalwart/stalwart.*` | A glob; the files are read in name order, which for Stalwart is date order |
| `logs[].format` | `stalwart` | The parser. An SFU node will use another format with the same agent |
| `logs[].ship_existing` | **Ship Existing Logs** | Ship the files already on the node the first time the agent starts there. Off: only new lines are shipped |
| `logs[].delete_after_days` | **Delete Shipped Logs After (Days)** | Delete a rotated file this many days after it was shipped whole. 0 keeps the files |
| `logs[].poll_interval_seconds`, `flush_interval_seconds` | 1, 5 | How often a file is checked, and how long a partial batch waits before it is sent |
| `metrics.enabled` | `true` | Host and Stalwart metrics |
| `metrics.interval_seconds` | **Metrics Interval (Seconds)** | One metrics batch per interval |
| `metrics.mounts` | `["/"]` | Mounts reported as `host.disk.*` |
| `metrics.stalwart` | **Enable Prometheus Metrics** and **Metrics Password** of the cluster or gateway | Absent when the cluster has the switch off, and on an outbound-only node, which has no listener |
| `quota.hook_port`, `quota.hook_token` | 8095, **Quota Hook Token** of the cluster | The loopback port and bearer token of the sending-limit hook. The whole section is absent on gateways and outbound-only nodes, which take no client mail, and until the last build is 0.2.0 or newer |
| `quota.store` | The cluster's in-memory store | `{"type": "memory"}`, or `redis`, `redis_cluster` or `redis_sentinel` with the store's `urls` and credentials |
| `quota.limits_poll_seconds`, `usage_push_seconds` | 60, 60 | How often limits are pulled and counts pushed |

`suite-cloud-agent --config <path> --check` validates the file, the state directory and the log paths without a request. `--version` prints the version.

## What it ships

Every line of a Stalwart log file becomes one event: the timestamp, the level in lowercase, the event name in the parentheses, and the `key = value` pairs as `data`, with the line's description as `data.description`. Nested events become objects with `event` and `description`, durations become milliseconds, and a key that appears twice keeps the later value, as in Stalwart's own JSON tracer. Lines written with colour codes by older nodes are read too. A line the parser does not understand is counted and logged, and the agent moves on.

Every interval the agent sends the `host.*` gauges listed in the [telemetry API](api/telemetry.md#machine-metrics), computed from `/proc`, and Stalwart's own metrics from `/metrics/prometheus`, under Stalwart's names: `server_memory` on the endpoint is `server.memory` in Suite Cloud. A histogram's `value` is its mean since Stalwart started, with `count` and `sum` beside it. From the second scrape on, the agent also sends `<name>.p50`, `<name>.p95` and `<name>.p99` gauges: the percentiles of the observations made since the previous scrape, estimated from the histogram's buckets the way Prometheus's `histogram_quantile` does. An interval without observations, or one in which Stalwart restarted, has no percentiles.

## Delivery and state

The agent never ships a line twice and loses none to a restart. For every file it keeps the offset up to which the lines were shipped in `state.json`. Before the first request of a batch it records the batch id and the byte range the batch covers; after a restart it reads that range again and sends it under the same id, which Suite Cloud [deduplicates](api/telemetry.md#retries). Offsets move only once Suite Cloud took the batch or the agent gave it up.

| Answer | What the agent does |
| --- | --- |
| 200 | Commits the batch |
| 429 | Waits for the next minute and sends the same batch again; never counted as an attempt |
| 500 and up, network failure, timeout | Waits, doubling from two seconds to a minute, and tries again; after **Agent Max Attempts** the batch is skipped and logged with its file and offsets |
| 400, 417 | Skips the batch at once and logs the server's message: the same request would fail again |
| 401, 403 | Keeps retrying with a wait of up to five minutes and never skips; a rotated secret is fixed by an Install Agent job, and the Silent Host alert shows the gap |

Logs, metrics and the sending-limit calls share one request budget of **Agent Requests per Minute**, because Suite Cloud counts them all per node. Metrics are points in time: a batch that cannot be sent within an hour is dropped, and nothing is kept across a restart.

A new log file is read from the start; the file before it is finished once it stops growing. With **Delete Shipped Logs After (Days)** set, a rotated file is deleted that many days after it was shipped whole; the newest file and a file with a batch in flight are never deleted. The node's own tmpfiles rule removes log files after 14 days in any case. To ship a node's existing logs after the agent already ran there, stop the agent, delete `/var/lib/suite-cloud-agent/state.json`, turn **Ship Existing Logs** on and install the agent again.

## Running on a node

The service runs as the `stalwart` user, since the log directory belongs to it, and is sandboxed away from Stalwart's configuration and data. `journalctl -u suite-cloud-agent` shows what it does; at `info` it logs the start with the files it found, every file it finishes or deletes and every batch it skips.

Provisioning installs the agent as part of every Provision, Upgrade and Re-provision job of a node or gateway, after Stalwart. The job copies the binary of the node's architecture from the bench host, writes `config.json` and the unit, and restarts the agent when any of them changed. When **Enable Telemetry** is off or no agent build exists, the job stops and disables the agent on the node instead, and an Error Log names the missing build. **Install Agent** on a Stalwart Node or Egress Gateway runs that step on its own, without draining or restarting Stalwart: it rewrites the binary, `config.json` and the unit where they differ from the current build and settings, whatever version the node runs, and restarts the agent in any case. **Show Agent Config** on a Stalwart Node or Egress Gateway shows the `config.json` that job would write, built from the current settings, with every secret masked; it reads nothing from the node. **Upgrade Agents** on a Stalwart Cluster runs it on every Active or Draining node and every Active gateway: a draining node still ships its log. **Enable Telemetry** in Suite Cloud Settings is on by default on a new site. Turning it on queues that job for the same servers of every cluster by itself, and turning it off stops the agents the same way. Without a build at that moment an Error Log asks for Build Agent and Upgrade Agents instead. **Rotate Agent Secret** on a Stalwart Cluster replaces its Agent Secret and runs Upgrade Agents for it; an agent not yet reinstalled is refused and retries, and loses nothing.

Stalwart's Prometheus endpoint is part of the cluster plan. **Enable Prometheus Metrics** on the Stalwart Cluster, on by default, turns it on with the user `telemetry` and the **Metrics Password** generated per cluster and per gateway; off, the exporter is disabled on every node and gateway of the cluster and the agents report host metrics only. A change reaches Stalwart with the next Sync Config, and the agents with the next Install Agent or Upgrade Agents job.

## Sending limits

A Mail Account's **Daily Send Limit** and **Monthly Send Limit** count recipients per UTC day and month; 0 means no limit. Stalwart enforces them through an MTA Hook at the data stage, for signed-in senders only, which covers SMTP submission and JMAP sends. The hook calls the Agent on the same node at `http://127.0.0.1:8095/hooks/quota` with the cluster's **Quota Hook Token**. The Agent reads only the context and the envelope of the request, never the message, and in one atomic step either adds the recipients to the account's day and month counters or, when that would pass a limit, counts the message as refused instead and answers `550 5.4.5` with the limit and when it resets.

| Cluster | Counters and limits |
| --- | --- |
| Single node, no Redis | The Agent's memory, saved to `quota.json` in the state directory every 5 seconds and on shutdown |
| With a Redis in-memory store (every multi-node cluster) | That Redis under `sca:quota:`. A Lua script checks and counts, and every key of an account shares the hash tag `{account}`, so it also works on Redis Cluster |

The Agent pulls the limits changed since its last pull every 60 seconds and all of them on start and every hour; an account that two full pulls in a row do not return, such as a deleted one, loses its limits. One miss is not enough, because a pull leaves out saves younger than five seconds. It pushes the day and month totals of the accounts it counted, sent and refused, every 60 seconds and on shutdown, and keeps them for the next push when one fails. Until a push of the current UTC day goes through, after midnight, after a start or after failed pushes, and in the first ten minutes of every UTC day, each push also sends yesterday's totals (and an account whose yesterday total a failed request held gets it again until it is taken), and the previous month's when a month ended, so what was counted after yesterday's last push is not lost. Suite Cloud takes nothing older than yesterday. On a start the Agent also pushes every account its store counted today or yesterday, asking the store again at each push until it answers, so counts left by a crash are not lost. Both calls are in the [telemetry API](api/telemetry.md#sending-limits). The Mail Account form shows the last pushed totals: orange from 80% of a limit, red at 100%, with the refused messages beside them. Suite Cloud keeps each account's day and month totals for **Sending Usage Retention (Days)** in Suite Cloud Settings (90 by default) and deletes older days, and months that ended before them, daily. In Insights, the **Sending Usage** dashboard of the Stalwart Mail workbook shows recipients and refusals for the latest UTC day, the accounts at or near a limit, the top senders, recipients and refusals per day, and each account's daily history, filtered by tenant, cluster or account. Its "today" is the latest day and month the Agents reported rather than the database's date, so it stays right whatever the database's time zone. Each day and month keeps the limit the account had while it was current, so the history shows how close a past day came to the limit of that day.

Nothing in the quota path stops mail. When the Agent fails or takes longer than 2 seconds, Stalwart accepts the message uncounted. A native throttle, `suite-cloud-quota-backstop`, caps every signed-in account meanwhile at the cluster's **Backstop Messages per Day**, 5,000 by default, and 0 turns it off. It counts messages, not recipients, so keep it above the highest daily limit. A change applies with the next Sync Config. A message the hook accepted stays counted when Stalwart refuses it later, for example over a queue quota.

The hook and the backstop are part of the cluster plan, and both they and the `quota` section of `config.json` wait for **Enable Telemetry** and a build of 0.2.0 or newer. Until then the plan keeps both switched off, so turning telemetry off and running Sync Config turns off a hook installed before. To turn the limits on for a cluster: run **Build Agent**, then **Upgrade Agents**, then **Sync Config**. In the other order nothing breaks, but every message logs a hook error until the Agents are up. A single-node cluster that later gets a Redis in-memory store needs Upgrade Agents again to move its counters there.

## Status

The telemetry API notes the last batch each host sent and the agent version in its `User-Agent`. A Stalwart Node or Egress Gateway form shows it above the fields: green when the agent sent a batch within **Telemetry Silence (Minutes)** and runs the current build, orange when it runs an older build, red when it has been silent longer than that, blue when no batch was recorded yet. A Stalwart Cluster form sums up its Active and Draining nodes and its Active gateways the same way, so Upgrade Agents can be checked at a glance. The note lives in the site's cache: after the cache is cleared it returns with the next batch, within a metrics interval.

## Building

Suite Cloud builds the agent on the bench host with the Go toolchain found on `PATH` or at `/usr/local/go/bin/go`. The build needs Go 1.22 or newer and access to the Go module proxy, `proxy.golang.org` by default: it downloads the one dependency, `go-redis`, checks it against `agent/go.sum` and keeps it under `private/files/agent/.gomodcache`, so later builds need no network. A host behind a proxy sets `GOPROXY` for the bench's workers. Ubuntu 24.04 ships a recent enough `golang-go` package; on older releases, or to pick the version, install the release from [go.dev/dl](https://go.dev/dl/) under `/usr/local`, where Suite Cloud finds it without a `PATH` change:

```sh
sudo apt install golang-go                          # Ubuntu 24.04 or newer
curl -fsSL https://go.dev/dl/go1.22.12.linux-amd64.tar.gz | sudo tar -C /usr/local -xzf -   # any release
/usr/local/go/bin/go version
```

The bench's workers run the build, so the toolchain must be readable by the bench user. **Build Agent** in Suite Cloud Settings queues a build; a migrate queues one when the agent's sources changed since the last build. The build compiles `linux/amd64` and `linux/arm64` into `private/files/agent/<version>/` under the site, records the version in **Built Agent Version**, and keeps the previous build so a job in progress can finish. Provisioning installs the build recorded there, so a source change that is not built yet never stops a running agent. A failure is shown in **Last Build Error** and in an Error Log. The version is the `agent/VERSION` file plus a digest of the sources and `go.sum`, so a source change makes a new build without a bump; the install job compares the binary's checksum with the node's and copies it only when it differs.

## Installing by hand

For a node that Suite Cloud does not provision, copy `private/files/agent/<version>/suite-cloud-agent-linux-<arch>` to `/usr/local/bin/suite-cloud-agent`, write `/etc/suite-cloud-agent/config.json` from the example above with mode 0600, create `/var/lib/suite-cloud-agent` for the service user, and install this unit as `/etc/systemd/system/suite-cloud-agent.service`:

```ini
[Unit]
Description=Suite Cloud Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stalwart
Group=stalwart
ExecStartPre=/usr/local/bin/suite-cloud-agent --config /etc/suite-cloud-agent/config.json --check
ExecStart=/usr/local/bin/suite-cloud-agent --config /etc/suite-cloud-agent/config.json
Restart=always
RestartSec=5
TimeoutStopSec=90
StateDirectory=suite-cloud-agent
StateDirectoryMode=0750
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/log/stalwart
InaccessiblePaths=/etc/stalwart /var/lib/stalwart
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

Then `systemctl enable --now suite-cloud-agent`.
