package stalwart

import "bytes"

// StripANSI removes colour sequences (ESC [ ... m) from a line. Nodes set up before the tracer's
// ansi switch-off wrote coloured files, and those files are still shipped.
func StripANSI(line []byte) []byte {
	if bytes.IndexByte(line, 0x1b) < 0 {
		return line
	}
	out := make([]byte, 0, len(line))
	for i := 0; i < len(line); i++ {
		if line[i] == 0x1b && i+1 < len(line) && line[i+1] == '[' {
			end := bytes.IndexByte(line[i:], 'm')
			if end > 0 {
				i += end
				continue
			}
		}
		out = append(out, line[i])
	}
	return out
}
