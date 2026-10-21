package hostmetrics

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
)

type cpuTimes struct {
	busy, total uint64
}

// parseStat reads the aggregate "cpu" line: user nice system idle iowait irq softirq steal.
func parseStat(raw []byte) (cpuTimes, error) {
	for _, line := range strings.Split(string(raw), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 8 || fields[0] != "cpu" {
			continue
		}
		var values [8]uint64
		for i := range values {
			n, err := strconv.ParseUint(fields[i+1], 10, 64)
			if err != nil {
				return cpuTimes{}, fmt.Errorf("/proc/stat: %w", err)
			}
			values[i] = n
		}
		var total uint64
		for _, v := range values {
			total += v
		}
		idle := values[3] + values[4]
		return cpuTimes{busy: total - idle, total: total}, nil
	}
	return cpuTimes{}, errors.New("/proc/stat: no cpu line")
}

func cpuPercent(previous, current cpuTimes) (float64, bool) {
	if current.total <= previous.total || current.busy < previous.busy {
		return 0, false
	}
	return 100 * float64(current.busy-previous.busy) / float64(current.total-previous.total), true
}

func parseLoadavg(raw []byte) ([3]float64, error) {
	var load [3]float64
	fields := strings.Fields(string(raw))
	if len(fields) < 3 {
		return load, errors.New("/proc/loadavg: too short")
	}
	for i := range load {
		n, err := strconv.ParseFloat(fields[i], 64)
		if err != nil {
			return load, fmt.Errorf("/proc/loadavg: %w", err)
		}
		load[i] = n
	}
	return load, nil
}

type memory struct {
	total, available, swapTotal, swapFree uint64
}

// parseMeminfo reads the kB values of MemTotal, MemAvailable, SwapTotal and SwapFree.
func parseMeminfo(raw []byte) (memory, error) {
	wanted := map[string]*uint64{}
	var m memory
	wanted["MemTotal"], wanted["MemAvailable"], wanted["SwapTotal"], wanted["SwapFree"] = &m.total, &m.available, &m.swapTotal, &m.swapFree
	found := 0
	for _, line := range strings.Split(string(raw), "\n") {
		name, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		target := wanted[name]
		if target == nil {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			continue
		}
		n, err := strconv.ParseUint(fields[0], 10, 64)
		if err != nil {
			return m, fmt.Errorf("/proc/meminfo %s: %w", name, err)
		}
		*target = n * 1024
		found++
	}
	if found < 4 {
		return m, errors.New("/proc/meminfo: a field is missing")
	}
	return m, nil
}

func parseUptime(raw []byte) (float64, error) {
	fields := strings.Fields(string(raw))
	if len(fields) == 0 {
		return 0, errors.New("/proc/uptime: empty")
	}
	return strconv.ParseFloat(fields[0], 64)
}

// parseDiskstats returns bytes read and written per whole disk (sectors are 512 bytes here).
func parseDiskstats(raw []byte, isDisk func(string) bool) map[string]counters {
	disks := map[string]counters{}
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 10 || !isDisk(fields[2]) {
			continue
		}
		read, err1 := strconv.ParseUint(fields[5], 10, 64)
		written, err2 := strconv.ParseUint(fields[9], 10, 64)
		if err1 != nil || err2 != nil {
			continue
		}
		disks[fields[2]] = counters{read * 512, written * 512}
	}
	return disks
}

// parseNetDev returns bytes received and transmitted per interface, loopback left out.
func parseNetDev(raw []byte) map[string]counters {
	nets := map[string]counters{}
	for _, line := range strings.Split(string(raw), "\n") {
		name, rest, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		name = strings.TrimSpace(name)
		fields := strings.Fields(rest)
		if name == "lo" || len(fields) < 9 {
			continue
		}
		received, err1 := strconv.ParseUint(fields[0], 10, 64)
		transmitted, err2 := strconv.ParseUint(fields[8], 10, 64)
		if err1 != nil || err2 != nil {
			continue
		}
		nets[name] = counters{received, transmitted}
	}
	return nets
}

// isWholeDisk keeps sda and nvme0n1 and drops their partitions, loop and ram devices: a whole
// disk has an entry under /sys/block, a partition does not.
func isWholeDisk(name string) bool {
	for _, prefix := range []string{"loop", "ram", "zram", "sr", "fd", "dm-"} {
		if strings.HasPrefix(name, prefix) {
			return false
		}
	}
	_, err := os.Stat("/sys/block/" + name)
	return err == nil
}

func statfs(mount string) (total, used uint64, err error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(mount, &stat); err != nil {
		return 0, 0, fmt.Errorf("statfs %s: %w", mount, err)
	}
	size := uint64(stat.Bsize) //nolint:unconvert // Bsize is int64 on linux, uint32 elsewhere
	return stat.Blocks * size, (stat.Blocks - stat.Bfree) * size, nil
}
