package quota

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/state"
)

const (
	memoryFileName = "quota.json"
	saveInterval   = 5 * time.Second
)

type memoryData struct {
	Version int                         `json:"version"`
	Limits  map[string]Limit            `json:"limits"`
	Usage   map[string]map[string]int64 `json:"usage"`
	Refused map[string]map[string]int64 `json:"refused"`
}

// counters is an account's counters by period, created on first use.
func counters(all *map[string]map[string]int64, account string) map[string]int64 {
	if *all == nil {
		*all = map[string]map[string]int64{}
	}
	if (*all)[account] == nil {
		(*all)[account] = map[string]int64{}
	}
	return (*all)[account]
}

// MemoryStore is the store of a single-node cluster: one Agent, so one lock makes each check
// atomic. It is saved to quota.json every few seconds, so a crash loses at most those counts.
type MemoryStore struct {
	mu    sync.Mutex
	dir   string
	data  memoryData
	dirty bool
	now   func() time.Time
}

// OpenMemory loads quota.json from dir, or starts empty when there is none.
func OpenMemory(dir string) (*MemoryStore, error) {
	store := &MemoryStore{dir: dir, data: memoryData{Version: 1}, now: time.Now}
	raw, err := os.ReadFile(filepath.Join(dir, memoryFileName))
	if errors.Is(err, os.ErrNotExist) {
		return store, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, &store.data); err != nil {
		return nil, fmt.Errorf("%s: %w", filepath.Join(dir, memoryFileName), err)
	}
	return store, nil
}

func (m *MemoryStore) Add(_ context.Context, account string, n int64, periods Periods) (Verdict, Limit, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	limit := m.data.Limits[account]
	usage := m.data.Usage[account]
	verdict := Accepted
	switch {
	case limit.Daily > 0 && usage[periods.Day]+n > limit.Daily:
		verdict = OverDaily
	case limit.Monthly > 0 && usage[periods.Month]+n > limit.Monthly:
		verdict = OverMonthly
	}
	counted, by := n, &m.data.Usage
	if verdict != Accepted {
		counted, by = 1, &m.data.Refused
	}
	accountCounters := counters(by, account)
	accountCounters[periods.Day] += counted
	accountCounters[periods.Month] += counted
	m.dirty = true
	return verdict, limit, nil
}

func (m *MemoryStore) SetLimits(_ context.Context, limits map[string]Limit) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.data.Limits == nil {
		m.data.Limits = map[string]Limit{}
	}
	for account, limit := range limits {
		if stored, ok := m.data.Limits[account]; ok && stored.Version >= limit.Version {
			continue
		}
		m.data.Limits[account] = limit
		m.dirty = true
	}
	return nil
}

func (m *MemoryStore) ClearLimits(_ context.Context, accounts []string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, account := range accounts {
		if _, ok := m.data.Limits[account]; ok {
			delete(m.data.Limits, account)
			m.dirty = true
		}
	}
	return nil
}

func (m *MemoryStore) Accounts(context.Context) ([]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	accounts := make([]string, 0, len(m.data.Limits))
	for account := range m.data.Limits {
		accounts = append(accounts, account)
	}
	return accounts, nil
}

func (m *MemoryStore) CountedAccounts(_ context.Context, days ...string) ([]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	found := map[string]bool{}
	for _, counters := range []map[string]map[string]int64{m.data.Usage, m.data.Refused} {
		for account, byPeriod := range counters {
			for _, day := range days {
				if byPeriod[day] > 0 {
					found[account] = true
				}
			}
		}
	}
	return setMembers(found), nil
}

func (m *MemoryStore) Counts(_ context.Context, accounts []string, periods Periods) (map[string]Count, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	counts := make(map[string]Count, len(accounts))
	for _, account := range accounts {
		usage, refused := m.data.Usage[account], m.data.Refused[account]
		counts[account] = Count{usage[periods.Day], usage[periods.Month], refused[periods.Day], refused[periods.Month]}
	}
	return counts, nil
}

// Run saves the store every few seconds while it changed, and once more when the context ends.
func (m *MemoryStore) Run(ctx context.Context) error {
	ticker := time.NewTicker(saveInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return m.Save()
		case <-ticker.C:
			if err := m.Save(); err != nil {
				return err
			}
		}
	}
}

// Save drops the counters older than yesterday and its month, and writes the file when anything
// changed, a drop included, so a quiet store sheds old days too. Yesterday stays until the usage
// push has sent its last count.
func (m *MemoryStore) Save() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()
	today, yesterday := PeriodsAt(now), PeriodsAt(now.Add(-24*time.Hour))
	kept := map[string]bool{today.Day: true, today.Month: true, yesterday.Day: true, yesterday.Month: true}
	droppedUsage, droppedRefused := prune(m.data.Usage, kept), prune(m.data.Refused, kept)
	m.dirty = m.dirty || droppedUsage || droppedRefused
	if !m.dirty {
		return nil
	}
	if err := state.WriteFile(m.dir, memoryFileName, m.data); err != nil {
		return err
	}
	m.dirty = false
	return nil
}

// prune reports whether it dropped anything.
func prune(all map[string]map[string]int64, kept map[string]bool) bool {
	dropped := false
	for account, byPeriod := range all {
		for period := range byPeriod {
			if !kept[period] {
				delete(byPeriod, period)
				dropped = true
			}
		}
		if len(byPeriod) == 0 {
			delete(all, account)
		}
	}
	return dropped
}
