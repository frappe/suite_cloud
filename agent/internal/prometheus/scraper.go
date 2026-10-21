package prometheus

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net/http"
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
}

func NewScraper(cfg config.StalwartMetrics) *Scraper {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{ServerName: cfg.TLSServerName, InsecureSkipVerify: !cfg.ShouldVerifyTLS()} //nolint:gosec // a dev cluster's staging certificate
	return &Scraper{client: &http.Client{Transport: transport, Timeout: scrapeTimeout}, url: cfg.URL, username: cfg.Username, password: cfg.Password}
}

// Scrape fetches the exposition and returns one record per metric, histograms as one record each.
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
	return Convert(families, now), nil
}

// Convert turns families into API records. A histogram's value is its mean, sum over count: the
// exposition has no last observation.
func Convert(families []Family, now time.Time) []telemetry.Metric {
	stamp := now.UTC().Format(time.RFC3339)
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
			metrics = append(metrics, histograms(family, name, stamp)...)
		}
	}
	return metrics
}

func histograms(family Family, name, stamp string) []telemetry.Metric {
	type totals struct {
		labels map[string]string
		count  int64
		sum    float64
	}
	byLabels := map[string]*totals{}
	var order []string
	for _, sample := range family.Samples {
		labels := labelsOf(sample)
		delete(labels, "le")
		delete(labels, "quantile")
		if len(labels) == 0 {
			labels = nil
		}
		key := labelKey(labels)
		entry := byLabels[key]
		if entry == nil {
			entry = &totals{labels: labels}
			byLabels[key] = entry
			order = append(order, key)
		}
		switch {
		case strings.HasSuffix(sample.Name, "_count"):
			entry.count = int64(sample.Value)
		case strings.HasSuffix(sample.Name, "_sum"):
			entry.sum = sample.Value
		}
	}

	metrics := make([]telemetry.Metric, 0, len(order))
	for _, key := range order {
		entry := byLabels[key]
		mean := 0.0
		if entry.count > 0 {
			mean = entry.sum / float64(entry.count)
		}
		count, sum := entry.count, entry.sum
		metrics = append(metrics, telemetry.Metric{Timestamp: stamp, Name: name, Type: "histogram", Value: mean, Count: &count, Sum: &sum, Labels: entry.labels})
	}
	return metrics
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
