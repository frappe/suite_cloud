// Package stalwart parses the lines of Stalwart's text log tracer.
//
// A line reads `<RFC 3339> <LEVEL> <description> (<event.name>) key = value, key = value`, as
// written by crates/trc/src/serializers/text.rs in Stalwart: span keys first, then the event's own
// keys, so a key may repeat and the later value wins, as in Stalwart's JSON tracer. The span id is
// never written to the text log.
package stalwart

import (
	"bytes"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

const (
	maxEventLength = 128
	maxLevelLength = 16
)

var levels = map[string]string{
	"TRACE": "trace", "DEBUG": "debug", "INFO": "info", "WARN": "warn", "ERROR": "error",
}

// Parser implements format.Parser for Stalwart's text log.
type Parser struct{}

func (Parser) Parse(line []byte) (telemetry.Event, error) {
	return Parse(line)
}

// Parse turns one line into an event. The description goes into data as "description".
func Parse(line []byte) (telemetry.Event, error) {
	line = StripANSI(bytes.TrimRight(line, "\r\n"))
	timestamp, level, rest, err := parseHeader(line)
	if err != nil {
		return telemetry.Event{}, err
	}
	description, name, fields, err := splitEvent(rest)
	if err != nil {
		return telemetry.Event{}, err
	}

	data := map[string]any{"description": description}
	scanner := &scanner{buf: fields}
	if err := scanner.pairs(data, 0); err != nil {
		return telemetry.Event{}, fmt.Errorf("fields at byte %d: %w", scanner.pos, err)
	}
	return telemetry.Event{Timestamp: timestamp, Level: level, Event: name, Data: data}, nil
}

// parseHeader takes the timestamp and the level off the line.
func parseHeader(line []byte) (timestamp, level string, rest []byte, err error) {
	first := bytes.IndexByte(line, ' ')
	if first <= 0 {
		return "", "", nil, errors.New("no timestamp")
	}
	timestamp = string(line[:first])
	if _, err := time.Parse(time.RFC3339, timestamp); err != nil {
		return "", "", nil, fmt.Errorf("timestamp %q: %w", timestamp, err)
	}

	line = line[first+1:]
	second := bytes.IndexByte(line, ' ')
	if second <= 0 {
		return "", "", nil, errors.New("no level")
	}
	level, ok := levels[string(line[:second])]
	if !ok || len(level) > maxLevelLength {
		return "", "", nil, fmt.Errorf("unknown level %q", line[:second])
	}
	return timestamp, level, line[second+1:], nil
}

// splitEvent finds `description (event.name)` and returns what follows the closing parenthesis.
// The description is free text, so the event name is the first parenthesised dotted name.
func splitEvent(rest []byte) (description, name string, fields []byte, err error) {
	for start := 0; start < len(rest); {
		open := bytes.Index(rest[start:], []byte(" ("))
		if open < 0 {
			break
		}
		open += start
		end := eventNameEnd(rest, open+2)
		if end > 0 && (end == len(rest) || rest[end] == ' ') {
			name = string(rest[open+2 : end-1])
			if len(name) > maxEventLength {
				return "", "", nil, fmt.Errorf("event name longer than %d", maxEventLength)
			}
			return string(rest[:open]), name, bytes.TrimLeft(rest[end:], " "), nil
		}
		start = open + 2
	}
	return "", "", nil, errors.New("no event name")
}

// eventNameEnd returns the index after the ")" that closes a dotted event name starting at from,
// or 0 when the text there is not one.
func eventNameEnd(buf []byte, from int) int {
	dotted := false
	for i := from; i < len(buf); i++ {
		switch c := buf[i]; {
		case c == ')':
			if dotted && i > from && buf[i-1] != '.' {
				return i + 1
			}
			return 0
		case c == '.':
			if i == from || buf[i-1] == '.' {
				return 0
			}
			dotted = true
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-':
		default:
			return 0
		}
	}
	return 0
}

// isEventName reports whether text has the shape `description (event.name)`.
func isEventName(text string) (description, name string, ok bool) {
	if !strings.HasSuffix(text, ")") {
		return "", "", false
	}
	open := strings.LastIndex(text, " (")
	if open < 0 {
		return "", "", false
	}
	if eventNameEnd([]byte(text), open+2) != len(text) {
		return "", "", false
	}
	return text[:open], text[open+2 : len(text)-1], true
}
