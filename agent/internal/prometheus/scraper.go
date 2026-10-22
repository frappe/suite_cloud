package prometheus

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

const scrapeTimeout = 10 * time.Second

// Scraper reads Stalwart's /metrics/prometheus on this node. The listener serves the cluster's
// certificate, so the request names the node's hostname for TLS even though it goes to loopback.
type Scraper struct {
	client   *http.Client
	url      string
	username string
	password string
	previous map[string]Buckets
}

func NewScraper(cfg config.StalwartMetrics) *Scraper {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{ServerName: cfg.TLSServerName, InsecureSkipVerify: !cfg.ShouldVerifyTLS()} //nolint:gosec // a dev cluster's staging certificate
	return &Scraper{client: &http.Client{Transport: transport, Timeout: scrapeTimeout}, url: cfg.URL, username: cfg.Username, password: cfg.Password}
}

// Scrape fetches the exposition and returns one record per metric, histograms as one record each
// plus their percentiles since the previous scrape.
func (s *Scraper) Scrape(ctx context.Context, now time.Time) ([]telemetry.Metric, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, s.url, nil)
	if err != nil {
		return nil, err
	}
	request.SetBasicAuth(s.username, s.password)
	response, err := s.client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s answered %d", s.url, response.StatusCode)
	}
	text, err := io.ReadAll(io.LimitReader(response.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	families, err := Parse(text)
	if err != nil {
		return nil, err
	}
	var metrics []telemetry.Metric
	metrics, s.previous = Convert(families, now, s.previous)
	return metrics, nil
}

// Buckets is a histogram's cumulative bucket counts at one scrape. Stalwart's histograms count
// since the process started, so the percentiles of one interval come from the difference between
// two scrapes, as Prometheus's histogram_quantile over a rate does.
type Buckets struct {
	Bounds []float64 // upper bounds, ascending, +Inf last
	Counts []float64 // cumulative, one per bound
	Count  int64
}

var percentiles = []struct {
	suffix string
	q      float64
}{{".p50", 0.5}, {".p95", 0.95}, {".p99", 0.99}}

// Convert turns families into API records. A histogram's value is its mean, sum over count, with
// the count and sum beside it; given the previous scrape's buckets, it also yields `<name>.p50`,
// `.p95` and `.p99` gauges for the observations made since. The returned map is the input for
// the next call.
func Convert(families []Family, now time.Time, previous map[string]Buckets) ([]telemetry.Metric, map[string]Buckets) {
	stamp := now.UTC().Format(time.RFC3339)
	current := map[string]Buckets{}
	var metrics []telemetry.Metric
	for _, family := range families {
		name := ToStalwartID(family.Name)
		switch family.Type {
		case "counter", "gauge", "untyped":
			kind := family.Type
			if kind == "untyped" {
				kind = "gauge"
			}
			for _, sample := range family.Samples {
				metrics = append(metrics, telemetry.Metric{Timestamp: stamp, Name: name, Type: kind, Value: sample.Value, Labels: labelsOf(sample)})
			}
		case "histogram", "summary":
			metrics = append(metrics, histograms(family, name, stamp, previous, current)...)
		}
	}
	return metrics, current
}

type histogram struct {
	labels map[string]string
	count  int64
	sum    float64
	bounds []float64
	counts []float64
}

func histograms(family Family, name, stamp string, previous, current map[string]Buckets) []telemetry.Metric {
	byLabels := map[string]*histogram{}
	var order []string
	for _, sample := range family.Samples {
		labels := labelsOf(sample)
		le, isBucket := labels["le"]
		delete(labels, "le")
		delete(labels, "quantile")
		if len(labels) == 0 {
			labels = nil
		}
		key := labelKey(labels)
		entry := byLabels[key]
		if entry == nil {
			entry = &histogram{labels: labels}
			byLabels[key] = entry
			order = append(order, key)
		}
		switch {
		case strings.HasSuffix(sample.Name, "_count"):
			entry.count = int64(sample.Value)
		case strings.HasSuffix(sample.Name, "_sum"):
			entry.sum = sample.Value
		case isBucket:
			if bound, err := strconv.ParseFloat(le, 64); err == nil {
				entry.bounds = append(entry.bounds, bound)
				entry.counts = append(entry.counts, sample.Value)
			}
		}
	}

	var metrics []telemetry.Metric
	for _, key := range order {
		entry := byLabels[key]
		mean := 0.0
		if entry.count > 0 {
			mean = entry.sum / float64(entry.count)
		}
		count, sum := entry.count, entry.sum
		metrics = append(metrics, telemetry.Metric{Timestamp: stamp, Name: name, Type: "histogram", Value: mean, Count: &count, Sum: &sum, Labels: entry.labels})

		buckets := entry.sorted()
		current[name+"|"+key] = buckets
		deltas, ok := buckets.since(previous[name+"|"+key])
		if !ok {
			continue
		}
		for _, p := range percentiles {
			value := Quantile(p.q, buckets.Bounds, deltas)
			metrics = append(metrics, telemetry.Metric{Timestamp: stamp, Name: name + p.suffix, Type: "gauge", Value: value, Labels: entry.labels})
		}
	}
	return metrics
}

func (h *histogram) sorted() Buckets {
	order := make([]int, len(h.bounds))
	for i := range order {
		order[i] = i
	}
	sort.Slice(order, func(a, b int) bool { return h.bounds[order[a]] < h.bounds[order[b]] })
	buckets := Buckets{Count: h.count}
	for _, i := range order {
		buckets.Bounds = append(buckets.Bounds, h.bounds[i])
		buckets.Counts = append(buckets.Counts, h.counts[i])
	}
	return buckets
}

// since returns the cumulative counts of the observations made after `previous`. There are none
// to report on the first scrape, after a restart of Stalwart (a count went down), when the
// buckets changed, or when nothing was observed.
func (b Buckets) since(previous Buckets) ([]float64, bool) {
	if len(b.Bounds) < 2 || len(previous.Bounds) != len(b.Bounds) || b.Count <= previous.Count {
		return nil, false
	}
	deltas := make([]float64, len(b.Counts))
	for i := range b.Counts {
		if b.Bounds[i] != previous.Bounds[i] || b.Counts[i] < previous.Counts[i] {
			return nil, false
		}
		deltas[i] = b.Counts[i] - previous.Counts[i]
		if i > 0 && deltas[i] < deltas[i-1] {
			return nil, false
		}
	}
	if deltas[len(deltas)-1] <= 0 {
		return nil, false
	}
	return deltas, true
}

// Quantile estimates the q-th quantile from cumulative bucket counts the way Prometheus's
// histogram_quantile does: linear interpolation inside the bucket the rank falls in, the lower
// bound of the +Inf bucket when it falls there.
func Quantile(q float64, bounds, counts []float64) float64 {
	total := counts[len(counts)-1]
	rank := q * total
	b := sort.SearchFloat64s(counts, rank)
	if b == len(counts)-1 {
		return bounds[len(bounds)-2]
	}
	lower, before := 0.0, 0.0
	if b > 0 {
		lower, before = bounds[b-1], counts[b-1]
	}
	width := counts[b] - before
	if width <= 0 {
		return bounds[b]
	}
	return lower + (bounds[b]-lower)*(rank-before)/width
}

func labelsOf(sample Sample) map[string]string {
	if len(sample.Labels) == 0 {
		return nil
	}
	labels := make(map[string]string, len(sample.Labels))
	for k, v := range sample.Labels {
		labels[k] = v
	}
	return labels
}

func labelKey(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	parts := make([]string, 0, len(labels))
	for k, v := range labels {
		parts = append(parts, k+"="+v)
	}
	// Deterministic enough for grouping: Stalwart emits no labels; this keeps any that come.
	for i := 1; i < len(parts); i++ {
		for j := i; j > 0 && parts[j] < parts[j-1]; j-- {
			parts[j], parts[j-1] = parts[j-1], parts[j]
		}
	}
	return strings.Join(parts, ",")
}
