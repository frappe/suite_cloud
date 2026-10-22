package telemetry

import (
	"regexp"
	"sort"
	"testing"
)

func TestUUIDv7Shape(t *testing.T) {
	id := NewUUIDv7()
	if !regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`).MatchString(id) {
		t.Fatalf("not a UUIDv7: %s", id)
	}
}

func TestUUIDv7IdsCountUp(t *testing.T) {
	ids := make([]string, 1000)
	for i := range ids {
		ids[i] = NewUUIDv7()
	}
	if !sort.StringsAreSorted(ids) {
		t.Fatal("ids are not in order")
	}
	seen := map[string]bool{}
	for _, id := range ids {
		if seen[id] {
			t.Fatalf("repeated id %s", id)
		}
		seen[id] = true
	}
}

func TestMarshalBody(t *testing.T) {
	batch := Batch{ID: "x", Kind: Events, Source: "stalwart", HostIP: "203.0.113.5", Records: []any{Event{Timestamp: "t", Level: "info", Event: "a.b"}}}
	body, err := batch.MarshalBody()
	if err != nil {
		t.Fatal(err)
	}
	want := `{"source":"stalwart","host_ip":"203.0.113.5","batch_id":"x","records":[{"timestamp":"t","level":"info","event":"a.b"}]}`
	if string(body) != want {
		t.Fatalf("body %s", body)
	}
}
