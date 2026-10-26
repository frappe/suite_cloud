package sender

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

// clock stands in for time: sleeping moves it forward at once and records the wait.
type clock struct {
	mu    sync.Mutex
	now   time.Time
	waits []time.Duration
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) Sleep(ctx context.Context, d time.Duration) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	c.waits = append(c.waits, d)
	return nil
}

type request struct {
	path, auth string
	body       []byte
}

func newServer(t *testing.T, statuses ...int) (*httptest.Server, *[]request) {
	t.Helper()
	var requests []request
	var mu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		requests = append(requests, request{r.URL.Path, r.Header.Get("Authorization"), body})
		n := len(requests)
		mu.Unlock()
		status := statuses[min(n, len(statuses))-1]
		w.WriteHeader(status)
		switch status {
		case http.StatusOK:
			var envelope struct {
				BatchID string `json:"batch_id"`
				Records []any  `json:"records"`
			}
			json.Unmarshal(body, &envelope)
			fmt.Fprintf(w, `{"data": {"batch_id": %q, "accepted": %d}}`, envelope.BatchID, len(envelope.Records))
		default:
			fmt.Fprintf(w, `{"errors": [{"type": "SomeError", "message": "status %d"}]}`, status)
		}
	}))
	t.Cleanup(server.Close)
	return server, &requests
}

func newSender(t *testing.T, url string, perMinute, maxAttempts int) (*Sender, *clock) {
	t.Helper()
	c := &clock{now: time.Date(2026, 10, 20, 12, 0, 30, 0, time.UTC)}
	s := New(config.SuiteCloud{URL: url + "/", Secret: "s3cret", RequestsPerMinute: perMinute, RecordsPerRequest: 5000, MaxAttempts: maxAttempts, RequestTimeoutSeconds: 5}, "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	s.now, s.sleep = c.Now, c.Sleep
	s.limiter.now, s.limiter.sleep = c.Now, c.Sleep
	return s, c
}

func batch() telemetry.Batch {
	return telemetry.Batch{ID: "0199f7e0-0000-7000-8000-000000000001", Kind: telemetry.Events, Source: "stalwart", HostIP: "203.0.113.5", Records: []any{telemetry.Event{Timestamp: "2026-10-20T00:00:00Z", Level: "info", Event: "a.b"}}}
}

func TestSentOnFirstTry(t *testing.T) {
	server, requests := newServer(t, 200)
	s, _ := newSender(t, server.URL, 300, 8)
	outcome, err := s.Send(context.Background(), batch())
	if err != nil || outcome != telemetry.Sent {
		t.Fatalf("%v %v", outcome, err)
	}
	r := (*requests)[0]
	if r.path != "/api/v2/method/suite_cloud.api.telemetry.events" || r.auth != "Bearer s3cret" {
		t.Fatalf("request %+v", r)
	}
}

func TestRateLimitedRetryKeepsTheBatchId(t *testing.T) {
	server, requests := newServer(t, 429, 200)
	s, c := newSender(t, server.URL, 300, 8)
	outcome, err := s.Send(context.Background(), batch())
	if err != nil || outcome != telemetry.Sent {
		t.Fatalf("%v %v", outcome, err)
	}
	if len(*requests) != 2 || string((*requests)[0].body) != string((*requests)[1].body) {
		t.Fatalf("requests %+v", *requests)
	}
	if len(c.waits) != 1 || c.waits[0] < 31*time.Second || c.waits[0] > 36*time.Second {
		t.Fatalf("waited %v, want until the next minute", c.waits)
	}
}

func TestServerErrorsAreGivenUpAfterMaxAttempts(t *testing.T) {
	server, requests := newServer(t, 500)
	s, c := newSender(t, server.URL, 300, 4)
	outcome, err := s.Send(context.Background(), batch())
	if err != nil || outcome != telemetry.Skipped {
		t.Fatalf("%v %v", outcome, err)
	}
	if len(*requests) != 4 || len(c.waits) != 3 {
		t.Fatalf("%d requests, waits %v", len(*requests), c.waits)
	}
	for i := 1; i < len(c.waits); i++ {
		if c.waits[i] < c.waits[i-1] {
			t.Fatalf("waits do not grow: %v", c.waits)
		}
	}
}

func TestServerErrorThenSuccess(t *testing.T) {
	server, requests := newServer(t, 503, 502, 200)
	s, _ := newSender(t, server.URL, 300, 8)
	outcome, _ := s.Send(context.Background(), batch())
	if outcome != telemetry.Sent || len(*requests) != 3 {
		t.Fatalf("%v after %d requests", outcome, len(*requests))
	}
}

func TestBadRequestIsSkippedAtOnce(t *testing.T) {
	for _, status := range []int{400, 417, 404} {
		server, requests := newServer(t, status)
		s, _ := newSender(t, server.URL, 300, 8)
		outcome, _ := s.Send(context.Background(), batch())
		if outcome != telemetry.Skipped || len(*requests) != 1 {
			t.Fatalf("%d: %v after %d requests", status, outcome, len(*requests))
		}
	}
}

func TestRefusedSecretHoldsUntilCancelled(t *testing.T) {
	server, requests := newServer(t, 401)
	s, c := newSender(t, server.URL, 300, 2)
	ctx, cancel := context.WithCancel(context.Background())
	stop := 0
	s.sleep = func(ctx context.Context, d time.Duration) error {
		stop++
		if stop == 6 {
			cancel()
		}
		return c.Sleep(ctx, d)
	}
	_, err := s.Send(ctx, batch())
	if err == nil {
		t.Fatal("returned without the context ending")
	}
	if len(*requests) != 6 {
		t.Fatalf("%d requests: a refused secret must keep retrying", len(*requests))
	}
}

func TestNetworkErrorsRetryThenSkip(t *testing.T) {
	server, _ := newServer(t, 200)
	url := server.URL
	server.Close()
	s, c := newSender(t, url, 300, 3)
	outcome, _ := s.Send(context.Background(), batch())
	if outcome != telemetry.Skipped || len(c.waits) != 2 {
		t.Fatalf("%v waits %v", outcome, c.waits)
	}
}

func TestLimiterSharesOneMinuteWindow(t *testing.T) {
	c := &clock{now: time.Date(2026, 10, 20, 12, 0, 10, 0, time.UTC)}
	l := NewLimiter(3)
	l.now, l.sleep = c.Now, c.Sleep
	for i := 0; i < 3; i++ {
		if err := l.Wait(context.Background()); err != nil || len(c.waits) != 0 {
			t.Fatalf("blocked early: %v", c.waits)
		}
	}
	if err := l.Wait(context.Background()); err != nil || len(c.waits) != 1 || c.waits[0] != 50*time.Second {
		t.Fatalf("waits %v", c.waits)
	}
	l.Exhaust(c.Now().Add(63 * time.Second)) // a 429: closed until past the next boundary
	l.Wait(context.Background())
	if len(c.waits) != 2 || c.waits[1] != 63*time.Second {
		t.Fatalf("waits %v", c.waits)
	}
	if l.Wait(context.Background()); len(c.waits) != 2 {
		t.Fatalf("the new window did not open: %v", c.waits)
	}
}

func TestLimiterIsSafeAcrossGoroutines(t *testing.T) {
	c := &clock{now: time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)}
	l := NewLimiter(100)
	l.now, l.sleep = c.Now, c.Sleep
	var wg sync.WaitGroup
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				l.Wait(context.Background())
			}
		}()
	}
	wg.Wait()
	if len(c.waits) != 1 {
		t.Fatalf("200 requests at 100 a minute took %d waits", len(c.waits))
	}
}

