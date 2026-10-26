package quota

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

const (
	fullPullInterval  = time.Hour // also catches a save committed after a newer one was pulled
	finalPushTimeout  = 5 * time.Second
	limitsMethod      = "quota.limits"
	usageMethod       = "quota.usage"
	recordsPerAccount = 2 // a day and a month
)

// Limits copies the cluster's limits from Suite Cloud into the store: what changed since the last
// pull, and everything on start and every hour.
type Limits struct {
	Store    Store
	Caller   Caller
	HostIP   string
	Interval time.Duration
	Logger   *slog.Logger
	Now      func() time.Time
	since    string
	lastFull time.Time
}

type limitsPage struct {
	Limits []struct {
		Account string `json:"account"`
		Limit
	} `json:"limits"`
	NextVersion string `json:"next_version"`
	HasMore     bool   `json:"has_more"`
}

func (l *Limits) Run(ctx context.Context) error {
	ticker := time.NewTicker(l.Interval)
	defer ticker.Stop()
	for {
		full := l.lastFull.IsZero() || l.Now().Sub(l.lastFull) >= fullPullInterval
		if err := l.Pull(ctx, full); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			l.Logger.Warn("limits not pulled; the stored ones stay in force", "full", full, "error", err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// Pull reads every page. A full pull also clears the limits of accounts Suite Cloud no longer
// returns, which is how a deleted account loses them.
func (l *Limits) Pull(ctx context.Context, full bool) error {
	since := l.since
	if full {
		since = ""
	}
	seen := map[string]bool{}
	for {
		var page limitsPage
		if err := l.Caller.Call(ctx, limitsMethod, map[string]any{"host_ip": l.HostIP, "since_version": since}, &page); err != nil {
			return err
		}
		limits := make(map[string]Limit, len(page.Limits))
		for _, entry := range page.Limits {
			account := Account(entry.Account)
			limits[account] = entry.Limit
			seen[account] = true
		}
		if err := l.Store.SetLimits(ctx, limits); err != nil {
			return err
		}
		if page.NextVersion != "" {
			since = page.NextVersion
		}
		if !page.HasMore {
			break
		}
	}
	if full {
		if err := l.clearMissing(ctx, seen); err != nil {
			return err
		}
		l.lastFull = l.Now()
	}
	l.since = since
	return nil
}

func (l *Limits) clearMissing(ctx context.Context, seen map[string]bool) error {
	stored, err := l.Store.Accounts(ctx)
	if err != nil {
		return err
	}
	var missing []string
	for _, account := range stored {
		if !seen[account] {
			missing = append(missing, account)
		}
	}
	return l.Store.ClearLimits(ctx, missing)
}

// Usage pushes the day and month totals of the accounts counted since the last push.
type Usage struct {
	Store     Store
	Caller    Caller
	HostIP    string
	Touched   *Touched
	Interval  time.Duration
	BatchSize int // records per request
	Logger    *slog.Logger
	Now       func() time.Time
}

type usageRecord struct {
	Account string `json:"account"`
	Period  string `json:"period"`
	Sent    int64  `json:"sent"`
}

// Run pushes on every tick, and once more on the way out.
func (u *Usage) Run(ctx context.Context) error {
	ticker := time.NewTicker(u.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			final, cancel := context.WithTimeout(context.WithoutCancel(ctx), finalPushTimeout)
			defer cancel()
			if err := u.Push(final); err != nil {
				u.Logger.Warn("usage not pushed on shutdown", "error", err)
			}
			return ctx.Err()
		case <-ticker.C:
			if err := u.Push(ctx); err != nil && !errors.Is(err, context.Canceled) {
				u.Logger.Warn("usage not pushed; it is sent with the next push", "error", err)
			}
		}
	}
}

// Push sends the touched accounts in batches. Accounts whose batch was not taken stay touched.
func (u *Usage) Push(ctx context.Context) error {
	accounts := u.Touched.Take()
	if len(accounts) == 0 {
		return nil
	}
	periods := PeriodsAt(u.Now())
	counts, err := u.Store.Counts(ctx, accounts, periods)
	if err != nil {
		u.Touched.Add(accounts...)
		return err
	}
	step := max(u.BatchSize/recordsPerAccount, 1)
	for start := 0; start < len(accounts); start += step {
		batch := accounts[start:min(start+step, len(accounts))]
		records := make([]usageRecord, 0, len(batch)*recordsPerAccount)
		for _, account := range batch {
			count := counts[account]
			records = append(records, usageRecord{account, periods.Day, count.Day}, usageRecord{account, periods.Month, count.Month})
		}
		var answer struct{}
		if err := u.Caller.Call(ctx, usageMethod, map[string]any{"host_ip": u.HostIP, "records": records}, &answer); err != nil {
			u.Touched.Add(accounts[start:]...)
			return err
		}
	}
	return nil
}
