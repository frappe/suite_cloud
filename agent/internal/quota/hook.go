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
	mux := http.NewServeMux()
	mux.Handle(HookPath, h)
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
		defer cancel()
		server.Shutdown(shutdown)
	}()
	if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
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
	if verdict == Accepted {
		h.Touched.Add(account)
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
	var hookCtx hookContext
	var envelope *hookEnvelope
	haveContext := false
	for decoder.More() && !(haveContext && envelope != nil) {
		key, err := decoder.Token()
		if err != nil {
			return "", 0, err
		}
		switch key {
		case "context":
			err, haveContext = decoder.Decode(&hookCtx), true
		case "envelope":
			envelope = &hookEnvelope{}
			err = decoder.Decode(envelope)
		default:
			err = decoder.Decode(&json.RawMessage{})
		}
		if err != nil {
			return "", 0, fmt.Errorf("%v: %w", key, err)
		}
	}
	if envelope == nil {
		return "", 0, errors.New("no envelope")
	}
	if hookCtx.SASL != nil {
		login = hookCtx.SASL.Login
	}
	return login, int64(len(envelope.To)), nil
}
