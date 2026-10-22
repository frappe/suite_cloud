// Package metrics samples the host and Stalwart on a timer and ships one metrics batch per tick.
package metrics

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/hostmetrics"
	"github.com/frappe/suite_cloud/agent/internal/prometheus"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

const (
	queueLength  = 60 // an hour of ticks at the default interval
	warnInterval = 5 * time.Minute
)

// Pipeline collects on each tick and hands the batches to the sender through a bounded queue.
// Samples are points in time, so when the queue is full the oldest is dropped, and nothing is
// kept across a restart.
type Pipeline struct {
	Host      *hostmetrics.Collector
	Scraper   *prometheus.Scraper // nil: no Stalwart listener on this node
	Interval  time.Duration
	BatchSize int
	Node      config.Node
	Sender    telemetry.Sender
	Logger    *slog.Logger
	Now       func() time.Time

	queue          chan telemetry.Batch
	lastHostWarn   time.Time
	lastScrapeWarn time.Time
}

func (p *Pipeline) Run(ctx context.Context) error {
	p.queue = make(chan telemetry.Batch, queueLength)
	errs := make(chan error, 1)
	go func() { errs <- p.deliver(ctx) }()

	ticker := time.NewTicker(p.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return <-errs
		case <-ticker.C:
			for _, batch := range p.Tick() {
				p.enqueue(batch)
			}
		}
	}
}

// Tick samples once and returns the batches, split to the batch size.
func (p *Pipeline) Tick() []telemetry.Batch {
	now := p.now()
	records, err := p.Host.Collect(now)
	if err != nil {
		p.warn(&p.lastHostWarn, "some host metrics not read", err)
	}
	if p.Scraper != nil {
		scraped, err := p.Scraper.Scrape(context.Background(), now)
		if err != nil {
			p.warn(&p.lastScrapeWarn, "stalwart metrics not scraped", err)
		}
		records = append(records, scraped...)
	}

	var batches []telemetry.Batch
	for start := 0; start < len(records); start += p.BatchSize {
		end := min(start+p.BatchSize, len(records))
		items := make([]any, 0, end-start)
		for _, record := range records[start:end] {
			items = append(items, record)
		}
		batches = append(batches, telemetry.Batch{ID: telemetry.NewUUIDv7(), Kind: telemetry.Metrics, Source: p.Node.Source, HostIP: p.Node.HostIP, Hostname: p.Node.Hostname, Records: items})
	}
	return batches
}

func (p *Pipeline) enqueue(batch telemetry.Batch) {
	for {
		select {
		case p.queue <- batch:
			return
		default:
			select {
			case dropped := <-p.queue:
				p.Logger.Warn("metrics queue full; oldest batch dropped", "batch_id", dropped.ID, "records", len(dropped.Records))
			default:
			}
		}
	}
}

func (p *Pipeline) deliver(ctx context.Context) error {
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case batch := <-p.queue:
			outcome, err := p.Sender.Send(ctx, batch)
			if err != nil {
				return err
			}
			if outcome == telemetry.Skipped {
				p.Logger.Warn("metrics batch dropped", "batch_id", batch.ID, "records", len(batch.Records))
			} else {
				p.Logger.Debug("metrics batch sent", "batch_id", batch.ID, "records", len(batch.Records))
			}
		}
	}
}

// warn logs a repeating problem once per warnInterval.
func (p *Pipeline) warn(last *time.Time, message string, err error) {
	if p.now().Sub(*last) < warnInterval {
		return
	}
	*last = p.now()
	p.Logger.Warn(message, "error", err)
}

func (p *Pipeline) now() time.Time {
	if p.Now != nil {
		return p.Now()
	}
	return time.Now()
}
