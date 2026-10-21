// Package tail follows a set of log files and ships their lines in batches, once each.
package tail

import (
	"os"
	"path/filepath"
	"sort"
	"syscall"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/state"
)

// FileInfo is one file matching a log's glob.
type FileInfo struct {
	Path string
	state.Identity
	Size    int64
	ModTime time.Time
}

// Discover lists the files a glob matches, oldest name first. Stalwart names rotated files by
// date, so the name order is the time order. A file that vanishes between glob and stat is left out.
func Discover(pattern string) ([]FileInfo, error) {
	paths, err := filepath.Glob(pattern)
	if err != nil {
		return nil, err
	}
	sort.Strings(paths)
	files := make([]FileInfo, 0, len(paths))
	for _, path := range paths {
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		files = append(files, FileInfo{Path: path, Identity: identity(info), Size: info.Size(), ModTime: info.ModTime()})
	}
	return files, nil
}

func identity(info os.FileInfo) state.Identity {
	if stat, ok := info.Sys().(*syscall.Stat_t); ok {
		return state.Identity{Dev: uint64(stat.Dev), Inode: uint64(stat.Ino)} //nolint:unconvert // Dev is int32 on some platforms
	}
	return state.Identity{}
}

func newest(files []FileInfo) string {
	if len(files) == 0 {
		return ""
	}
	return files[len(files)-1].Path
}

func find(files []FileInfo, id state.Identity) (FileInfo, bool) {
	for _, file := range files {
		if file.Identity == id {
			return file, true
		}
	}
	return FileInfo{}, false
}
