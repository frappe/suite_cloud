package quota

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"testing"
	"time"
)

const hookRequest = `{
  "context": {"stage": "data", "sasl": {"login": "A@Example.com", "method": "plain"}, "protocol": {"version": 1}},
  "envelope": {"from": {"address": "a@example.com"}, "to": [{"address": "b@example.org"}, {"address": "c@example.org"}]},
  "message": {"headers": [["Subject", "hi"]], "contents": "%s", "size": 2}
}`

type failingStore struct{ Store }

func (failingStore) Add(context.Context, string, int64, Periods) (Verdict, Limit, error) {
	return Accepted, Limit{}, errors.New("store down")
}

func newHook(t *testing.T, store Store) *Hook {
	t.Helper()
	return &Hook{Token: "t0ken", Store: store, Touched: &Touched{}, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Now: func() time.Time { return time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC) }}
}

func call(hook *Hook, auth, body string) (*httptest.ResponseRecorder, hookAnswer) {
	request := httptest.NewRequest(http.MethodPost, HookPath, strings.NewReader(body))
	request.Header.Set("Authorization", auth)
	recorder := httptest.NewRecorder()
	hook.ServeHTTP(recorder, request)
	var answer hookAnswer
	json.Unmarshal(recorder.Body.Bytes(), &answer)
	return recorder, answer
}

func TestHookCountsRecipientsAndAccepts(t *testing.T) {
	store, _ := OpenMemory(t.TempDir())
	hook := newHook(t, store)
	recorder, answer := call(hook, "Bearer t0ken", strings.Replace(hookRequest, "%s", "hi", 1))
	if recorder.Code != 200 || answer.Action != "accept" {
		t.Fatalf("%d %+v", recorder.Code, answer)
	}
	counts, _ := store.Counts(context.Background(), []string{"a@example.com"}, PeriodsAt(hook.Now()))
	if counts["a@example.com"] != (Count{Day: 2, Month: 2}) {
		t.Fatalf("counts %+v", counts)
	}
	if touched := hook.Touched.Take(); len(touched) != 1 || touched[0] != "a@example.com" {
		t.Fatalf("touched %v", touched)
	}
}

func TestHookRejectsInStalwartsShape(t *testing.T) {
	store, _ := OpenMemory(t.TempDir())
	setLimit(t, store, "a@example.com", 1, 0, "1")
	hook := newHook(t, store)
	recorder, _ := call(hook, "Bearer t0ken", strings.Replace(hookRequest, "%s", "hi", 1))
	var raw map[string]any
	json.Unmarshal(recorder.Body.Bytes(), &raw)
	response, _ := raw["response"].(map[string]any)
	if raw["action"] != "reject" || response["status"] != 550.0 || response["enhanced_status"] != "5.4.5" {
		t.Fatalf("answer %s", recorder.Body)
	}
	if !strings.Contains(response["message"].(string), "Daily sending limit of 1 recipients") {
		t.Fatalf("message %v", response["message"])
	}
	if touched := hook.Touched.Take(); len(touched) != 1 {
		t.Fatalf("a refused account must be pushed too: %v", touched)
	}
}

func TestHookRefusesAWrongToken(t *testing.T) {
	store, _ := OpenMemory(t.TempDir())
	for _, auth := range []string{"", "Bearer wrong", "t0ken"} {
		if recorder, _ := call(newHook(t, store), auth, hookRequest); recorder.Code != http.StatusUnauthorized {
			t.Fatalf("%q: %d", auth, recorder.Code)
		}
	}
}

func TestHookAcceptsSendersWithoutLogin(t *testing.T) {
	store, _ := OpenMemory(t.TempDir())
	body := `{"context": {"stage": "data"}, "envelope": {"from": {"address": "x@example.org"}, "to": [{"address": "a@example.com"}]}}`
	if recorder, answer := call(newHook(t, store), "Bearer t0ken", body); recorder.Code != 200 || answer.Action != "accept" {
		t.Fatalf("%d %+v", recorder.Code, answer)
	}
	if accounts, _ := store.Accounts(context.Background()); len(store.data.Usage) != 0 || len(accounts) != 0 {
		t.Fatal("an unauthenticated message was counted")
	}
}

func TestHookStoreErrorIsAServerError(t *testing.T) {
	recorder, _ := call(newHook(t, failingStore{}), "Bearer t0ken", strings.Replace(hookRequest, "%s", "hi", 1))
	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("status %d", recorder.Code)
	}
}

// countingReader reports how much of the body was read before the answer was decided.
type countingReader struct {
	io.Reader
	read int
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.Reader.Read(p)
	c.read += n
	return n, err
}

func TestReadRequestStopsBeforeTheMessage(t *testing.T) {
	body := &countingReader{Reader: strings.NewReader(strings.Replace(hookRequest, "%s", strings.Repeat("x", 4<<20), 1))}
	login, recipients, err := readRequest(body)
	if err != nil || login != "A@Example.com" || recipients != 2 {
		t.Fatalf("%q %d %v", login, recipients, err)
	}
	if body.read > 64<<10 {
		t.Fatalf("read %d bytes of a 4 MiB message", body.read)
	}
}

func TestReadRequestNeedsAnEnvelope(t *testing.T) {
	if _, _, err := readRequest(strings.NewReader(`{"context": {}}`)); err == nil {
		t.Fatal("accepted")
	}
	if _, _, err := readRequest(strings.NewReader(`[]`)); err == nil {
		t.Fatal("accepted")
	}
}

// blockingStore holds each Add until released, as a slow store would.
type blockingStore struct {
	Store
	entered, release chan struct{}
}

func (b blockingStore) Add(context.Context, string, int64, Periods) (Verdict, Limit, error) {
	b.entered <- struct{}{}
	<-b.release
	return Accepted, Limit{}, nil
}

func TestHookAnswersTheCallsInFlightBeforeItStops(t *testing.T) {
	store := blockingStore{entered: make(chan struct{}), release: make(chan struct{})}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan error, 1)
	go func() { stopped <- newHook(t, store).serve(ctx, listener) }()

	answered := make(chan int, 1)
	go func() {
		request, _ := http.NewRequest(http.MethodPost, "http://"+listener.Addr().String()+HookPath, strings.NewReader(strings.Replace(hookRequest, "%s", "hi", 1)))
		request.Header.Set("Authorization", "Bearer t0ken")
		if response, err := http.DefaultClient.Do(request); err == nil {
			answered <- response.StatusCode
			response.Body.Close()
		}
	}()
	<-store.entered
	cancel()
	select {
	case err := <-stopped:
		t.Fatalf("stopped with a call in flight: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	close(store.release)
	if code := <-answered; code != http.StatusOK {
		t.Fatalf("in-flight call answered %d", code)
	}
	if err := <-stopped; err != nil {
		t.Fatal(err)
	}
}

// brokenListener fails every Accept, so Serve returns at once.
type brokenListener struct{}

func (brokenListener) Accept() (net.Conn, error) { return nil, errors.New("listener broke") }
func (brokenListener) Close() error              { return nil }
func (brokenListener) Addr() net.Addr            { return &net.TCPAddr{} }

func TestAHookThatFailsOnItsOwnLeavesNothingBehind(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	hook := newHook(t, failingStore{})
	before := runtime.NumGoroutine()
	for range 20 {
		if err := hook.serve(ctx, brokenListener{}); err == nil {
			t.Fatal("a broken listener served")
		}
	}
	if after := runtime.NumGoroutine(); after > before+2 {
		t.Fatalf("goroutines grew from %d to %d over 20 failed runs", before, after)
	}
}
