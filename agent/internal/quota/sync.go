package quota

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

const (
	fullPullInterval = time.Hour // also catches a save committed after a newer one was pulled
	finalPushTimeout = 5 * time.Second
	limitsMethod     = "quota.limits"
	usageMethod      = "quota.usage"
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
	missing  map[string]bool // accounts the last full pull did not return
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

// Pull reads every page. A full pull also clears the limits of accounts that this and the previous
// full pull did not return, which is how a deleted account loses them. One miss is not enough: a
// save younger than the settle window is left out of a pull, and the account would send without
// a limit until the next one.
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
	missing := map[string]bool{}
	var gone []string
	for _, account := range stored {
		if seen[account] {
			continue
		}
		missing[account] = true
		if l.missing[account] {
			gone = append(gone, account)
		}
	}
	if err := l.Store.ClearLimits(ctx, gone); err != nil {
		return err
	}
	l.missing = missing
	return nil
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
	pushed    Periods // the periods of the last push that went through; zero until one does
}

type usageRecord struct {
	Account string `json:"account"`
	Period  string `json:"period"`
	Sent    int64  `json:"sent"`
	Refused int64  `json:"refused"`
}

// Run pushes on every tick, and once more on the way out.
func (u *Usage) Run(ctx context.Context) error {
	u.TouchCounted(ctx)
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

// TouchCounted marks every account with a count today or yesterday, as on a start: what was
// counted before a crash, or after the last push before one, is then pushed. Suite Cloud keeps
// the higher count, so sending one again is harmless.
func (u *Usage) TouchCounted(ctx context.Context) {
	now := u.Now()
	accounts, err := u.Store.CountedAccounts(ctx, PeriodsAt(now).Day, PeriodsAt(now.Add(-24*time.Hour)).Day)
	if err != nil {
		u.Logger.Warn("counted accounts not read; they are pushed when they send again", "error", err)
	}
	u.Touched.Add(accounts...)
}

// Push sends the touched accounts' records in requests of at most BatchSize records. Accounts
// with a record in a request that was not taken stay touched.
// Until a push of the current day has gone through (after midnight UTC, after a start, after
// failures) each push also sends yesterday's day and month: what was counted after the last push
// of yesterday would otherwise never be reported. Suite Cloud takes nothing older.
func (u *Usage) Push(ctx context.Context) error {
	accounts := u.Touched.Take()
	if len(accounts) == 0 {
		return nil
	}
	now := u.Now()
	current := PeriodsAt(now)
	sets := []Periods{current}
	if u.pushed != current {
		sets = append(sets, PeriodsAt(now.Add(-24*time.Hour)))
	}
	counts := make([]map[string]Count, len(sets))
	for i, periods := range sets {
		var err error
		if counts[i], err = u.Store.Counts(ctx, accounts, periods); err != nil {
			u.Touched.Add(accounts...)
			return err
		}
	}
	var records []usageRecord
	for _, account := range accounts {
		for i, periods := range sets {
			count := counts[i][account]
			// Before midnight, only what was counted: an account first counted after it has nothing.
			if i == 0 || count.Day > 0 || count.RefusedDay > 0 {
				records = append(records, usageRecord{account, periods.Day, count.Day, count.RefusedDay})
			}
			if i == 0 || periods.Month != current.Month && (count.Month > 0 || count.RefusedMonth > 0) {
				records = append(records, usageRecord{account, periods.Month, count.Month, count.RefusedMonth})
			}
		}
	}
	size := max(u.BatchSize, 1)
	for start := 0; start < len(records); start += size {
		batch := records[start:min(start+size, len(records))]
		var answer struct{}
		if err := u.Caller.Call(ctx, usageMethod, map[string]any{"host_ip": u.HostIP, "records": batch}, &answer); err != nil {
			for _, record := range records[start:] {
				u.Touched.Add(record.Account)
			}
			return err
		}
	}
	u.pushed = current
	return nil
}
