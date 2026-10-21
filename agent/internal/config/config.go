// Package config reads and checks the agent's config.json.
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"time"
)

// DefaultPath is where provisioning writes the file; Stalwart's own config.json lives elsewhere.
const DefaultPath = "/etc/suite-cloud-agent/config.json"

type Config struct {
	SuiteCloud SuiteCloud `json:"suite_cloud"`
	Node       Node       `json:"node"`
	StateDir   string     `json:"state_dir"`
	LogLevel   string     `json:"log_level"`
	Logs       []LogDef   `json:"logs"`
	Metrics    Metrics    `json:"metrics"`
}

// SuiteCloud is where batches go and the limits the site enforces on them.
type SuiteCloud struct {
	URL                   string `json:"url"`
	Secret                string `json:"secret"`
	RequestsPerMinute     int    `json:"requests_per_minute"`
	RecordsPerRequest     int    `json:"records_per_request"`
	MaxAttempts           int    `json:"max_attempts"`
	RequestTimeoutSeconds int    `json:"request_timeout_seconds"`
}

// Node identifies this machine in every batch; host_ip must be the address Suite Cloud stores.
type Node struct {
	Source   string `json:"source"`
	HostIP   string `json:"host_ip"`
	Hostname string `json:"hostname"`
}

// LogDef is one set of log files to tail.
type LogDef struct {
	Name                 string `json:"name"`
	Path                 string `json:"path"`
	Format               string `json:"format"`
	Source               string `json:"source,omitempty"`
	ShipExisting         bool   `json:"ship_existing"`
	DeleteAfterDays      int    `json:"delete_after_days"`
	PollIntervalSeconds  int    `json:"poll_interval_seconds"`
	FlushIntervalSeconds int    `json:"flush_interval_seconds"`
}

type Metrics struct {
	Enabled         *bool            `json:"enabled"`
	IntervalSeconds int              `json:"interval_seconds"`
	Mounts          []string         `json:"mounts"`
	Stalwart        *StalwartMetrics `json:"stalwart,omitempty"`
}

// StalwartMetrics is Stalwart's Prometheus endpoint on this node, behind basic auth.
type StalwartMetrics struct {
	URL           string `json:"url"`
	Username      string `json:"username"`
	Password      string `json:"password"`
	TLSServerName string `json:"tls_server_name"`
	VerifyTLS     *bool  `json:"verify_tls"`
}

// Load reads, completes and validates a config file. Unknown keys are errors, so a typo cannot
// silently turn a setting off.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	cfg := &Config{}
	if err := decoder.Decode(cfg); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	cfg.ApplyDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return cfg, nil
}

func (c *Config) ApplyDefaults() {
	setInt(&c.SuiteCloud.RequestsPerMinute, 300)
	setInt(&c.SuiteCloud.RecordsPerRequest, 5000)
	setInt(&c.SuiteCloud.MaxAttempts, 8)
	setInt(&c.SuiteCloud.RequestTimeoutSeconds, 60)
	if c.Node.Hostname == "" {
		c.Node.Hostname, _ = os.Hostname()
	}
	if c.StateDir == "" {
		c.StateDir = "/var/lib/suite-cloud-agent"
	}
	if c.LogLevel == "" {
		c.LogLevel = "info"
	}
	for i := range c.Logs {
		log := &c.Logs[i]
		if log.Source == "" {
			log.Source = c.Node.Source
		}
		setInt(&log.PollIntervalSeconds, 1)
		setInt(&log.FlushIntervalSeconds, 5)
	}
	if c.Metrics.Enabled == nil {
		c.Metrics.Enabled = new(bool)
		*c.Metrics.Enabled = true
	}
	setInt(&c.Metrics.IntervalSeconds, 60)
	if len(c.Metrics.Mounts) == 0 {
		c.Metrics.Mounts = []string{"/"}
	}
	if s := c.Metrics.Stalwart; s != nil {
		if s.TLSServerName == "" {
			s.TLSServerName = c.Node.Hostname
		}
		if s.VerifyTLS == nil {
			s.VerifyTLS = new(bool)
			*s.VerifyTLS = true
		}
	}
}

func (c *Config) RequestTimeout() time.Duration {
	return time.Duration(c.SuiteCloud.RequestTimeoutSeconds) * time.Second
}

func (l LogDef) PollInterval() time.Duration {
	return time.Duration(l.PollIntervalSeconds) * time.Second
}
func (l LogDef) FlushInterval() time.Duration {
	return time.Duration(l.FlushIntervalSeconds) * time.Second
}

func (m Metrics) IsEnabled() bool         { return m.Enabled != nil && *m.Enabled }
func (m Metrics) Interval() time.Duration { return time.Duration(m.IntervalSeconds) * time.Second }

func (s StalwartMetrics) ShouldVerifyTLS() bool { return s.VerifyTLS == nil || *s.VerifyTLS }

func setInt(field *int, value int) {
	if *field == 0 {
		*field = value
	}
}
