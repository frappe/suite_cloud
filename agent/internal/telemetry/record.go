// Package telemetry holds the record and batch shapes of Suite Cloud's telemetry API.
package telemetry

// Kind names one of the two API methods a batch goes to.
type Kind string

const (
	Events  Kind = "events"
	Metrics Kind = "metrics"
)

// Event is one log or trace record as the API takes it.
type Event struct {
	Timestamp string         `json:"timestamp"`
	Level     string         `json:"level"`
	Event     string         `json:"event"`
	SpanID    any            `json:"span_id,omitempty"`
	Data      map[string]any `json:"data,omitempty"`
}

// Metric is one sample as the API takes it.
type Metric struct {
	Timestamp string            `json:"timestamp"`
	Name      string            `json:"name"`
	Type      string            `json:"type"`
	Value     float64           `json:"value"`
	Count     *int64            `json:"count,omitempty"`
	Sum       *float64          `json:"sum,omitempty"`
	Labels    map[string]string `json:"labels,omitempty"`
}
