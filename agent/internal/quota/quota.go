// Package quota enforces the sending limits of Mail Accounts. Stalwart's MTA hook asks it before it
// accepts a signed-in sender's message, and it pulls the limits from Suite Cloud and pushes the
// counts back.
package quota

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

// Limit is an account's recipients per UTC day and month; 0 means no limit. Version is the
// account's modified time in Suite Cloud, so a newer entry always sorts after an older one.
type Limit struct {
	Daily   int64  `json:"daily"`
	Monthly int64  `json:"monthly"`
	Version string `json:"version"`
}

// Periods are the counter keys of one moment, in UTC.
type Periods struct {
	Day   string // d:YYYYMMDD
	Month string // m:YYYYMM
}

func PeriodsAt(moment time.Time) Periods {
	moment = moment.UTC()
	return Periods{Day: "d:" + moment.Format("20060102"), Month: "m:" + moment.Format("200601")}
}

// Verdict says which limit refused a message, if any.
type Verdict int

const (
	Accepted Verdict = iota
	OverDaily
	OverMonthly
)

func (v Verdict) String() string {
	return [...]string{"accepted", "daily", "monthly"}[v]
}

// Count is what an account sent in the current day and month, in recipients, and how many of
// its messages were refused.
type Count struct {
	Day          int64
	Month        int64
	RefusedDay   int64
	RefusedMonth int64
}

// Store holds the limits and the counters. Every Agent of a cluster shares one, so a limit holds
// across nodes.
type Store interface {
	// Add counts n recipients in both periods, unless that takes the account over a limit; then
	// only the refused message is counted, and the limit that refused it is returned.
	Add(ctx context.Context, account string, n int64, periods Periods) (Verdict, Limit, error)
	// SetLimits stores each entry whose version is newer than the stored one.
	SetLimits(ctx context.Context, limits map[string]Limit) error
	// ClearLimits forgets the accounts' limits and versions, so a later pull can set any again.
	ClearLimits(ctx context.Context, accounts []string) error
	// Accounts lists the accounts that have a stored limit.
	Accounts(ctx context.Context) ([]string, error)
	Counts(ctx context.Context, accounts []string, periods Periods) (map[string]Count, error)
	// CountedAccounts lists the accounts with a sent or refused count on any of the days, such as
	// "d:20261026".
	CountedAccounts(ctx context.Context, days ...string) ([]string, error)
	// Run keeps the store until the context ends, then saves or closes it.
	Run(ctx context.Context) error
}

// Open returns the store the config names: memory saved in the state directory, or Redis.
func Open(cfg config.QuotaStore, stateDir string) (Store, error) {
	if cfg.Type == "memory" {
		return OpenMemory(stateDir)
	}
	return OpenRedis(cfg)
}

// Account is the key of a Stalwart login: Suite Cloud stores addresses in lowercase.
func Account(login string) string {
	return strings.ToLower(strings.TrimSpace(login))
}

func rejectMessage(verdict Verdict, limit Limit) string {
	if verdict == OverDaily {
		return fmt.Sprintf("Daily sending limit of %d recipients reached. Resets at 00:00 UTC.", limit.Daily)
	}
	return fmt.Sprintf("Monthly sending limit of %d recipients reached. Resets on the 1st at 00:00 UTC.", limit.Monthly)
}

// Touched is the set of accounts counted since the last usage push.
type Touched struct {
	mu       sync.Mutex
	accounts map[string]struct{}
}

func (t *Touched) Add(accounts ...string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.accounts == nil {
		t.accounts = map[string]struct{}{}
	}
	for _, account := range accounts {
		t.accounts[account] = struct{}{}
	}
}

// Take empties the set and returns what it held.
func (t *Touched) Take() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	taken := make([]string, 0, len(t.accounts))
	for account := range t.accounts {
		taken = append(taken, account)
	}
	t.accounts = nil
	return taken
}

// Caller makes one request to a telemetry API method; the sender implements it.
type Caller interface {
	Call(ctx context.Context, method string, payload, out any) error
}

func setMembers(set map[string]bool) []string {
	members := make([]string, 0, len(set))
	for member := range set {
		members = append(members, member)
	}
	return members
}
