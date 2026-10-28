// Package sender posts batches to Suite Cloud's telemetry API with the retry policy the API
// documents: 429 and 5xx are retried, 4xx is not, and a batch keeps its id across retries.
package sender

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

const methodPrefix = "/api/v2/method/suite_cloud.api.telemetry."

const (
	batchAnswerLimit = 64 << 10
	callAnswerLimit  = 8 << 20 // a page of limits
)

type Sender struct {
	client      *http.Client
	base        string
	secret      string
	userAgent   string
	maxAttempts int
	limiter     *Limiter
	log         *slog.Logger
	now         func() time.Time
	sleep       func(context.Context, time.Duration) error
}

func New(cfg config.SuiteCloud, version string, log *slog.Logger) *Sender {
	return &Sender{
		client:      &http.Client{Timeout: time.Duration(cfg.RequestTimeoutSeconds) * time.Second},
		base:        strings.TrimRight(cfg.URL, "/") + methodPrefix,
		secret:      cfg.Secret,
		userAgent:   "suite-cloud-agent/" + version,
		maxAttempts: cfg.MaxAttempts,
		limiter:     NewLimiter(cfg.RequestsPerMinute),
		log:         log,
		now:         time.Now,
		sleep:       sleep,
	}
}

// Send posts one batch until Suite Cloud takes it or the policy gives it up. An error is returned
// only when the context ends; the caller then still holds the batch as pending.
func (s *Sender) Send(ctx context.Context, batch telemetry.Batch) (telemetry.Outcome, error) {
	body, err := batch.MarshalBody()
	if err != nil {
		s.log.Error("batch not encoded", "batch_id", batch.ID, "error", err)
		return telemetry.Skipped, nil
	}
	attempts, refusals := 0, 0
	for {
		if err := s.limiter.Wait(ctx); err != nil {
			return 0, err
		}
		status, message, err := s.post(ctx, string(batch.Kind), body, batchAnswerLimit)
		if status != http.StatusOK {
			message = errorMessage(message)
		}
		fields := []any{"kind", batch.Kind, "batch_id", batch.ID, "records", len(batch.Records), "status", status}
		switch {
		case err == nil && status == http.StatusOK:
			s.checkAnswer(batch, message)
			return telemetry.Sent, nil
		case status == http.StatusTooManyRequests:
			until := nextMinute(s.now())
			s.limiter.Exhaust(until)
			s.log.Debug("rate limited; waiting for the next minute", append(fields, "until", until)...)
		case status == http.StatusUnauthorized || status == http.StatusForbidden:
			refusals++
			delay := refusedBackoff.Delay(refusals)
			s.log.Error("agent credentials refused; Install Agent or Upgrade Agents brings the current ones", append(fields, "message", message, "retry_in", delay)...)
			if err := s.sleep(ctx, delay); err != nil {
				return 0, err
			}
		case err == nil && status >= 400 && status < 500:
			s.log.Error("batch refused; it is skipped", append(fields, "message", message)...)
			return telemetry.Skipped, nil
		default:
			attempts++
			if attempts >= s.maxAttempts {
				s.log.Error("batch given up after repeated failures", append(fields, "attempts", attempts, "error", err, "message", message)...)
				return telemetry.Skipped, nil
			}
			delay := transientBackoff.Delay(attempts)
			s.log.Warn("send failed; retrying", append(fields, "attempt", attempts, "error", err, "message", message, "retry_in", delay)...)
			if err := s.sleep(ctx, delay); err != nil {
				return 0, err
			}
		}
	}
}

// Call posts one request to a method, such as "quota.limits", under the shared rate limit and
// decodes the answer's data into out. It makes one attempt: callers retry on their next tick.
func (s *Sender) Call(ctx context.Context, method string, payload, out any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	if err := s.limiter.Wait(ctx); err != nil {
		return err
	}
	status, answer, err := s.post(ctx, method, body, callAnswerLimit)
	switch {
	case err != nil:
		return err
	case status == http.StatusTooManyRequests:
		s.limiter.Exhaust(nextMinute(s.now()))
		return fmt.Errorf("%s: rate limited", method)
	case status != http.StatusOK:
		return fmt.Errorf("%s: status %d: %s", method, status, errorMessage(answer))
	}
	envelope := struct {
		Data any `json:"data"`
	}{out}
	if err := json.Unmarshal([]byte(answer), &envelope); err != nil {
		return fmt.Errorf("%s: answer not understood: %w", method, err)
	}
	return nil
}

// post makes one request. It outlives a cancelled context so a request on the wire completes and
// is committed; the client timeout bounds it.
func (s *Sender) post(ctx context.Context, method string, body []byte, answerLimit int64) (status int, message string, err error) {
	request, err := http.NewRequestWithContext(context.WithoutCancel(ctx), http.MethodPost, s.base+method, bytes.NewReader(body))
	if err != nil {
		return 0, "", err
	}
	request.Header.Set("Authorization", "Bearer "+s.secret)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", s.userAgent)

	response, err := s.client.Do(request)
	if err != nil {
		return 0, "", err
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, answerLimit))
	return response.StatusCode, string(raw), nil
}

// checkAnswer warns when Suite Cloud staged something else than what was sent.
func (s *Sender) checkAnswer(batch telemetry.Batch, raw string) {
	var answer struct {
		Data struct {
			BatchID  string `json:"batch_id"`
			Accepted int    `json:"accepted"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw), &answer); err != nil {
		s.log.Warn("answer not understood", "batch_id", batch.ID, "body", truncate(raw))
		return
	}
	if answer.Data.BatchID != batch.ID || answer.Data.Accepted != len(batch.Records) {
		s.log.Warn("answer differs from the batch", "batch_id", batch.ID, "answer_id", answer.Data.BatchID, "records", len(batch.Records), "accepted", answer.Data.Accepted)
	}
}

// errorMessage pulls the message out of Frappe's {"errors": [{"type", "message"}]} body.
func errorMessage(raw string) string {
	var body struct {
		Errors []struct {
			Type    string `json:"type"`
			Message string `json:"message"`
		} `json:"errors"`
	}
	if json.Unmarshal([]byte(raw), &body) == nil && len(body.Errors) > 0 {
		return fmt.Sprintf("%s: %s", body.Errors[0].Type, body.Errors[0].Message)
	}
	return truncate(raw)
}

func truncate(text string) string {
	if len(text) > 500 {
		return text[:500] + "..."
	}
	return text
}
