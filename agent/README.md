# Suite Cloud Agent

The agent runs on every Stalwart node and egress gateway as a systemd service. It tails Stalwart's log files, samples the machine, scrapes Stalwart's Prometheus endpoint and pushes batches to the [telemetry API](../docs/api/telemetry.md). On a Stalwart node it also answers the sending-limit hook (`internal/quota`). [docs/agent.md](../docs/agent.md) describes the configuration, the state it keeps and how Suite Cloud builds and installs it.

The only dependency is `github.com/redis/go-redis/v9`, for the quota counters on multi-node clusters. It is vendored, so a build needs no network. After a dependency change, run `go mod tidy && go mod vendor` and commit `vendor/`.

```sh
go test -race ./...
SCA_TEST_REDIS_URL=redis://localhost:6379/7 go test -race ./internal/quota/  # also tests the Redis store
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w -X main.version=$(cat VERSION)" -o suite-cloud-agent ./cmd/suite-cloud-agent
./suite-cloud-agent --config config.json --check
```

`VERSION` is the version operators see. Suite Cloud appends a digest of the sources when it builds, so a change here rolls out without a bump.

A new log format is a package under `internal/format` with a `Parse(line []byte) (telemetry.Event, error)` function, named in `format.Lookup`, and a `format` value in `config.json`.
