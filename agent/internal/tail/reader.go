package tail

import (
	"bufio"
	"io"
	"os"
)

// maxBatchBytes bounds one read so a batch of long lines stays within memory.
const maxBatchBytes = 16 << 20

// ReadLines returns the complete lines of path from offset from, at most maxLines of them and
// stopping after the line that passes maxBatchBytes. With limit > 0 only that many bytes are
// read, which is how a pending batch is read again. A trailing line without its newline is left
// for the next read. end is the offset after the last line returned.
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
	bytesRead := 0
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
		bytesRead += len(line)
		if bytesRead >= maxBatchBytes {
			break
		}
	}
	return lines, end, nil
}