func TestErrorMessage(t *testing.T) {
	if got := errorMessage(`{"errors": [{"type": "InvalidInputError", "message": "records[12].timestamp must carry a UTC offset."}]}`); got != "InvalidInputError: records[12].timestamp must carry a UTC offset." {
		t.Fatal(got)
	}
	if got := errorMessage("<html>gateway"); got != "<html>gateway" {
		t.Fatal(got)
	}
}

func TestCallDecodesTheAnswerData(t *testing.T) {
	server, requests := newServer(t, 200)
	s, _ := newSender(t, server.URL, 300, 8)
	var answer struct {
		Accepted int `json:"accepted"`
	}
	payload := map[string]any{"batch_id": "b", "records": []int{1, 2}}
	if err := s.Call(context.Background(), "quota.usage", payload, &answer); err != nil {
		t.Fatal(err)
	}
	r := (*requests)[0]
	if r.path != "/api/v2/method/suite_cloud.api.telemetry.quota.usage" || r.auth != "Bearer s3cret" || answer.Accepted != 2 {
		t.Fatalf("request %+v answer %+v", r, answer)
	}
}

func TestCallMakesOneAttempt(t *testing.T) {
	for _, status := range []int{400, 429, 500} {
		server, requests := newServer(t, status, 200)
		s, _ := newSender(t, server.URL, 300, 8)
		if err := s.Call(context.Background(), "quota.limits", map[string]any{}, &struct{}{}); err == nil {
			t.Fatalf("status %d accepted", status)
		}
		if len(*requests) != 1 {
			t.Fatalf("status %d: %d requests", status, len(*requests))
		}
	}
}
