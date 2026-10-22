package sender

import (
	"math/rand/v2"
	"time"
)

// Backoff doubles the wait on each failed attempt, with jitter so nodes do not retry in step.
type Backoff struct {
	Base   time.Duration
	Max    time.Duration
	Jitter float64
}

var (
	transientBackoff = Backoff{Base: 2 * time.Second, Max: time.Minute, Jitter: 0.2}
	refusedBackoff   = Backoff{Base: 10 * time.Second, Max: 5 * time.Minute, Jitter: 0.2}
)

// Delay for the attempt that just failed, counted from 1.
func (b Backoff) Delay(attempt int) time.Duration {
	delay := b.Base
	for i := 1; i < attempt && delay < b.Max; i++ {
		delay *= 2
	}
	if delay > b.Max {
		delay = b.Max
	}
	spread := 1 + b.Jitter*(2*rand.Float64()-1)
	return time.Duration(float64(delay) * spread)
}

// nextMinute is when the server's window opens again, a little past the boundary so a skewed
// clock does not land the retry in the same window.
func nextMinute(now time.Time) time.Time {
	return now.Truncate(time.Minute).Add(time.Minute + time.Second + time.Duration(rand.IntN(4000))*time.Millisecond)
}
