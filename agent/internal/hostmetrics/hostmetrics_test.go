package hostmetrics

import (
	"fmt"
	"testing"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

var fixtures = map[string]string{
	"/proc/stat":    "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8\n",
	"/proc/loadavg": "0.52 0.58 0.59 1/1270 12345\n",
	"/proc/meminfo": "MemTotal:       16000000 kB\nMemFree:        1000000 kB\nMemAvailable:   12000000 kB\nSwapTotal:      2000000 kB\nSwapFree:       1500000 kB\n",
	"/proc/uptime":  "350735.47 234388.90\n",
	"/proc/diskstats": "   8       0 sda 100 0 2000 50 200 0 4000 80 0 100 130\n" +
		"   8       1 sda1 100 0 2000 50 200 0 4000 80 0 100 130\n" +
		"   7       0 loop0 1 0 8 0 0 0 0 0 0 0 0\n",
	"/proc/net/dev": "Inter-|   Receive                                                |  Transmit\n" +
		" face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n" +
		"    lo: 500 5 0 0 0 0 0 0 500 5 0 0 0 0 0 0\n" +
		"  eth0: 10000 100 0 0 0 0 0 0 20000 200 0 0 0 0 0 0\n",
}

func collector(files map[string]string) *Collector {
	c := New([]string{"/"})
	c.readFile = func(path string) ([]byte, error) {
		text, ok := files[path]
		if !ok {
			return nil, fmt.Errorf("%s: no fixture", path)
		}
		return []byte(text), nil
	}
	c.isDisk = func(name string) bool { return name == "sda" }
	c.statfs = func(string) (uint64, uint64, error) { return 100e9, 40e9, nil }
	return c
}

func byName(metrics []telemetry.Metric) map[string]float64 {
	out := map[string]float64{}
	for _, m := range metrics {
		key := m.Name
		for _, v := range m.Labels {
			key += "{" + v + "}"
		}
		out[key] = m.Value
	}
	return out
}

func TestFirstSampleHasNoRates(t *testing.T) {
	c := collector(fixtures)
	metrics, err := c.Collect(time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	values := byName(metrics)
	want := map[string]float64{
		"host.load.1m": 0.52, "host.load.5m": 0.58, "host.load.15m": 0.59,
		"host.memory.total.bytes": 16000000 * 1024, "host.memory.used.bytes": 4000000 * 1024, "host.swap.used.bytes": 500000 * 1024,
		"host.disk.total.bytes{/}": 100e9, "host.disk.used.bytes{/}": 40e9,
		"host.uptime.seconds": 350735.47,
	}
	for name, value := range want {
		if values[name] != value {
			t.Errorf("%s = %v, want %v", name, values[name], value)
		}
	}
	for _, name := range []string{"host.cpu.percent", "host.disk.read.bytes_per_second{sda}", "host.network.receive.bytes_per_second{eth0}"} {
		if _, ok := values[name]; ok {
			t.Errorf("%s reported on the first sample", name)
		}
	}
	if metrics[0].Timestamp != "2026-10-20T00:00:00Z" || metrics[0].Type != "gauge" {
		t.Fatalf("record %+v", metrics[0])
	}
}

func TestSecondSampleHasRates(t *testing.T) {
	c := collector(fixtures)
	start := time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC)
	c.Collect(start)

	later := map[string]string{}
	for k, v := range fixtures {
		later[k] = v
	}
	later["/proc/stat"] = "cpu  1500 0 500 8500 500 0 0 0 0 0\n" // 500 busy of 1000 total
	later["/proc/diskstats"] = "   8       0 sda 100 0 4000 50 200 0 5000 80 0 100 130\n"
	later["/proc/net/dev"] = fixtures["/proc/net/dev"] + "  eth1: 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n"
	later["/proc/net/dev"] = replaceLine(later["/proc/net/dev"], "  eth0: 10000 100 0 0 0 0 0 0 20000 200 0 0 0 0 0 0", "  eth0: 20000 100 0 0 0 0 0 0 25000 200 0 0 0 0 0 0")
	c.readFile = func(path string) ([]byte, error) { return []byte(later[path]), nil }

	metrics, err := c.Collect(start.Add(10 * time.Second))
	if err != nil {
		t.Fatal(err)
	}
	values := byName(metrics)
	want := map[string]float64{
		"host.cpu.percent":                             50,
		"host.disk.read.bytes_per_second{sda}":         2000 * 512 / 10,
		"host.disk.write.bytes_per_second{sda}":        1000 * 512 / 10,
		"host.network.receive.bytes_per_second{eth0}":  1000,
		"host.network.transmit.bytes_per_second{eth0}": 500,
	}
	for name, value := range want {
		if values[name] != value {
			t.Errorf("%s = %v, want %v", name, values[name], value)
		}
	}
	if _, ok := values["host.network.receive.bytes_per_second{eth1}"]; ok {
		t.Error("a new interface got a rate on its first sample")
	}
	if _, ok := values["host.disk.read.bytes_per_second{sda1}"]; ok {
		t.Error("a partition was reported")
	}
}

func TestCounterWrapSkipsTheDevice(t *testing.T) {
	c := collector(fixtures)
	start := time.Now()
	c.Collect(start)
	wrapped := map[string]string{}
	for k, v := range fixtures {
		wrapped[k] = v
	}
	wrapped["/proc/diskstats"] = "   8       0 sda 100 0 10 50 200 0 4000 80 0 100 130\n"
	c.readFile = func(path string) ([]byte, error) { return []byte(wrapped[path]), nil }
	metrics, _ := c.Collect(start.Add(10 * time.Second))
	if _, ok := byName(metrics)["host.disk.read.bytes_per_second{sda}"]; ok {
		t.Fatal("a counter that went backwards produced a rate")
	}
}

func TestUnreadableFilesAreReportedNotFatal(t *testing.T) {
	partial := map[string]string{"/proc/loadavg": fixtures["/proc/loadavg"]}
	c := collector(partial)
	metrics, err := c.Collect(time.Now())
	if err == nil {
		t.Fatal("no error for missing files")
	}
	if values := byName(metrics); values["host.load.1m"] != 0.52 {
		t.Fatalf("loadavg lost: %v", values)
	}
}

func replaceLine(text, old, replacement string) string {
	out := ""
	for i, line := range splitLines(text) {
		if line == old {
			line = replacement
		}
		if i > 0 {
			out += "\n"
		}
		out += line
	}
	return out
}

func splitLines(text string) []string {
	var lines []string
	start := 0
	for i := 0; i < len(text); i++ {
		if text[i] == '\n' {
			lines = append(lines, text[start:i])
			start = i + 1
		}
	}
	return append(lines, text[start:])
}
