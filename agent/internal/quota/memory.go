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
	switch {
	case limit.Daily > 0 && usage[periods.Day]+n > limit.Daily:
		return OverDaily, limit, nil
	case limit.Monthly > 0 && usage[periods.Month]+n > limit.Monthly:
		return OverMonthly, limit, nil
	}
	if usage == nil {
		if m.data.Usage == nil {
			m.data.Usage = map[string]map[string]int64{}
		}
		usage = map[string]int64{}
		m.data.Usage[account] = usage
	}
	usage[periods.Day] += n
	usage[periods.Month] += n
	m.dirty = true
	return Accepted, limit, nil
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
		if limit, ok := m.data.Limits[account]; ok {
			m.data.Limits[account] = Limit{Version: limit.Version}
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

func (m *MemoryStore) Counts(_ context.Context, accounts []string, periods Periods) (map[string]Count, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	counts := make(map[string]Count, len(accounts))
	for _, account := range accounts {
		usage := m.data.Usage[account]
		counts[account] = Count{Day: usage[periods.Day], Month: usage[periods.Month]}
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

// Save drops the counters of past periods and writes the file when anything changed.
func (m *MemoryStore) Save() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.dirty {
		return nil
	}
	periods := PeriodsAt(m.now())
	for account, usage := range m.data.Usage {
		for period := range usage {
			if period != periods.Day && period != periods.Month {
				delete(usage, period)
			}
		}
		if len(usage) == 0 {
			delete(m.data.Usage, account)
		}
	}
	if err := state.WriteFile(m.dir, memoryFileName, m.data); err != nil {
		return err
	}
	m.dirty = false
	return nil
}
