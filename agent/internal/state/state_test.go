package state

import (
	"os"
	"path/filepath"
	"testing"
)

func TestOpenEmptyAndRoundTrip(t *testing.T) {
	dir := t.TempDir()
	store, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	err = store.Update(func(s *State) error {
		log := s.Log("stalwart")
		log.Initialised = true
		log.Cursors = append(log.Cursors, &Cursor{Path: "/x/a", Identity: Identity{1, 2}, Offset: 10})
		log.Pending = &Pending{BatchID: "b", Path: "/x/a", Identity: Identity{1, 2}, Start: 10, End: 20, Records: 3}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	again, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	again.View(func(s *State) {
		log := s.Log("stalwart")
		if !log.Initialised || len(log.Cursors) != 1 || log.Cursors[0].Offset != 10 || log.Pending.End != 20 {
			t.Fatalf("state %+v", log)
		}
		if log.Cursor(Identity{1, 2}) == nil || log.Cursor(Identity{9, 9}) != nil {
			t.Fatal("cursor lookup")
		}
		log.Drop(Identity{1, 2})
		if len(log.Cursors) != 0 {
			t.Fatal("drop")
		}
	})

	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 || entries[0].Name() != "state.json" {
		t.Fatalf("leftovers: %v", entries)
	}
}

func TestOpenRefusesCorruptFile(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "state.json"), []byte("{not json"), 0o600)
	if _, err := Open(dir); err == nil {
		t.Fatal("opened a corrupt state file")
	}
}
