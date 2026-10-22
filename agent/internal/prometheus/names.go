// Package prometheus scrapes Stalwart's Prometheus endpoint and turns the exposition into metric
// records under Stalwart's own metric names.
package prometheus

import "strings"

// ToStalwartID undoes the exporter's renaming. Stalwart writes every non-alphanumeric character
// of an id as "_"; its ids are "group.name-with-hyphens" and never hold "_", so the first "_" was
// the dot and the rest were hyphens: server_memory -> server.memory, delivery_total_time ->
// delivery.total-time.
func ToStalwartID(name string) string {
	group, rest, found := strings.Cut(name, "_")
	if !found {
		return name
	}
	return group + "." + strings.ReplaceAll(rest, "_", "-")
}
