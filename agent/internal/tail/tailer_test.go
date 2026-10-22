package tail

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/format"
	"github.com/frappe/suite_cloud/agent/internal/state"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

type fakeSender struct {
	batches []telemetry.Batch
	outcome telemetry.Outcome
}

func (f *fakeSender) Send(_ context.Context, batch telemetry.Batch) (telemetry.Outcome, error) {
	f.batches = append(f.batches, batch)
	return f.outcome, nil
}

type harness struct {
	t      *testing.T
	dir    string
	store  *state.Store
	sender *fakeSender
	clock  time.Time
	tailer *Tailer
}

func newHarness(t *testing.T, shipExisting bool, batchSize int) *harness {
	t.Helper()
	h := &harness{t: t, dir: t.TempDir(), sender: &fakeSender{}, clock: time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)}
	h.reopen(shipExisting, batchSize)
	return h
}

// reopen builds a fresh Tailer on the same directory, as a restart would.
func (h *harness) reopen(shipExisting bool, batchSize int) {
	store, err := state.Open(h.dir)
	if err != nil {
		h.t.Fatal(err)
	}
	h.store = store
	parser, _ := format.Lookup("stalwart")
	h.tailer = &Tailer{
		Def:       config.LogDef{Name: "stalwart", Path: filepath.Join(h.dir, "stalwart.*"), Format: "stalwart", Source: "stalwart", ShipExisting: shipExisting, DeleteAfterDays: 0, PollIntervalSeconds: 1, FlushIntervalSeconds: 5},
		Node:      config.Node{Source: "stalwart", HostIP: "203.0.113.5", Hostname: "n1"},
		BatchSize: batchSize,
		Parser:    parser,
		Store:     store,
		Sender:    h.sender,
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
		Now:       func() time.Time { return h.clock },
	}
}

func (h *harness) write(name string, lines ...string) string {
	path := filepath.Join(h.dir, name)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		h.t.Fatal(err)
	}
	defer file.Close()
	for _, line := range lines {
		fmt.Fprintln(file, line)
	}
	return path
}

func (h *harness) step() bool {
	idle, err := h.tailer.Step(context.Background())
	if err != nil {
		h.t.Fatal(err)
	}
	return idle
}

func (h *harness) cursor(path string) *state.Cursor {
	var found *state.Cursor
	h.store.View(func(s *state.State) {
		for _, c := range s.Log("stalwart").Cursors {
			if c.Path == path {
				copied := *c
				found = &copied
			}
		}
	})
	return found
}

func line(i int) string {
	return fmt.Sprintf(`2026-10-20T10:00:%02dZ INFO SMTP EHLO command (smtp.ehlo) listenerId = "smtp", remoteIp = 203.0.113.%d, n = %d`, i%60, i%250, i)
}

func lines(from, to int) []string {
	out := make([]string, 0, to-from)
	for i := from; i < to; i++ {
		out = append(out, line(i))
	}
	return out
}

