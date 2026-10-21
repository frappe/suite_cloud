// Package hostmetrics samples the machine from /proc and reports the host.* gauges the Suite Cloud
// dashboards read. Rates are computed here, so a dashboard needs no counter arithmetic.
package hostmetrics

import (
	"errors"
	"os"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/telemetry"
)

// counters is a pair of byte counters: read and written, or received and transmitted.
type counters struct {
	first, second uint64
}

type sample struct {
	at    time.Time
	cpu   cpuTimes
	disks map[string]counters
	nets  map[string]counters
}

// Collector keeps the previous sample so the next one can be turned into rates.
type Collector struct {
	mounts   []string
	previous *sample
	readFile func(string) ([]byte, error)
	isDisk   func(string) bool
	statfs   func(string) (total, used uint64, err error)
}

func New(mounts []string) *Collector {
	return &Collector{mounts: mounts, readFile: os.ReadFile, isDisk: isWholeDisk, statfs: statfs}
}

// Collect samples the machine. It returns what it could read along with the errors of what it
// could not; the first call reports no rates.
func (c *Collector) Collect(now time.Time) ([]telemetry.Metric, error) {
	var metrics []telemetry.Metric
	var problems []error
	current := &sample{at: now, disks: map[string]counters{}, nets: map[string]counters{}}
	stamp := now.UTC().Format(time.RFC3339)
	gauge := func(name string, value float64, labels map[string]string) {
		metrics = append(metrics, telemetry.Metric{Timestamp: stamp, Name: name, Type: "gauge", Value: value, Labels: labels})
	}

	if raw, err := c.readFile("/proc/stat"); err != nil {
		problems = append(problems, err)
	} else if cpu, err := parseStat(raw); err != nil {
		problems = append(problems, err)
	} else {
		current.cpu = cpu
		if c.previous != nil {
			if percent, ok := cpuPercent(c.previous.cpu, cpu); ok {
				gauge("host.cpu.percent", percent, nil)
			}
		}
	}

	if raw, err := c.readFile("/proc/loadavg"); err != nil {
		problems = append(problems, err)
	} else if load, err := parseLoadavg(raw); err != nil {
		problems = append(problems, err)
	} else {
		gauge("host.load.1m", load[0], nil)
		gauge("host.load.5m", load[1], nil)
		gauge("host.load.15m", load[2], nil)
	}

	if raw, err := c.readFile("/proc/meminfo"); err != nil {
		problems = append(problems, err)
	} else if memory, err := parseMeminfo(raw); err != nil {
		problems = append(problems, err)
	} else {
		gauge("host.memory.total.bytes", float64(memory.total), nil)
		gauge("host.memory.used.bytes", float64(memory.total-memory.available), nil)
		gauge("host.swap.used.bytes", float64(memory.swapTotal-memory.swapFree), nil)
	}

	for _, mount := range c.mounts {
		total, used, err := c.statfs(mount)
		if err != nil {
			problems = append(problems, err)
			continue
		}
		gauge("host.disk.total.bytes", float64(total), map[string]string{"mount": mount})
		gauge("host.disk.used.bytes", float64(used), map[string]string{"mount": mount})
	}

	if raw, err := c.readFile("/proc/diskstats"); err != nil {
		problems = append(problems, err)
	} else {
		current.disks = parseDiskstats(raw, c.isDisk)
		if c.previous != nil {
			for device, rate := range rates(c.previous.disks, current.disks, now.Sub(c.previous.at)) {
				gauge("host.disk.read.bytes_per_second", rate.first, map[string]string{"device": device})
				gauge("host.disk.write.bytes_per_second", rate.second, map[string]string{"device": device})
			}
		}
	}

	if raw, err := c.readFile("/proc/net/dev"); err != nil {
		problems = append(problems, err)
	} else {
		current.nets = parseNetDev(raw)
		if c.previous != nil {
			for name, rate := range rates(c.previous.nets, current.nets, now.Sub(c.previous.at)) {
				gauge("host.network.receive.bytes_per_second", rate.first, map[string]string{"interface": name})
				gauge("host.network.transmit.bytes_per_second", rate.second, map[string]string{"interface": name})
			}
		}
	}

	if raw, err := c.readFile("/proc/uptime"); err != nil {
		problems = append(problems, err)
	} else if uptime, err := parseUptime(raw); err != nil {
		problems = append(problems, err)
	} else {
		gauge("host.uptime.seconds", uptime, nil)
	}

	c.previous = current
	return metrics, errors.Join(problems...)
}

// rate holds two per-second rates in the same order as counters.
type rate struct {
	first, second float64
}

// rates turns two readings of counters into per-second rates, in name order. A device that
// appeared since the last sample or whose counter went backwards is left out this time.
func rates(previous, current map[string]counters, elapsed time.Duration) map[string]rate {
	out := map[string]rate{}
	seconds := elapsed.Seconds()
	if seconds <= 0 {
		return out
	}
	for name, now := range current {
		before, ok := previous[name]
		if !ok || now.first < before.first || now.second < before.second {
			continue
		}
		out[name] = rate{float64(now.first-before.first) / seconds, float64(now.second-before.second) / seconds}
	}
	return out
}
