// Package format maps a log format name from the config to its parser.
package format

import (
	"github.com/frappe/suite_cloud/agent/internal/format/stalwart"
	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

// Parser turns one log line into an event. A line of another shape returns an error.
type Parser interface {
	Parse(line []byte) (telemetry.Event, error)
}

// Lookup returns the parser for a format name from config.json.
func Lookup(name string) (Parser, bool) {
	switch name {
	case "stalwart":
		return stalwart.Parser{}, true
	}
	return nil, false
}
