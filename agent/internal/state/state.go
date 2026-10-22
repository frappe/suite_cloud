// Package state is what the agent remembers across restarts: how far each log file was shipped
// and which batch was in flight.
package state

import "time"

// Identity tells a file apart from another at the same path after a replacement.
type Identity struct {
	Dev   uint64 `json:"dev"`
	Inode uint64 `json:"inode"`
}

// Cursor is the shipped position in one file. Everything before Offset was sent or skipped.
type Cursor struct {
	Path string `json:"path"`
	Identity
	Offset     int64     `json:"offset"`
	Finished   bool      `json:"finished"`
	FinishedAt time.Time `json:"finished_at,omitempty"`
}

// Pending is a batch whose id was chosen but whose send has not been confirmed. It is written
// before the first request, so a restart resends exactly these bytes under the same id.
type Pending struct {
	BatchID string `json:"batch_id"`
	Path    string `json:"path"`
	Identity
	Start   int64 `json:"start"`
	End     int64 `json:"end"`
	Records int   `json:"records"`
}

// LogState is one configured log's cursors. Initialised says the ship_existing rule was applied.
type LogState struct {
	Initialised bool      `json:"initialised"`
	Cursors     []*Cursor `json:"cursors"`
	Pending     *Pending  `json:"pending,omitempty"`
}

type State struct {
	Version int                  `json:"version"`
	Logs    map[string]*LogState `json:"logs"`
}

// Log returns the state of a named log, creating it empty.
func (s *State) Log(name string) *LogState {
	if s.Logs == nil {
		s.Logs = map[string]*LogState{}
	}
	log := s.Logs[name]
	if log == nil {
		log = &LogState{}
		s.Logs[name] = log
	}
	return log
}

// Cursor finds the cursor of a file by identity.
func (l *LogState) Cursor(id Identity) *Cursor {
	for _, cursor := range l.Cursors {
		if cursor.Identity == id {
			return cursor
		}
	}
	return nil
}

// Drop forgets a file's cursor.
func (l *LogState) Drop(id Identity) {
	kept := l.Cursors[:0]
	for _, cursor := range l.Cursors {
		if cursor.Identity != id {
			kept = append(kept, cursor)
		}
	}
	l.Cursors = kept
}
