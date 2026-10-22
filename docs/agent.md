# Suite Cloud Agent

The Suite Cloud Agent is one Go binary, `suite-cloud-agent`, that runs as a systemd service on every Stalwart node and egress gateway. It tails Stalwart's log files, samples the machine, scrapes Stalwart's Prometheus endpoint and pushes the records to the [telemetry API](api/telemetry.md). Its source is the `agent/` folder of this repository. Suite Cloud builds it on the bench host and installs it with the node's provisioning job.

## Configuration

The agent starts from `/etc/suite-cloud-agent/config.json`. Provisioning writes the file from Suite Cloud Settings and the node record; a change to either reaches the node with the next Provision, Upgrade or Install Agent job, and the agent restarts to read it. Unknown keys are refused, so a typo cannot turn a setting off.

```json
{
  "suite_cloud": {"url": "https://cloud.example.com", "secret": "<telemetry secret>", "requests_per_minute": 300, "records_per_request": 5000, "max_attempts": 8},
  "node": {"source": "stalwart", "host_ip": "203.0.113.5", "hostname": "n1.c1.example.com"},
  "state_dir": "/var/lib/suite-cloud-agent",
  "logs": [{"name": "stalwart", "path": "/var/log/stalwart/stalwart.*", "format": "stalwart", "ship_existing": false, "delete_after_days": 0}],
  "metrics": {"interval_seconds": 60, "mounts": ["/"], "stalwart": {"url": "https://127.0.0.1:443/metrics/prometheus", "username": "telemetry", "password": "<metrics password>", "tls_server_name": "n1.c1.example.com", "verify_tls": true}}
}
```

| Key | Source | Meaning |
| --- | --- | --- |
| `suite_cloud.url` | **Public URL**, else the site's own URL | Where the batches go |
| `suite_cloud.secret` | **Telemetry Secret** | Sent as the bearer token |
| `suite_cloud.requests_per_minute`, `records_per_request` | **Telemetry Requests per Minute**, **Telemetry Records per Request** | The agent keeps to the limits the site enforces |
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

`suite-cloud-agent --config <path> --check` validates the file, the state directory and the log paths without a request. `--version` prints the version.

## What it ships

Every line of a Stalwart log file becomes one event: the timestamp, the level in lowercase, the event name in the parentheses, and the `key = value` pairs as `data`, with the line's description as `data.description`. Nested events become objects with `event` and `description`, durations become milliseconds, and a key that appears twice keeps the later value, as in Stalwart's own JSON tracer. Lines written with colour codes by older nodes are read too. A line the parser does not understand is counted and logged, and the agent moves on.

Every interval the agent sends the `host.*` gauges listed in the [telemetry API](api/telemetry.md#machine-metrics), computed from `/proc`, and Stalwart's own metrics from `/metrics/prometheus`, under Stalwart's names: `server_memory` on the endpoint is `server.memory` in Suite Cloud. A histogram's `value` is its mean, with `count` and `sum` beside it.

## Delivery and state

The agent never ships a line twice and loses none to a restart. For every file it keeps the offset up to which the lines were shipped in `state.json`. Before the first request of a batch it records the batch id and the byte range the batch covers; after a restart it reads that range again and sends it under the same id, which Suite Cloud [deduplicates](api/telemetry.md#retries). Offsets move only once Suite Cloud took the batch or the agent gave it up.

| Answer | What the agent does |
| --- | --- |
| 200 | Commits the batch |
| 429 | Waits for the next minute and sends the same batch again; never counted as an attempt |
| 500 and up, network failure, timeout | Waits, doubling from two seconds to a minute, and tries again; after **Agent Max Attempts** the batch is skipped and logged with its file and offsets |
| 400, 417 | Skips the batch at once and logs the server's message: the same request would fail again |
| 401, 403 | Keeps retrying with a wait of up to five minutes and never skips; a rotated secret is fixed by an Install Agent job, and the Silent Host alert shows the gap |

Logs and metrics share one request budget of **Telemetry Requests per Minute**, because Suite Cloud counts both per node. Metrics are points in time: a batch that cannot be sent within an hour is dropped, and nothing is kept across a restart.

A new log file is read from the start; the file before it is finished once it stops growing. With **Delete Shipped Logs After (Days)** set, a rotated file is deleted that many days after it was shipped whole; the newest file and a file with a batch in flight are never deleted. The node's own tmpfiles rule removes log files after 14 days in any case. To ship a node's existing logs after the agent already ran there, stop the agent, delete `/var/lib/suite-cloud-agent/state.json`, turn **Ship Existing Logs** on and install the agent again.

## Running on a node

The service runs as the `stalwart` user, since the log directory belongs to it, and is sandboxed away from Stalwart's configuration and data. `journalctl -u suite-cloud-agent` shows what it does; at `info` it logs the start with the files it found, every file it finishes or deletes and every batch it skips.

Provisioning installs the agent as part of every Provision, Upgrade and Re-provision job of a node or gateway, after Stalwart. The job copies the binary of the node's architecture from the bench host, writes `config.json` and the unit, and restarts the agent when any of them changed. When the **Telemetry Secret** is empty or no agent build exists, the job stops and disables the agent on the node instead, and an Error Log names the missing build. **Install Agent** on a Stalwart Node or Egress Gateway runs that step on its own, without draining or restarting Stalwart: it rewrites the binary, `config.json` and the unit where they differ from the current build and settings, whatever version the node runs, and restarts the agent in any case. **Upgrade Agents** on a Stalwart Cluster runs it on every Active node and gateway. After a secret rotation, run Upgrade Agents within the day the previous secret stays accepted.

Stalwart's Prometheus endpoint is part of the cluster plan. **Enable Prometheus Metrics** on the Stalwart Cluster, on by default, turns it on with the user `telemetry` and the **Metrics Password** generated per cluster and per gateway; off, the exporter is disabled on every node and gateway of the cluster and the agents report host metrics only. A change reaches Stalwart with the next Sync Config, and the agents with the next Install Agent or Upgrade Agents job.

## Building

Suite Cloud builds the agent on the bench host with the Go toolchain found on `PATH` or at `/usr/local/go/bin/go`. The build needs Go 1.22 or newer and nothing else: the module has no dependencies, so it compiles offline. Ubuntu 24.04 ships a recent enough `golang-go` package; on older releases, or to pick the version, install the release from [go.dev/dl](https://go.dev/dl/) under `/usr/local`, where Suite Cloud finds it without a `PATH` change:

```sh
sudo apt install golang-go                          # Ubuntu 24.04 or newer
curl -fsSL https://go.dev/dl/go1.22.12.linux-amd64.tar.gz | sudo tar -C /usr/local -xzf -   # any release
/usr/local/go/bin/go version
```

The bench's workers run the build, so the toolchain must be readable by the bench user. **Build Agent** in Suite Cloud Settings queues a build; a migrate queues one when the agent's sources changed since the last build. The build compiles `linux/amd64` and `linux/arm64` into `private/files/agent/<version>/` under the site, records the version in **Built Agent Version**, and keeps the previous build so a job in progress can finish. Provisioning installs the build recorded there, so a source change that is not built yet never stops a running agent. A failure is shown in **Last Build Error** and in an Error Log. The version is the `agent/VERSION` file plus a digest of the sources, so a source change makes a new build without a bump; the install job compares the binary's checksum with the node's and copies it only when it differs.

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
