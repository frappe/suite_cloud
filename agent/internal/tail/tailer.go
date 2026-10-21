package tail

import (
	"context"
	"log/slog"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/format"
	"github.com/frappe/suite_cloud/agent/internal/state"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

const pruneInterval = time.Minute

// Tailer ships one configured log. Lines are read in file order, batched, and committed to the
// state only once Suite Cloud has taken the batch or the sender has given it up.
type Tailer struct {
	Def       config.LogDef
	Node      config.Node
	BatchSize int
	Parser    format.Parser
	Store     *state.Store
	Sender    telemetry.Sender
	Logger    *slog.Logger
	Now       func() time.Time

	buffer     []any
	bufferFile state.Identity
	bufferEnd  int64
	bufferedAt time.Time
	unparsed   int
	lastPrune  time.Time
}

// Run resends a pending batch, then follows the files until the context ends.
func (t *Tailer) Run(ctx context.Context) error {
	if err := t.Recover(ctx); err != nil {
		return err
	}
	for {
		idle, err := t.Step(ctx)
		if err != nil {
			return err
		}
		if idle {
			if err := sleep(ctx, t.Def.PollInterval()); err != nil {
				return err
			}
		}
	}
}

// Step reads and ships what one pass finds. idle means nothing was there to do.
func (t *Tailer) Step(ctx context.Context) (idle bool, err error) {
	files, err := Discover(t.Def.Path)
	if err != nil {
		return true, err
	}
	if t.now().Sub(t.lastPrune) >= pruneInterval {
		files = t.Prune(files)
		t.lastPrune = t.now()
	}
	cursor, hasNewer := t.reconcile(files)
	if cursor == nil {
		return true, nil
	}

	if len(t.buffer) > 0 && t.bufferFile != cursor.Identity {
		t.Logger.Warn("buffered lines dropped: their file is gone", "records", len(t.buffer))
		t.buffer, t.bufferedAt = nil, time.Time{}
	}
	from := cursor.Offset
	if len(t.buffer) > 0 {
		from = t.bufferEnd
	}
	lines, end, err := ReadLines(cursor.Path, from, 0, t.BatchSize-len(t.buffer))
	if err != nil {
		t.Logger.Warn("log file not readable", "path", cursor.Path, "error", err)
		return true, nil
	}
	t.append(cursor, lines, end)

	switch {
	case len(t.buffer) >= t.BatchSize, end-cursor.Offset >= maxBatchBytes, len(t.buffer) == 0 && end > cursor.Offset:
		return false, t.ship(ctx, cursor)
	case end > from:
		if t.flushDue() {
			return false, t.ship(ctx, cursor)
		}
		return false, nil
	case t.flushDue():
		return false, t.ship(ctx, cursor)
	case hasNewer:
		if err := t.ship(ctx, cursor); err != nil {
			return false, err
		}
		return false, t.finish(cursor)
	}
	return true, nil
}

// Recover resends the batch a previous run had in flight, under the same id.
func (t *Tailer) Recover(ctx context.Context) error {
	var pending *state.Pending
	t.Store.View(func(s *state.State) {
		if p := s.Log(t.Def.Name).Pending; p != nil {
			copied := *p
			pending = &copied
		}
	})
	if pending == nil {
		return nil
	}

	files, _ := Discover(t.Def.Path)
	file, found := find(files, pending.Identity)
	if !found {
		t.Logger.Warn("pending batch dropped: its file is gone", "batch_id", pending.BatchID, "path", pending.Path, "start", pending.Start, "end", pending.End)
		return t.commit(pending.Identity, pending.End)
	}
	lines, end, err := ReadLines(file.Path, pending.Start, pending.End-pending.Start, 0)
	if err != nil {
		return err
	}
	if end != pending.End {
		t.Logger.Warn("pending batch is shorter than recorded", "batch_id", pending.BatchID, "path", file.Path, "end", end, "recorded_end", pending.End)
	}
	t.Logger.Info("resending pending batch", "batch_id", pending.BatchID, "path", file.Path, "start", pending.Start, "end", pending.End, "records", pending.Records)

	records := t.parse(lines)
	if len(records) > 0 {
		if err := t.send(ctx, telemetry.Batch{ID: pending.BatchID, Kind: telemetry.Events, Source: t.Def.Source, HostIP: t.Node.HostIP, Hostname: t.Node.Hostname, Records: records}, pending); err != nil {
			return err
		}
	}
	return t.commit(pending.Identity, pending.End)
}

// reconcile matches the files on disk with the cursors and returns a copy of the one to read next
// and whether a newer file than it exists.
func (t *Tailer) reconcile(files []FileInfo) (next *state.Cursor, hasNewer bool) {
	_ = t.Store.Update(func(s *state.State) error {
		log := s.Log(t.Def.Name)
		if !log.Initialised {
			t.initialise(log, files)
		}
		for _, file := range files {
			cursor := log.Cursor(file.Identity)
			if cursor == nil {
				cursor = t.adopt(log, file, files)
			}
			cursor.Path = file.Path
			if file.Size < cursor.Offset {
				t.Logger.Warn("log file truncated; reading it from the start", "path", file.Path, "offset", cursor.Offset, "size", file.Size)
				cursor.Offset, cursor.Finished = 0, false
				if t.bufferFile == file.Identity {
					t.buffer, t.bufferEnd = nil, 0
				}
			} else if cursor.Finished && file.Size > cursor.Offset {
				t.Logger.Warn("finished log file grew; reading it again", "path", file.Path)
				cursor.Finished = false
			}
		}
		t.forgetMissing(log, files)

		for _, cursor := range log.Cursors {
			if _, present := find(files, cursor.Identity); present && !cursor.Finished {
				copied := *cursor
				next = &copied
				hasNewer = cursor.Path < newest(files)
				break
			}
		}
		return nil
	})
	return next, hasNewer
}

// initialise applies ship_existing on first contact: either everything is shipped from the start,
// or the existing files count as done and only new lines are followed.
func (t *Tailer) initialise(log *state.LogState, files []FileInfo) {
	var skipped int64
	for _, file := range files {
		cursor := &state.Cursor{Path: file.Path, Identity: file.Identity}
		if !t.Def.ShipExisting {
			cursor.Offset = file.Size
			skipped += file.Size
			if file.Path != newest(files) {
				cursor.Finished, cursor.FinishedAt = true, t.now()
			}
		}
		log.Cursors = append(log.Cursors, cursor)
	}
	log.Initialised = true
	t.Logger.Info("log initialised", "files", len(files), "ship_existing", t.Def.ShipExisting, "skipped_bytes", skipped)
}

// adopt starts a cursor for a file seen for the first time. A different file at a known path
// replaced the old one, whose cursor ends here.
func (t *Tailer) adopt(log *state.LogState, file FileInfo, files []FileInfo) *state.Cursor {
	for _, old := range log.Cursors {
		if old.Path == file.Path {
			if _, present := find(files, old.Identity); !present && !old.Finished {
				old.Finished, old.FinishedAt = true, t.now()
				t.Logger.Warn("log file replaced", "path", file.Path, "offset", old.Offset)
			}
		}
	}
	cursor := &state.Cursor{Path: file.Path, Identity: file.Identity}
	log.Cursors = append(log.Cursors, cursor)
	t.Logger.Info("new log file", "path", file.Path)
	return cursor
}

// forgetMissing drops cursors of files that are gone, unless a pending batch still names one.
func (t *Tailer) forgetMissing(log *state.LogState, files []FileInfo) {
	kept := log.Cursors[:0]
	for _, cursor := range log.Cursors {
		_, present := find(files, cursor.Identity)
		if present || (log.Pending != nil && log.Pending.Identity == cursor.Identity) {
			kept = append(kept, cursor)
		} else {
			t.Logger.Info("log file gone", "path", cursor.Path, "shipped_to", cursor.Offset)
		}
	}
	log.Cursors = kept
}

func (t *Tailer) append(cursor *state.Cursor, lines [][]byte, end int64) {
	records := t.parse(lines)
	if len(records) > 0 && len(t.buffer) == 0 {
		t.bufferedAt = t.now()
	}
	t.buffer = append(t.buffer, records...)
	t.bufferFile, t.bufferEnd = cursor.Identity, end
}

func (t *Tailer) parse(lines [][]byte) []any {
	records := make([]any, 0, len(lines))
	for _, line := range lines {
		event, err := t.Parser.Parse(line)
		if err != nil {
			t.unparsed++
			if t.unparsed <= 5 {
				t.Logger.Warn("line not parsed", "error", err, "line", truncate(line, 200))
			}
			continue
		}
		records = append(records, event)
	}
	return records
}

func (t *Tailer) flushDue() bool {
	return !t.bufferedAt.IsZero() && t.now().Sub(t.bufferedAt) >= t.Def.FlushInterval()
}

// ship sends the buffered records as one batch, then moves the cursor past them. Bytes that held
// no record (unparsed lines) are committed without a request.
func (t *Tailer) ship(ctx context.Context, cursor *state.Cursor) error {
	if t.bufferFile != cursor.Identity || t.bufferEnd <= cursor.Offset {
		return nil
	}
	if len(t.buffer) > 0 {
		pending := &state.Pending{BatchID: telemetry.NewUUIDv7(), Path: cursor.Path, Identity: cursor.Identity, Start: cursor.Offset, End: t.bufferEnd, Records: len(t.buffer)}
		if err := t.Store.Update(func(s *state.State) error { s.Log(t.Def.Name).Pending = pending; return nil }); err != nil {
			return err
		}
		batch := telemetry.Batch{ID: pending.BatchID, Kind: telemetry.Events, Source: t.Def.Source, HostIP: t.Node.HostIP, Hostname: t.Node.Hostname, Records: t.buffer}
		if err := t.send(ctx, batch, pending); err != nil {
			return err
		}
	}
	if err := t.commit(cursor.Identity, t.bufferEnd); err != nil {
		return err
	}
	t.buffer, t.bufferedAt = nil, time.Time{}
	return nil
}

func (t *Tailer) send(ctx context.Context, batch telemetry.Batch, pending *state.Pending) error {
	outcome, err := t.Sender.Send(ctx, batch)
	if err != nil {
		return err
	}
	if outcome == telemetry.Skipped {
		t.Logger.Error("log batch skipped; these lines are lost", "batch_id", batch.ID, "path", pending.Path, "start", pending.Start, "end", pending.End, "records", pending.Records)
	} else {
		t.Logger.Debug("log batch sent", "batch_id", batch.ID, "path", pending.Path, "records", pending.Records, "unparsed_total", t.unparsed)
	}
	return nil
}

// commit records that everything before end in the file is done and clears the pending batch.
func (t *Tailer) commit(id state.Identity, end int64) error {
	return t.Store.Update(func(s *state.State) error {
		log := s.Log(t.Def.Name)
		if cursor := log.Cursor(id); cursor != nil {
			cursor.Offset = end
		}
		log.Pending = nil
		return nil
	})
}

func (t *Tailer) finish(cursor *state.Cursor) error {
	t.Logger.Info("log file finished", "path", cursor.Path, "size", t.bufferEnd)
	return t.Store.Update(func(s *state.State) error {
		if c := s.Log(t.Def.Name).Cursor(cursor.Identity); c != nil {
			c.Finished, c.FinishedAt = true, t.now()
		}
		return nil
	})
}

func (t *Tailer) now() time.Time {
	if t.Now != nil {
		return t.Now()
	}
	return time.Now()
}

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func truncate(line []byte, n int) string {
	if len(line) > n {
		return string(line[:n]) + "..."
	}
	return string(line)
}
