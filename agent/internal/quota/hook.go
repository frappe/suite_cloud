package quota

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"time"
)

const (
	HookPath        = "/hooks/quota"
	shutdownTimeout = 5 * time.Second
)

// Hook answers Stalwart's MTA hook at the data stage. Any error it returns makes Stalwart accept
// the message, so an Agent fault never stops mail.
type Hook struct {
	Port    int
	Token   string
	Store   Store
	Touched *Touched
	Logger  *slog.Logger
	Now     func() time.Time
}

type hookAnswer struct {
	Action   string      `json:"action"`
	Response *smtpAnswer `json:"response,omitempty"`
}

// smtpAnswer keeps Stalwart's snake_case field names.
type smtpAnswer struct {
	Status         int    `json:"status"`
	EnhancedStatus string `json:"enhanced_status"`
	Message        string `json:"message"`
}

// Run serves the hook on loopback only until the context ends.
func (h *Hook) Run(ctx context.Context) error {
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(h.Port)))
	if err != nil {
		return err
	}
	return h.serve(ctx, listener)
}

// serve returns once the calls in flight when the context ended are answered, so their counts
// reach the store before its last save. When Serve fails on its own, the shutdown is unregistered
// and a run started again leaves nothing behind.
func (h *Hook) serve(ctx context.Context, listener net.Listener) error {
	mux := http.NewServeMux()
	mux.Handle(HookPath, h)
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	drained := make(chan struct{})
	stop := context.AfterFunc(ctx, func() {
		defer close(drained)
		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
		defer cancel()
		server.Shutdown(shutdown)
	})
	err := server.Serve(listener)
	if stop() {
		return err
	}
	<-drained
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (h *Hook) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !h.isAuthorized(r) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	login, recipients, err := readRequest(r.Body)
	io.Copy(io.Discard, r.Body) // Stalwart reads the answer only once it sent the whole message
	if err != nil {
		h.Logger.Warn("hook request not understood", "error", err)
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	answer, err := h.check(r.Context(), Account(login), recipients)
	if err != nil {
		h.Logger.Error("quota check failed; Stalwart accepts the message", "account", login, "error", err)
		http.Error(w, "quota check failed", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(answer)
}

func (h *Hook) isAuthorized(r *http.Request) bool {
	want := "Bearer " + h.Token
	return subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte(want)) == 1
}

func (h *Hook) check(ctx context.Context, account string, recipients int64) (hookAnswer, error) {
	if account == "" || recipients == 0 {
		return hookAnswer{Action: "accept"}, nil
	}
	verdict, limit, err := h.Store.Add(ctx, account, recipients, PeriodsAt(h.Now()))
	if err != nil {
		return hookAnswer{}, err
	}
	h.Touched.Add(account)
	if verdict == Accepted {
		return hookAnswer{Action: "accept"}, nil
	}
	h.Logger.Info("message over the sending limit", "account", account, "recipients", recipients, "over", verdict.String())
	reply := &smtpAnswer{Status: 550, EnhancedStatus: "5.4.5", Message: rejectMessage(verdict, limit)}
	return hookAnswer{Action: "reject", Response: reply}, nil
}

type hookContext struct {
	SASL *struct {
		Login string `json:"login"`
	} `json:"sasl"`
}

type hookEnvelope struct {
	To []struct{} `json:"to"`
}

// readRequest takes the signed-in login and the recipient count from Stalwart's request. At the
// data stage the request ends with the whole message after context and envelope, so the decoder
// stops once it has both and never holds the message.
func readRequest(body io.Reader) (login string, recipients int64, err error) {
	decoder := json.NewDecoder(body)
	if token, err := decoder.Token(); err != nil || token != json.Delim('{') {
		return "", 0, fmt.Errorf("not a JSON object: %v", err)
	}
	var request hookFields
	for decoder.More() && !request.isComplete() {
		if err := request.decodeField(decoder); err != nil {
			return "", 0, err
		}
	}
	if request.envelope == nil {
		return "", 0, errors.New("no envelope")
	}
	return request.login(), int64(len(request.envelope.To)), nil
}

// hookFields holds the parts of Stalwart's request the hook reads.
type hookFields struct {
	context  *hookContext
	envelope *hookEnvelope
}

func (r *hookFields) isComplete() bool { return r.context != nil && r.envelope != nil }

// decodeField decodes the next key and its value; a key the hook does not read is skipped.
func (r *hookFields) decodeField(decoder *json.Decoder) error {
	key, err := decoder.Token()
	if err != nil {
		return err
	}
	var value any = &json.RawMessage{}
	switch key {
	case "context":
		r.context = &hookContext{}
		value = r.context
	case "envelope":
		r.envelope = &hookEnvelope{}
		value = r.envelope
	}
	if err := decoder.Decode(value); err != nil {
		return fmt.Errorf("%v: %w", key, err)
	}
	return nil
}

func (r *hookFields) login() string {
	if r.context == nil || r.context.SASL == nil {
		return ""
	}
	return r.context.SASL.Login
}
