package telemetry

import (
	"context"
	"encoding/json"
)

// Batch is one request to the API: the envelope plus up to records_per_request records.
type Batch struct {
	ID       string
	Kind     Kind
	Source   string
	HostIP   string
	Hostname string
	Records  []any
}

// Outcome is how a send ended; a batch is never left in between.
type Outcome int

const (
	Sent    Outcome = iota // accepted by Suite Cloud
	Skipped                // given up on; the caller moves past it
)

func (o Outcome) String() string {
	if o == Sent {
		return "sent"
	}
	return "skipped"
}

type envelope struct {
	Source   string `json:"source"`
	HostIP   string `json:"host_ip"`
	Hostname string `json:"hostname,omitempty"`
	BatchID  string `json:"batch_id"`
	Records  []any  `json:"records"`
}

// MarshalBody encodes the request body. It is built once per batch so retries resend the same
// bytes under the same batch id.
func (b Batch) MarshalBody() ([]byte, error) {
	return json.Marshal(envelope{b.Source, b.HostIP, b.Hostname, b.ID, b.Records})
}

// Sender delivers one batch and reports how it ended. An error means the context is done.
type Sender interface {
	Send(ctx context.Context, batch Batch) (Outcome, error)
}