func TestExistingFilesAreSkippedByDefault(t *testing.T) {
	h := newHarness(t, false, 5000)
	old := h.write("stalwart.2026-10-19", lines(0, 3)...)
	current := h.write("stalwart.2026-10-20", lines(0, 2)...)

	if !h.step() {
		t.Fatal("expected idle")
	}
	if len(h.sender.batches) != 0 {
		t.Fatalf("sent %d batches", len(h.sender.batches))
	}
	if c := h.cursor(old); !c.Finished || c.Offset != size(t, old) {
		t.Fatalf("old cursor %+v", c)
	}
	if c := h.cursor(current); c.Finished || c.Offset != size(t, current) {
		t.Fatalf("current cursor %+v", c)
	}

	h.write("stalwart.2026-10-20", line(7))
	h.step()
	if len(h.sender.batches) != 0 {
		t.Fatal("sent before the flush interval")
	}
	h.clock = h.clock.Add(5 * time.Second)
	h.step()
	if len(h.sender.batches) != 1 || len(h.sender.batches[0].Records) != 1 {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	if got := h.sender.batches[0].Records[0].(telemetry.Event).Data["n"]; got != int64(7) {
		t.Fatalf("record %v", got)
	}
	if c := h.cursor(current); c.Offset != size(t, current) {
		t.Fatalf("cursor %+v", c)
	}
}

func TestExistingFilesAreShippedWhenAsked(t *testing.T) {
	h := newHarness(t, true, 2)
	h.write("stalwart.2026-10-19", lines(0, 2)...)
	h.write("stalwart.2026-10-20", lines(2, 4)...)

	for i := 0; i < 6; i++ {
		h.step()
	}
	if len(h.sender.batches) != 2 {
		t.Fatalf("batches %d", len(h.sender.batches))
	}
	if first := h.sender.batches[0].Records[0].(telemetry.Event).Data["n"]; first != int64(0) {
		t.Fatalf("oldest file first, got n=%v", first)
	}
	if h.sender.batches[0].ID >= h.sender.batches[1].ID {
		t.Fatal("batch ids are not ordered")
	}
	if c := h.cursor(filepath.Join(h.dir, "stalwart.2026-10-19")); !c.Finished {
		t.Fatalf("old file not finished: %+v", c)
	}
}

func TestPartialLastLineWaits(t *testing.T) {
	h := newHarness(t, true, 5000)
	path := filepath.Join(h.dir, "stalwart.2026-10-20")
	os.WriteFile(path, []byte(line(1)+"\n"+"2026-10-20T10:00:02Z INFO Half"), 0o644)

	h.clock = h.clock.Add(time.Minute)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	if len(h.sender.batches) != 1 || len(h.sender.batches[0].Records) != 1 {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	if c := h.cursor(path); c.Offset != int64(len(line(1))+1) {
		t.Fatalf("offset %d", c.Offset)
	}

	h.write("stalwart.2026-10-20", ` line (a.b) n = 2`)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	if len(h.sender.batches) != 2 || h.sender.batches[1].Records[0].(telemetry.Event).Event != "a.b" {
		t.Fatalf("batches %+v", h.sender.batches)
	}
}

func TestRotationFinishesTheOldFile(t *testing.T) {
	h := newHarness(t, true, 5000)
	old := h.write("stalwart.2026-10-20", lines(0, 2)...)
	h.clock = h.clock.Add(time.Minute)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step() // ships the two lines
	if len(h.sender.batches) != 1 {
		t.Fatalf("batches %d", len(h.sender.batches))
	}
	if c := h.cursor(old); c.Finished {
		t.Fatal("finished without a newer file")
	}

	current := h.write("stalwart.2026-10-21", lines(5, 6)...)
	h.step() // old file: nothing new, newer exists
	if c := h.cursor(old); !c.Finished {
		t.Fatalf("old not finished: %+v", c)
	}
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	if len(h.sender.batches) != 2 || h.sender.batches[1].Records[0].(telemetry.Event).Data["n"] != int64(5) {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	if c := h.cursor(current); c.Finished || c.Offset != size(t, current) {
		t.Fatalf("current %+v", c)
	}
}

func TestPendingBatchIsResentUnderTheSameId(t *testing.T) {
	h := newHarness(t, true, 5000)
	path := h.write("stalwart.2026-10-20", lines(0, 4)...)
	files, _ := Discover(h.tailer.Def.Path)
	start := int64(len(line(0)) + 1)
	end := start + int64(len(line(1))+1+len(line(2))+1)
	err := h.store.Update(func(s *state.State) error {
		log := s.Log("stalwart")
		log.Initialised = true
		log.Cursors = []*state.Cursor{{Path: path, Identity: files[0].Identity, Offset: start}}
		log.Pending = &state.Pending{BatchID: "0199f7e0-0000-7000-8000-000000000001", Path: path, Identity: files[0].Identity, Start: start, End: end, Records: 2}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	h.reopen(true, 5000)
	if err := h.tailer.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(h.sender.batches) != 1 || h.sender.batches[0].ID != "0199f7e0-0000-7000-8000-000000000001" {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	records := h.sender.batches[0].Records
	if len(records) != 2 || records[0].(telemetry.Event).Data["n"] != int64(1) || records[1].(telemetry.Event).Data["n"] != int64(2) {
		t.Fatalf("records %+v", records)
	}
	if c := h.cursor(path); c.Offset != end {
		t.Fatalf("offset %d, want %d", c.Offset, end)
	}
	h.store.View(func(s *state.State) {
		if s.Log("stalwart").Pending != nil {
			t.Fatal("pending not cleared")
		}
	})

	// The rest of the file follows under a new id.
	h.clock = h.clock.Add(time.Minute)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	if len(h.sender.batches) != 2 || h.sender.batches[1].Records[0].(telemetry.Event).Data["n"] != int64(3) {
		t.Fatalf("batches %+v", h.sender.batches)
	}
}

func TestLongLinesBoundTheBatchAndRecoverWhole(t *testing.T) {
	previous := maxBatchBytes
	maxBatchBytes = 4096
	defer func() { maxBatchBytes = previous }()
	h := newHarness(t, true, 5000)
	long := make([]string, 0, 40)
	for i := 0; i < 40; i++ {
		long = append(long, line(i)+`, details = "`+strings.Repeat("x", 300)+`"`)
	}
	path := h.write("stalwart.2026-10-20", long...)

	h.step()
	if len(h.sender.batches) != 1 || len(h.sender.batches[0].Records) >= 40 {
		t.Fatalf("a batch past the byte bound was not shipped early: %d batches", len(h.sender.batches))
	}
	first := len(h.sender.batches[0].Records)

	// A pending batch larger than one read must come back whole after a restart.
	files, _ := Discover(h.tailer.Def.Path)
	err := h.store.Update(func(s *state.State) error {
		log := s.Log("stalwart")
		log.Cursors[0].Offset = 0
		log.Pending = &state.Pending{BatchID: "0199f7e0-0000-7000-8000-000000000002", Path: path, Identity: files[0].Identity, Start: 0, End: size(t, path), Records: 40}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	h.reopen(true, 5000)
	if err := h.tailer.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	resent := h.sender.batches[len(h.sender.batches)-1]
	if resent.ID != "0199f7e0-0000-7000-8000-000000000002" || len(resent.Records) != 40 {
		t.Fatalf("recovered %d of 40 records (first batch had %d)", len(resent.Records), first)
	}
	if c := h.cursor(path); c.Offset != size(t, path) {
		t.Fatalf("cursor %+v", c)
	}
}

func TestPendingBatchOfAVanishedFileIsDropped(t *testing.T) {
	h := newHarness(t, true, 5000)
	err := h.store.Update(func(s *state.State) error {
		log := s.Log("stalwart")
		log.Initialised = true
		log.Pending = &state.Pending{BatchID: "x", Path: filepath.Join(h.dir, "stalwart.2026-10-01"), Identity: state.Identity{Dev: 1, Inode: 1}, Start: 0, End: 10}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := h.tailer.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(h.sender.batches) != 0 {
		t.Fatal("sent something")
	}
	h.store.View(func(s *state.State) {
		if s.Log("stalwart").Pending != nil {
			t.Fatal("pending kept")
		}
	})
}

func TestTruncationStartsOver(t *testing.T) {
	h := newHarness(t, true, 5000)
	path := h.write("stalwart.2026-10-20", lines(0, 3)...)
	h.clock = h.clock.Add(time.Minute)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()

	os.WriteFile(path, []byte(line(9)+"\n"), 0o644)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	if len(h.sender.batches) != 2 || h.sender.batches[1].Records[0].(telemetry.Event).Data["n"] != int64(9) {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	if c := h.cursor(path); c.Offset != size(t, path) {
		t.Fatalf("cursor %+v", c)
	}
}

func TestReplacedFileGetsANewCursor(t *testing.T) {
	h := newHarness(t, true, 5000)
	path := h.write("stalwart.2026-10-20", lines(0, 2)...)
	h.clock = h.clock.Add(time.Minute)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	before := h.cursor(path)

	replacement := h.write("stalwart.tmp", lines(5, 8)...) // another inode, moved into place
	if err := os.Rename(replacement, path); err != nil {
		t.Fatal(err)
	}
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	after := h.cursor(path)
	if after.Identity == before.Identity || after.Offset != size(t, path) {
		t.Fatalf("before %+v after %+v", before, after)
	}
	if len(h.sender.batches) != 2 || len(h.sender.batches[1].Records) != 3 {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	h.store.View(func(s *state.State) {
		if n := len(s.Log("stalwart").Cursors); n != 1 {
			t.Fatalf("%d cursors", n)
		}
	})
}

func TestBatchingBySizeAndFlush(t *testing.T) {
	h := newHarness(t, true, 5000)
	h.write("stalwart.2026-10-20", lines(0, 12000)...)

	h.step()
	h.step()
	if len(h.sender.batches) != 2 || len(h.sender.batches[0].Records) != 5000 || len(h.sender.batches[1].Records) != 5000 {
		t.Fatalf("batches %d", len(h.sender.batches))
	}
	h.step()
	if len(h.sender.batches) != 2 {
		t.Fatal("remainder sent before the flush interval")
	}
	h.clock = h.clock.Add(5 * time.Second)
	h.step()
	if len(h.sender.batches) != 3 || len(h.sender.batches[2].Records) != 2000 {
		t.Fatalf("batches %d", len(h.sender.batches))
	}
	if last := h.sender.batches[2].Records[1999].(telemetry.Event).Data["n"]; last != int64(11999) {
		t.Fatalf("last record n=%v", last)
	}
}

func TestUnparsedLinesAdvanceTheCursor(t *testing.T) {
	h := newHarness(t, true, 5000)
	path := h.write("stalwart.2026-10-20", "garbage", line(1), "more garbage")
	h.clock = h.clock.Add(time.Minute)
	h.step()
	h.clock = h.clock.Add(time.Minute)
	h.step()
	if len(h.sender.batches) != 1 || len(h.sender.batches[0].Records) != 1 {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	if c := h.cursor(path); c.Offset != size(t, path) {
		t.Fatalf("cursor %+v", c)
	}

	onlyGarbage := h.write("stalwart.2026-10-21", "nothing here")
	h.step()
	h.step()
	if c := h.cursor(onlyGarbage); c.Offset != size(t, onlyGarbage) {
		t.Fatalf("cursor %+v", c)
	}
	if len(h.sender.batches) != 1 {
		t.Fatal("an empty batch was sent")
	}
}

func TestSkippedBatchMovesOn(t *testing.T) {
	h := newHarness(t, true, 2)
	h.sender.outcome = telemetry.Skipped
	path := h.write("stalwart.2026-10-20", lines(0, 4)...)
	h.step()
	h.step()
	if len(h.sender.batches) != 2 || h.sender.batches[0].ID == h.sender.batches[1].ID {
		t.Fatalf("batches %+v", h.sender.batches)
	}
	if c := h.cursor(path); c.Offset != size(t, path) {
		t.Fatalf("cursor %+v", c)
	}
}

func TestPruneDeletesShippedOldFiles(t *testing.T) {
	h := newHarness(t, false, 5000)
	h.tailer.Def.DeleteAfterDays = 7
	old := h.write("stalwart.2026-10-01", lines(0, 2)...)
	recent := h.write("stalwart.2026-10-18", lines(0, 2)...)
	current := h.write("stalwart.2026-10-20", lines(0, 2)...)
	for _, path := range []string{old, recent, current} {
		os.Chtimes(path, h.clock, time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC))
	}
	os.Chtimes(recent, h.clock, h.clock.Add(-2*24*time.Hour))

	h.step()
	for _, path := range []string{old, recent, current} {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("%s removed before the prune interval passed: %v", path, err)
		}
	}
	h.clock = h.clock.Add(pruneInterval)
	h.step()
	if _, err := os.Stat(old); !os.IsNotExist(err) {
		t.Fatal("old shipped file kept")
	}
	for _, path := range []string{recent, current} {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("%s deleted", path)
		}
	}
	if h.cursor(old) != nil {
		t.Fatal("cursor of the deleted file kept")
	}

	h.tailer.Def.DeleteAfterDays = 0
	os.Chtimes(current, h.clock, time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	h.clock = h.clock.Add(pruneInterval)
	h.step()
	if _, err := os.Stat(current); err != nil {
		t.Fatal("deleted with delete_after_days 0")
	}
}

func TestReadLinesHonoursLimits(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "f")
	os.WriteFile(path, []byte("a\nbb\nccc\ndddd"), 0o644)

	lines, end, err := ReadLines(path, 0, 0, 2)
	if err != nil || len(lines) != 2 || end != 5 {
		t.Fatalf("%v %d %v", lines, end, err)
	}
	lines, end, _ = ReadLines(path, 5, 0, 0)
	if len(lines) != 1 || string(lines[0]) != "ccc\n" || end != 9 {
		t.Fatalf("partial line kept: %q %d", lines, end)
	}
	lines, end, _ = ReadLines(path, 2, 3, 0)
	if len(lines) != 1 || string(lines[0]) != "bb\n" || end != 5 {
		t.Fatalf("limit: %q %d", lines, end)
	}
}

func size(t *testing.T, path string) int64 {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info.Size()
}
