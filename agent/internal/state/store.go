package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

const fileName = "state.json"

// Store holds the state in memory and writes it atomically. Tailers share one Store and change
// only their own log's entries, under its lock.
type Store struct {
	mu    sync.Mutex
	dir   string
	state *State
}

// Open loads the state file in dir, or starts empty when there is none. A file that cannot be
// read is an error: starting from zero would ship every file again.
func Open(dir string) (*Store, error) {
	raw, err := os.ReadFile(filepath.Join(dir, fileName))
	if errors.Is(err, os.ErrNotExist) {
		return &Store{dir: dir, state: &State{Version: 1}}, nil
	}
	if err != nil {
		return nil, err
	}
	state := &State{}
	if err := json.Unmarshal(raw, state); err != nil {
		return nil, fmt.Errorf("%s: %w", filepath.Join(dir, fileName), err)
	}
	return &Store{dir: dir, state: state}, nil
}

// Update runs fn on the state under the lock and saves the result.
func (s *Store) Update(fn func(*State) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := fn(s.state); err != nil {
		return err
	}
	return s.save()
}

// View runs fn on the state under the lock without saving.
func (s *Store) View(fn func(*State)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	fn(s.state)
}

func (s *Store) save() error {
	return WriteFile(s.dir, fileName, s.state)
}

// WriteFile writes value as JSON through a temp file and a rename, so a crash leaves the old file
// or the new one.
func WriteFile(dir, name string, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	temp, err := os.CreateTemp(dir, name+".*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	if _, err := temp.Write(raw); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(temp.Name(), filepath.Join(dir, name))
}
