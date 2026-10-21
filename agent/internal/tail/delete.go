package tail

import (
	"os"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/state"
)

// Prune deletes rotated files that were shipped whole and are older than delete_after_days, and
// returns the files that remain. The newest file, an unfinished file and the file of a pending
// batch are never touched.
func (t *Tailer) Prune(files []FileInfo) []FileInfo {
	if t.Def.DeleteAfterDays <= 0 {
		return files
	}
	cutoff := t.now().Add(-time.Duration(t.Def.DeleteAfterDays) * 24 * time.Hour)
	latest := newest(files)
	kept := files[:0]
	_ = t.Store.Update(func(s *state.State) error {
		log := s.Log(t.Def.Name)
		for _, file := range files {
			cursor := log.Cursor(file.Identity)
			shipped := cursor != nil && cursor.Finished && cursor.Offset >= file.Size
			inFlight := log.Pending != nil && log.Pending.Identity == file.Identity
			if !shipped || inFlight || file.Path == latest || !file.ModTime.Before(cutoff) {
				kept = append(kept, file)
				continue
			}
			if err := os.Remove(file.Path); err != nil {
				t.Logger.Warn("log file not deleted", "path", file.Path, "error", err)
				kept = append(kept, file)
				continue
			}
			log.Drop(file.Identity)
			t.Logger.Info("shipped log file deleted", "path", file.Path, "age_days", int(t.now().Sub(file.ModTime).Hours()/24))
		}
		return nil
	})
	return kept
}
