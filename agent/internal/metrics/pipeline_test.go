package metrics

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/prometheus"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

type recorder struct {
	batches []telemetry.Batch
	block   chan struct{}
}

func (r *recorder) Send(ctx context.Context, batch telemetry.Batch) (telemetry.Outcome, error) {
	if r.block != nil {
		select {
		case <-r.block:
		case <-ctx.Done():
			return 0, ctx.Err()
		}
	}
	r.batches = append(r.batches, batch)
	return telemetry.Sent, nil
}

// fakeHost stands in for /proc, which only Linux has.
type fakeHost struct{}

func (fakeHost) Collect(now time.Time) ([]telemetry.Metric, error) {
	stamp := now.UTC().Format(time.RFC3339)
	var metrics []telemetry.Metric
	for _, name := range []string{"host.memory.used.bytes", "host.memory.total.bytes", "host.load.1m", "host.uptime.seconds"} {
		metrics = append(metrics, telemetry.Metric{Timestamp: stamp, Name: name, Type: "gauge", Value: 1})
	}
	return metrics, nil
}

func pipeline(scraper *prometheus.Scraper) (*Pipeline, *recorder) {
	sender := &recorder{}
	return &Pipeline{
		Host:      fakeHost{},
		Scraper:   scraper,
		Interval:  time.Hour,
		BatchSize: 5000,
		Node:      config.Node{Source: "stalwart", HostIP: "203.0.113.5", Hostname: "n1"},
		Sender:    sender,
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	}, sender
}

func TestTickProducesOneBatchOfHostAndStalwartMetrics(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("# TYPE server_memory gauge\nserver_memory 5\n"))
	}))
	defer server.Close()
	p, _ := pipeline(prometheus.NewScraper(config.StalwartMetrics{URL: server.URL, Username: "u", Password: "p"}))

	batches := p.Tick()
	if len(batches) != 1 || batches[0].Kind != telemetry.Metrics || batches[0].HostIP != "203.0.113.5" {
		t.Fatalf("batches %+v", batches)
	}
	names := map[string]bool{}
	for _, record := range batches[0].Records {
		names[record.(telemetry.Metric).Name] = true
	}
	if !names["host.memory.used.bytes"] || !names["server.memory"] {
		t.Fatalf("names %v", names)
	}
}

func TestScrapeFailureKeepsHostMetrics(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) }))
	defer server.Close()
	p, _ := pipeline(prometheus.NewScraper(config.StalwartMetrics{URL: server.URL, Username: "u", Password: "p"}))
	batches := p.Tick()
	if len(batches) != 1 || len(batches[0].Records) == 0 {
		t.Fatalf("batches %+v", batches)
	}
}

func TestTickSplitsLargeBatches(t *testing.T) {
	p, _ := pipeline(nil)
	p.BatchSize = 3
	batches := p.Tick()
	if len(batches) < 2 {
		t.Fatalf("%d batches", len(batches))
	}
	for _, batch := range batches {
		if len(batch.Records) > 3 {
			t.Fatalf("batch of %d", len(batch.Records))
		}
	}
}

func TestQueueDropsTheOldestWhenFull(t *testing.T) {
	p, sender := pipeline(nil)
	sender.block = make(chan struct{})
	p.queue = make(chan telemetry.Batch, 2)
	for i := 0; i < 3; i++ {
		p.enqueue(telemetry.Batch{ID: string(rune('a' + i))})
	}
	if len(p.queue) != 2 || (<-p.queue).ID != "b" {
		t.Fatal("the oldest batch was not the one dropped")
	}
}

func TestRunDeliversAndStops(t *testing.T) {
	p, sender := pipeline(nil)
	p.Interval = 10 * time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	err := p.Run(ctx)
	if err != context.DeadlineExceeded {
		t.Fatalf("err %v", err)
	}
	if len(sender.batches) == 0 {
		t.Fatal("nothing delivered")
	}
}
