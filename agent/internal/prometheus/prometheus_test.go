package prometheus

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

const exposition = `# HELP server_memory Server memory usage
# TYPE server_memory gauge
server_memory 123456
# TYPE smtp_active_connections gauge
smtp_active_connections 7
# TYPE delivery_total counter
delivery_total 42
# TYPE delivery_total_time histogram
delivery_total_time_bucket{le="100"} 1
delivery_total_time_bucket{le="1000"} 3
delivery_total_time_bucket{le="+Inf"} 4
delivery_total_time_sum 2200
delivery_total_time_count 4
# TYPE store_read_time histogram
store_read_time_sum 0
store_read_time_count 0
queue_count{queue="default", kind="a,b"} 9
`

func TestConvertExposition(t *testing.T) {
	families, err := Parse([]byte(exposition))
	if err != nil {
		t.Fatal(err)
	}
	metrics := Convert(families, time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC))
	byName := map[string]int{}
	for i, m := range metrics {
		byName[m.Name] = i
	}
	if m := metrics[byName["server.memory"]]; m.Type != "gauge" || m.Value != 123456 || m.Timestamp != "2026-10-20T00:00:00Z" {
		t.Fatalf("%+v", m)
	}
	if m := metrics[byName["smtp.active-connections"]]; m.Value != 7 {
		t.Fatalf("%+v", m)
	}
	if m := metrics[byName["delivery.total"]]; m.Type != "counter" || m.Value != 42 {
		t.Fatalf("%+v", m)
	}
	if m := metrics[byName["delivery.total-time"]]; m.Type != "histogram" || m.Value != 550 || *m.Count != 4 || *m.Sum != 2200 || m.Labels != nil {
		t.Fatalf("%+v", m)
	}
	if m := metrics[byName["store.read-time"]]; m.Value != 0 || *m.Count != 0 {
		t.Fatalf("%+v", m)
	}
	if m := metrics[byName["queue.count"]]; m.Labels["queue"] != "default" || m.Labels["kind"] != "a,b" {
		t.Fatalf("%+v", m)
	}
	if len(metrics) != 6 {
		t.Fatalf("%d metrics", len(metrics))
	}
}

func TestParseRejectsGarbage(t *testing.T) {
	for _, text := range []string{"server_memory", "server_memory abc", "# TYPE x", `a{b="c} 1`} {
		if _, err := Parse([]byte(text)); err == nil {
			t.Fatalf("%q parsed", text)
		}
	}
}

func TestToStalwartID(t *testing.T) {
	cases := map[string]string{
		"server_memory": "server.memory", "delivery_total_time": "delivery.total-time",
		"smtp_active_connections": "smtp.active-connections", "queue_count": "queue.count", "plain": "plain",
	}
	for in, want := range cases {
		if got := ToStalwartID(in); got != want {
			t.Errorf("%s -> %s, want %s", in, got, want)
		}
	}
}

func TestScrapeUsesBasicAuthOverTLS(t *testing.T) {
	var seenUser, seenPass string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenUser, seenPass, _ = r.BasicAuth()
		w.Write([]byte("# TYPE server_memory gauge\nserver_memory 5\n"))
	}))
	server.Config.ErrorLog = log.New(io.Discard, "", 0) // the strict scrape below fails the handshake on purpose
	defer server.Close()
	verify := false
	scraper := NewScraper(config.StalwartMetrics{URL: server.URL + "/metrics/prometheus", Username: "telemetry", Password: "p", TLSServerName: "n1.example.test", VerifyTLS: &verify})

	metrics, err := scraper.Scrape(context.Background(), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if seenUser != "telemetry" || seenPass != "p" || len(metrics) != 1 || metrics[0].Name != "server.memory" {
		t.Fatalf("%s %s %+v", seenUser, seenPass, metrics)
	}

	verify = true
	strict := NewScraper(config.StalwartMetrics{URL: server.URL, Username: "telemetry", Password: "p", TLSServerName: "n1.example.test", VerifyTLS: &verify})
	if _, err := strict.Scrape(context.Background(), time.Now()); err == nil {
		t.Fatal("a self-signed certificate was accepted with verify_tls on")
	}
}

func TestScrapeReportsStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(401) }))
	defer server.Close()
	scraper := NewScraper(config.StalwartMetrics{URL: server.URL, Username: "u", Password: "p"})
	if _, err := scraper.Scrape(context.Background(), time.Now()); err == nil {
		t.Fatal("401 accepted")
	}
}
