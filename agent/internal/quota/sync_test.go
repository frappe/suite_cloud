package quota

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"sort"
	"testing"
	"time"
)

// fakeCaller answers each method from a queue of JSON answers and records the payloads.
type fakeCaller struct {
	answers  map[string][]string
	payloads map[string][]map[string]any
	fail     bool
}

func (f *fakeCaller) Call(_ context.Context, method string, payload, out any) error {
	if f.payloads == nil {
		f.payloads = map[string][]map[string]any{}
	}
	raw, _ := json.Marshal(payload)
	var recorded map[string]any
	json.Unmarshal(raw, &recorded)
	f.payloads[method] = append(f.payloads[method], recorded)
	if f.fail {
		return errors.New("suite cloud down")
	}
	queue := f.answers[method]
	if len(queue) == 0 {
		return json.Unmarshal([]byte(`{}`), out)
	}
	f.answers[method] = queue[1:]
	return json.Unmarshal([]byte(queue[0]), out)
}

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

func newLimits(t *testing.T, caller Caller) (*Limits, *MemoryStore) {
	store, _ := OpenMemory(t.TempDir())
	return &Limits{Store: store, Caller: caller, HostIP: "203.0.113.5", Logger: quiet, Now: time.Now}, store
}

func TestPullReadsEveryPageAndRemembersTheVersion(t *testing.T) {
	caller := &fakeCaller{answers: map[string][]string{limitsMethod: {
		`{"limits": [{"account": "A@example.com", "daily": 5, "monthly": 0, "version": "v1"}], "next_version": "v1", "has_more": true}`,
		`{"limits": [{"account": "b@example.com", "daily": 0, "monthly": 9, "version": "v2"}], "next_version": "v2", "has_more": false}`,
		`{"limits": [], "next_version": "v2", "has_more": false}`,
	}}}
	limits, store := newLimits(t, caller)
	if err := limits.Pull(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if store.data.Limits["a@example.com"].Daily != 5 || store.data.Limits["b@example.com"].Monthly != 9 {
		t.Fatalf("limits %+v", store.data.Limits)
	}
	if err := limits.Pull(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	sent := caller.payloads[limitsMethod]
	if sent[0]["since_version"] != "" || sent[1]["since_version"] != "v1" || sent[2]["since_version"] != "v2" || sent[2]["host_ip"] != "203.0.113.5" {
		t.Fatalf("payloads %v", sent)
	}
}

func TestFullPullClearsAccountsMissedTwiceInARow(t *testing.T) {
	kept := `{"limits": [{"account": "kept@example.com", "daily": 5, "monthly": 0, "version": "v1"}], "next_version": "v1", "has_more": false}`
	settled := `{"limits": [{"account": "kept@example.com", "daily": 5, "monthly": 0, "version": "v1"}, {"account": "saved@example.com", "daily": 4, "monthly": 0, "version": "v2"}], "next_version": "v2", "has_more": false}`
	caller := &fakeCaller{answers: map[string][]string{limitsMethod: {kept, settled, kept, kept}}}
	limits, store := newLimits(t, caller)
	setLimit(t, store, "deleted@example.com", 3, 3, "v0")
	setLimit(t, store, "saved@example.com", 2, 0, "v0")
	pull := func() {
		t.Helper()
		if err := limits.Pull(context.Background(), true); err != nil {
			t.Fatal(err)
		}
	}

	// The first miss keeps the limits: saved@ was just saved and is still inside the settle window.
	pull()
	if store.data.Limits["deleted@example.com"].Daily != 3 || store.data.Limits["saved@example.com"].Daily != 2 {
		t.Fatalf("limits after one miss %+v", store.data.Limits)
	}
	pull()
	if store.data.Limits["deleted@example.com"] != (Limit{Version: "v0"}) || store.data.Limits["saved@example.com"].Daily != 4 {
		t.Fatalf("limits after two misses %+v", store.data.Limits)
	}
	// saved@ came back in between, so its next miss starts the count again.
	pull()
	if store.data.Limits["saved@example.com"].Daily != 4 {
		t.Fatalf("one miss after a return cleared the limit: %+v", store.data.Limits)
	}
	pull()
	if store.data.Limits["saved@example.com"] != (Limit{Version: "v2"}) || store.data.Limits["kept@example.com"].Daily != 5 {
		t.Fatalf("limits %+v", store.data.Limits)
	}
}

func TestFailedPullKeepsTheVersion(t *testing.T) {
	caller := &fakeCaller{fail: true}
	limits, _ := newLimits(t, caller)
	limits.since = "v1"
	if err := limits.Pull(context.Background(), false); err == nil || limits.since != "v1" || !limits.lastFull.IsZero() {
		t.Fatalf("%v since %q", err, limits.since)
	}
}

func newUsage(t *testing.T, caller Caller, batch int) (*Usage, *MemoryStore) {
	store, _ := OpenMemory(t.TempDir())
	now := func() time.Time { return time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC) }
	return &Usage{Store: store, Caller: caller, HostIP: "203.0.113.5", Touched: &Touched{}, BatchSize: batch, Logger: quiet, Now: now}, store
}

func TestPushSendsDayAndMonthOfTouchedAccounts(t *testing.T) {
	caller := &fakeCaller{answers: map[string][]string{}}
	usage, store := newUsage(t, caller, 2)
	periods := PeriodsAt(usage.Now())
	for _, account := range []string{"a@example.com", "b@example.com"} {
		add(t, store, account, 3, periods)
		usage.Touched.Add(account)
	}
	setLimit(t, store, "a@example.com", 3, 0, "1")
	add(t, store, "a@example.com", 1, periods) // refused
	if err := usage.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	sent := caller.payloads[usageMethod]
	if len(sent) != 2 {
		t.Fatalf("%d requests, want one per account at 2 records each", len(sent))
	}
	var accounts []string
	for _, payload := range sent {
		records := payload["records"].([]any)
		first, second := records[0].(map[string]any), records[1].(map[string]any)
		if first["period"] != "d:20261026" || second["period"] != "m:202610" || first["sent"] != 3.0 {
			t.Fatalf("records %v", records)
		}
		if refused := map[any]float64{"a@example.com": 1, "b@example.com": 0}[first["account"]]; first["refused"] != refused || second["refused"] != refused {
			t.Fatalf("refused %v", records)
		}
		accounts = append(accounts, first["account"].(string))
	}
	sort.Strings(accounts)
	if accounts[0] != "a@example.com" || accounts[1] != "b@example.com" {
		t.Fatalf("accounts %v", accounts)
	}
	if err := usage.Push(context.Background()); err != nil || len(caller.payloads[usageMethod]) != 2 {
		t.Fatal("nothing touched, but a push was made")
	}
}

func TestTheFirstPushAfterMidnightSendsYesterdaysLastCounts(t *testing.T) {
	caller := &fakeCaller{answers: map[string][]string{}}
	usage, store := newUsage(t, caller, 100)
	now := time.Date(2026, 10, 26, 23, 59, 30, 0, time.UTC)
	usage.Now = func() time.Time { return now }
	yesterday, today := PeriodsAt(now), PeriodsAt(now.Add(time.Minute))

	add(t, store, "a@example.com", 3, yesterday)
	usage.Touched.Add("a@example.com")
	if err := usage.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	add(t, store, "a@example.com", 2, yesterday) // after that push, before midnight
	usage.Touched.Add("a@example.com")
	now = now.Add(time.Minute)
	add(t, store, "b@example.com", 1, today) // b sends only after midnight
	usage.Touched.Add("b@example.com")
	if err := usage.Push(context.Background()); err != nil {
		t.Fatal(err)
	}

	got := map[string]float64{}
	for _, record := range caller.payloads[usageMethod][1]["records"].([]any) {
		r := record.(map[string]any)
		got[r["account"].(string)+" "+r["period"].(string)] = r["sent"].(float64)
	}
	want := map[string]float64{
		"a@example.com d:20261027": 0, "a@example.com m:202610": 5, "a@example.com d:20261026": 5,
		"b@example.com d:20261027": 1, "b@example.com m:202610": 1,
	}
	if len(got) != len(want) {
		t.Fatalf("records %v", got)
	}
	for key, sent := range want {
		if value, ok := got[key]; !ok || value != sent {
			t.Fatalf("records %v, want %v", got, want)
		}
	}
	if err := usage.Push(context.Background()); err != nil || len(caller.payloads[usageMethod]) != 2 {
		t.Fatalf("a push with nothing touched sent %d requests: %v", len(caller.payloads[usageMethod]), err)
	}
}

// sentBy returns "account period" -> sent of every record in the given request.
func sentBy(t *testing.T, caller *fakeCaller, request int) map[string]float64 {
	t.Helper()
	got := map[string]float64{}
	for _, record := range caller.payloads[usageMethod][request]["records"].([]any) {
		r := record.(map[string]any)
		got[r["account"].(string)+" "+r["period"].(string)] = r["sent"].(float64)
	}
	return got
}

func TestAStartSendsYesterdaysCountsTheStoreKept(t *testing.T) {
	caller := &fakeCaller{answers: map[string][]string{}}
	usage, store := newUsage(t, caller, 100) // a fresh Usage, as after a restart
	now := time.Date(2026, 10, 27, 0, 5, 0, 0, time.UTC)
	usage.Now = func() time.Time { return now }
	add(t, store, "a@example.com", 4, PeriodsAt(now.Add(-24*time.Hour))) // counted before the restart
	add(t, store, "a@example.com", 1, PeriodsAt(now))
	usage.Touched.Add("a@example.com")
	if err := usage.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := sentBy(t, caller, 0); got["a@example.com d:20261026"] != 4 || got["a@example.com d:20261027"] != 1 {
		t.Fatalf("records %v", got)
	}
}

func TestPushesThatFailPastTwoMidnightsStillSendYesterday(t *testing.T) {
	caller := &fakeCaller{answers: map[string][]string{}}
	usage, store := newUsage(t, caller, 100)
	now := time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC)
	usage.Now = func() time.Time { return now }
	usage.pushed = PeriodsAt(now) // the last push that went through
	now = time.Date(2026, 10, 27, 0, 5, 0, 0, time.UTC)
	add(t, store, "a@example.com", 6, PeriodsAt(now.Add(-24*time.Hour)))
	usage.Touched.Add("a@example.com")
	if err := usage.Push(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := sentBy(t, caller, 0)
	if got["a@example.com d:20261026"] != 6 {
		t.Fatalf("the day in between was not sent: %v", got)
	}
	if _, ok := got["a@example.com d:20261025"]; ok {
		t.Fatalf("a day Suite Cloud would drop was sent: %v", got)
	}
}

func TestNoRequestHoldsMoreRecordsThanTheCap(t *testing.T) {
	for _, limit := range []int{1, 3} {
		caller := &fakeCaller{answers: map[string][]string{}}
		usage, store := newUsage(t, caller, limit)
		before := time.Date(2026, 10, 31, 23, 59, 30, 0, time.UTC) // the month ends too: 4 records an account
		now := before
		usage.Now = func() time.Time { return now }
		usage.pushed = PeriodsAt(before)
		for _, account := range []string{"a@example.com", "b@example.com"} {
			add(t, store, account, 2, PeriodsAt(before))
			usage.Touched.Add(account)
		}
		now = before.Add(time.Minute)
		if err := usage.Push(context.Background()); err != nil {
			t.Fatal(err)
		}
		total := 0
		for _, payload := range caller.payloads[usageMethod] {
			records := len(payload["records"].([]any))
			if records > limit {
				t.Fatalf("limit %d: a request held %d records", limit, records)
			}
			total += records
		}
		if total != 8 || usage.pushed != PeriodsAt(now) {
			t.Fatalf("limit %d: %d records sent, pushed %v", limit, total, usage.pushed)
		}
	}
}

func TestFailedPushKeepsTheAccountsTouched(t *testing.T) {
	usage, _ := newUsage(t, &fakeCaller{fail: true}, 100)
	usage.Touched.Add("a@example.com")
	if err := usage.Push(context.Background()); err == nil {
		t.Fatal("pushed")
	}
	if touched := usage.Touched.Take(); len(touched) != 1 {
		t.Fatalf("touched %v", touched)
	}
}
