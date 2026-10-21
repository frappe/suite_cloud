package stalwart

import (
	"math"
	"strconv"
	"strings"
)

// scalar turns a bare token into the JSON value it stands for. A token no rule covers stays a
// string, so IP addresses, timestamps and base64 bytes pass through unchanged.
func scalar(token string) any {
	switch token {
	case "(null)":
		return nil
	case "true":
		return true
	case "false":
		return false
	}
	if n, ok := integer(token); ok {
		return n
	}
	if n, ok := integer(strings.TrimSuffix(token, "ms")); ok && strings.HasSuffix(token, "ms") {
		return n // durations are printed as integer milliseconds
	}
	if f, err := strconv.ParseFloat(token, 64); err == nil && isNumeric(token) && !math.IsInf(f, 0) && !math.IsNaN(f) {
		return f
	}
	return token
}

// integer parses a decimal; a value past int64 is a Stalwart u64 and comes back as uint64.
func integer(token string) (any, bool) {
	if !isNumeric(token) || strings.ContainsAny(token, ".eE") {
		return nil, false
	}
	if n, err := strconv.ParseInt(token, 10, 64); err == nil {
		return n, true
	}
	if n, err := strconv.ParseUint(token, 10, 64); err == nil {
		return n, true
	}
	return nil, false
}

// isNumeric reports whether a token starts like a number, so "inf" and "2026-10-20T..." stay text.
func isNumeric(token string) bool {
	if token == "" {
		return false
	}
	c := token[0]
	if c == '-' && len(token) > 1 {
		c = token[1]
	}
	return c >= '0' && c <= '9'
}
