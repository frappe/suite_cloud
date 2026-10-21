package stalwart

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

const delivered = `2026-10-20T10:40:43Z INFO Message delivered (delivery.delivered) queueId = 335988624854242304, queueName = "default", from = "sender@example.org", to = ["one@example.net", "two@example.net"], size = 2178, total = 1, hostname = "mx.example.net", to = "one@example.net", code = 250, details = "Message queued with id 4a9adbd345c9e00.", elapsed = 4541ms`

func TestParseDeliveredLine(t *testing.T) {
	event := mustParse(t, delivered)

	if event.Timestamp != "2026-10-20T10:40:43Z" || event.Level != "info" || event.Event != "delivery.delivered" {
		t.Fatalf("header: %+v", event)
	}
	if event.SpanID != nil {
		t.Fatalf("span id %v: the text log carries none", event.SpanID)
	}
	want := map[string]any{
		"description": "Message delivered",
		"queueId":     int64(335988624854242304),
		"queueName":   "default",
		"from":        "sender@example.org",
		"to":          "one@example.net", // the event's own key wins over the span's list
		"size":        int64(2178),
		"total":       int64(1),
		"hostname":    "mx.example.net",
		"code":        int64(250),
		"details":     "Message queued with id 4a9adbd345c9e00.",
		"elapsed":     int64(4541),
	}
	if !reflect.DeepEqual(event.Data, want) {
		t.Fatalf("data\n got %#v\nwant %#v", event.Data, want)
	}
}

func TestParseValues(t *testing.T) {
	cases := []struct {
		name   string
		fields string
		want   map[string]any
	}{
		{"nested twice", `result = IPREV permanent error (iprev.perm-error) { causedBy = DNS record not found (mail-auth.dns-record-not-found) { code = "Non-Existent Domain" } }, elapsed = 1491ms`,
			map[string]any{
				"result": map[string]any{
					"event": "iprev.perm-error", "description": "IPREV permanent error",
					"causedBy": map[string]any{
						"event": "mail-auth.dns-record-not-found", "description": "DNS record not found",
						"code": "Non-Existent Domain",
					},
				},
				"elapsed": int64(1491),
			}},
		{"nested without braces", `result = DMARC check passed (dmarc.pass), elapsed = 36ms`,
			map[string]any{"result": map[string]any{"event": "dmarc.pass", "description": "DMARC check passed"}, "elapsed": int64(36)}},
		{"array of nested events", `strict = false, result = [DKIM verification passed (dkim.pass) { domain = "example.org" }], elapsed = 16ms`,
			map[string]any{
				"strict":  false,
				"result":  []any{map[string]any{"event": "dkim.pass", "description": "DKIM verification passed", "domain": "example.org"}},
				"elapsed": int64(16),
			}},
		{"bare timestamp and ip", `due = 2026-10-20T00:09:56Z, remoteIp = 203.0.113.9, remoteIp6 = 2001:db8::1`,
			map[string]any{"due": "2026-10-20T00:09:56Z", "remoteIp": "203.0.113.9", "remoteIp6": "2001:db8::1"}},
		{"escapes", `details = "a\tb\\c\nd"`, map[string]any{"details": "a\tb\\c\nd"}},
		{"unescaped quote inside a string", `details = "said "hi", then "bye"", code = 1`,
			map[string]any{"details": `said "hi", then "bye"`, "code": int64(1)}},
		{"scalars", `a = (null), b = 0.75, c = -3, d = 18446744073709551615, e = base64:AAEC, f = [], g = [1, 2], h = true, i = inf`,
			map[string]any{"a": nil, "b": 0.75, "c": int64(-3), "d": uint64(18446744073709551615), "e": "base64:AAEC", "f": []any{}, "g": []any{int64(1), int64(2)}, "h": true, "i": "inf"}},
		{"string in array with comma", `to = ["a, b@example.org", "c@example.org"]`,
			map[string]any{"to": []any{"a, b@example.org", "c@example.org"}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			event := mustParse(t, "2026-10-20T00:00:00Z INFO Something happened (test.event) "+c.fields)
			c.want["description"] = "Something happened"
			if !reflect.DeepEqual(event.Data, c.want) {
				t.Fatalf("data\n got %#v\nwant %#v", event.Data, c.want)
			}
			if _, err := json.Marshal(event); err != nil {
				t.Fatalf("not JSON: %v", err)
			}
		})
	}
}

