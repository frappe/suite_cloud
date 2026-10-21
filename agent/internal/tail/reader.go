package tail

import (
	"bufio"
	"io"
	"os"
)

// maxBatchBytes bounds one read, and so one batch, so a run of long lines stays within memory.
var maxBatchBytes int64 = 16 << 20

// ReadLines returns the complete lines of path from offset from, at most maxLines of them and
// stopping after the line that passes maxBatchBytes. With limit > 0 exactly that many bytes are
// read, whole: that is how a pending batch is read again. A trailing line without its newline is
// left for the next read. end is the offset after the last line returned.
func ReadLines(path string, from int64, limit int64, maxLines int) (lines [][]byte, end int64, err error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, from, err
	}
	defer file.Close()
	if _, err := file.Seek(from, io.SeekStart); err != nil {
		return nil, from, err
	}

	var source io.Reader = file
	if limit > 0 {
		source = io.LimitReader(file, limit)
	}
	reader := bufio.NewReaderSize(source, 256<<10)
	end = from
	var bytesRead int64
	for maxLines <= 0 || len(lines) < maxLines {
		line, err := reader.ReadBytes('\n')
		if err != nil {
			if err == io.EOF {
				return lines, end, nil
			}
			return lines, end, err
		}
		lines = append(lines, line)
		end += int64(len(line))
		bytesRead += int64(len(line))
		if limit <= 0 && bytesRead >= maxBatchBytes {
			break
		}
	}
	return lines, end, nil
}