func TestParseLinesWithoutFields(t *testing.T) {
	for _, line := range []string{
		"2026-10-13T06:02:04Z WARN Model not found (spam.model-not-found) ",
		"2026-10-13T06:02:04Z WARN Model not found (spam.model-not-found)",
		"2026-10-13T06:02:04Z WARN Model not found (spam.model-not-found)\r\n",
	} {
		event := mustParse(t, line)
		if event.Level != "warn" || event.Event != "spam.model-not-found" {
			t.Fatalf("%q: %+v", line, event)
		}
		if !reflect.DeepEqual(event.Data, map[string]any{"description": "Model not found"}) {
			t.Fatalf("%q: data %#v", line, event.Data)
		}
	}
}

func TestParseStripsColour(t *testing.T) {
	coloured := "\x1b[37m2026-06-26T18:10:30Z\x1b[0m \x1b[32;1mINFO\x1b[0m \x1b[37;1mStarting Stalwart Server\x1b[0m (server.startup) \x1b[36mhostname\x1b[0m = \"mail.example.net\", \x1b[36mversion\x1b[0m = \"0.16.11\""
	plain := `2026-06-26T18:10:30Z INFO Starting Stalwart Server (server.startup) hostname = "mail.example.net", version = "0.16.11"`

	if got, want := mustParse(t, coloured), mustParse(t, plain); !reflect.DeepEqual(got, want) {
		t.Fatalf("coloured %+v\nplain %+v", got, want)
	}
}

func TestParseLevels(t *testing.T) {
	for _, level := range []string{"TRACE", "DEBUG", "INFO", "WARN", "ERROR"} {
		event := mustParse(t, "2026-10-20T00:00:00Z "+level+" Thing (a.b)")
		if event.Level != strings.ToLower(level) {
			t.Fatalf("%s -> %q", level, event.Level)
		}
	}
}

func TestParseDescriptionWithParentheses(t *testing.T) {
	event := mustParse(t, `2026-10-20T00:00:00Z INFO Rejected (policy) by filter (smtp.rejected) code = 550`)
	if event.Event != "smtp.rejected" || event.Data["description"] != "Rejected (policy) by filter" {
		t.Fatalf("%+v", event)
	}
}

func TestParseRejectsOtherLines(t *testing.T) {
	for _, line := range []string{
		"",
		"not a log line",
		"2026-10-20T00:00:00Z INFO no event name here",
		"2026-10-20T00:00:00 INFO Thing (a.b)",
		"2026-10-20T00:00:00Z NOTICE Thing (a.b)",
		"2026-10-20T00:00:00Z INFO Thing (a.b) = broken",
		"2026-10-20T00:00:00Z INFO Thing (a.b) key = [1, 2",
	} {
		if _, err := Parse([]byte(line)); err == nil {
			t.Fatalf("%q parsed", line)
		}
	}
}

func FuzzParse(f *testing.F) {
	f.Add([]byte(delivered))
	f.Add([]byte(`2026-10-20T00:00:00Z INFO T (a.b) r = X (c.d) { e = [F (g.h) { i = "j" }] }`))
	f.Add([]byte(`2026-10-20T00:00:00Z WARN T (a.b) `))
	f.Fuzz(func(t *testing.T, line []byte) {
		event, err := Parse(line)
		if err == nil {
			if _, err := json.Marshal(event); err != nil {
				t.Fatalf("not JSON: %v", err)
			}
		}
	})
}

// TestRealLogs reads every stalwart.* file under $STALWART_LOG_DIR and fails on a line the parser
// does not take. It is skipped without the variable: production logs are not committed.
func TestRealLogs(t *testing.T) {
	dir := os.Getenv("STALWART_LOG_DIR")
	if dir == "" {
		t.Skip("STALWART_LOG_DIR not set")
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*", "stalwart.*"))
	more, _ := filepath.Glob(filepath.Join(dir, "stalwart.*"))
	lines, failed := 0, 0
	for _, path := range append(files, more...) {
		handle, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		reader := bufio.NewReaderSize(handle, 1<<20)
		for {
			line, err := reader.ReadBytes('\n')
			if len(line) > 0 {
				lines++
				if _, perr := Parse(line); perr != nil {
					failed++
					if failed <= 10 {
						t.Errorf("%s: %v\n%s", filepath.Base(path), perr, line)
					}
				}
			}
			if err != nil {
				break
			}
		}
		handle.Close()
	}
	t.Logf("%d lines, %d not parsed", lines, failed)
}

func mustParse(t *testing.T, line string) telemetry.Event {
	t.Helper()
	event, err := Parse([]byte(line))
	if err != nil {
		t.Fatalf("%q: %v", line, err)
	}
	return event
}
