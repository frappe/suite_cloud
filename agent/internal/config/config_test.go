package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const example = `{
  "suite_cloud": {"url": "https://cloud.example.com/", "secret": "s3cret"},
  "node": {"source": "stalwart", "host_ip": "203.0.113.5", "hostname": "n1.c1.example.com"},
  "logs": [{"name": "stalwart", "path": "/var/log/stalwart/stalwart.*", "format": "stalwart"}],
  "metrics": {"stalwart": {"url": "https://127.0.0.1:443/metrics/prometheus", "username": "telemetry", "password": "p"}}
}`

func load(t *testing.T, text string) (*Config, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
	return Load(path)
}

func TestLoadAppliesDefaults(t *testing.T) {
	cfg, err := load(t, example)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.SuiteCloud.RequestsPerMinute != 300 || cfg.SuiteCloud.RecordsPerRequest != 5000 || cfg.SuiteCloud.MaxAttempts != 8 {
		t.Fatalf("limits %+v", cfg.SuiteCloud)
	}
	if cfg.StateDir != "/var/lib/suite-cloud-agent" || cfg.LogLevel != "info" {
		t.Fatalf("paths %+v", cfg)
	}
	log := cfg.Logs[0]
	if log.Source != "stalwart" || log.PollIntervalSeconds != 1 || log.FlushIntervalSeconds != 5 || log.ShipExisting || log.DeleteAfterDays != 0 {
		t.Fatalf("log %+v", log)
	}
	if !cfg.Metrics.IsEnabled() || cfg.Metrics.IntervalSeconds != 60 || cfg.Metrics.Mounts[0] != "/" {
		t.Fatalf("metrics %+v", cfg.Metrics)
	}
	if cfg.Metrics.Stalwart.TLSServerName != "n1.c1.example.com" || !cfg.Metrics.Stalwart.ShouldVerifyTLS() {
		t.Fatalf("stalwart %+v", cfg.Metrics.Stalwart)
	}
	if strings.Contains(cfg.Summary(), "s3cret") || !strings.Contains(cfg.Summary(), "secret=set") {
		t.Fatalf("summary leaks: %s", cfg.Summary())
	}
}

func TestLoadRejectsBadValues(t *testing.T) {
	cases := map[string]string{
		"unknown key":     strings.Replace(example, `"node":`, `"nod": 1, "node":`, 1),
		"missing secret":  strings.Replace(example, `"secret": "s3cret"`, `"secret": " "`, 1),
		"bad url":         strings.Replace(example, "https://cloud.example.com/", "cloud.example.com", 1),
		"bad source":      strings.Replace(example, `"source": "stalwart"`, `"source": "Stalwart Node"`, 1),
		"bad ip":          strings.Replace(example, "203.0.113.5", "node-1", 1),
		"unknown format":  strings.Replace(example, `"format": "stalwart"`, `"format": "syslog"`, 1),
		"relative path":   strings.Replace(example, "/var/log/stalwart/stalwart.*", "stalwart.*", 1),
		"no password":     strings.Replace(example, `"password": "p"`, `"password": ""`, 1),
		"negative days":   strings.Replace(example, `"format": "stalwart"`, `"format": "stalwart", "delete_after_days": -1`, 1),
		"nothing to do":   strings.Replace(strings.Replace(example, `"logs": [{"name": "stalwart", "path": "/var/log/stalwart/stalwart.*", "format": "stalwart"}],`, `"logs": [],`, 1), `"metrics": {`, `"metrics": {"enabled": false, `, 1),
		"duplicate names": strings.Replace(example, `"logs": [`, `"logs": [{"name": "stalwart", "path": "/x/y.*", "format": "stalwart"}, `, 1),
		"bad interval":    strings.Replace(example, `"metrics": {`, `"metrics": {"interval_seconds": 1, `, 1),
	}
	for name, text := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := load(t, text); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestUnknownKeyNamesIt(t *testing.T) {
	_, err := load(t, strings.Replace(example, `"node"`, `"host"`, 1))
	if err == nil || !strings.Contains(err.Error(), "host") {
		t.Fatalf("error %v", err)
	}
}

func TestValidationNamesEveryKey(t *testing.T) {
	cfg := &Config{}
	cfg.ApplyDefaults()
	err := cfg.Validate()
	for _, key := range []string{"suite_cloud.url", "suite_cloud.secret", "node.source", "node.host_ip"} {
		if !strings.Contains(err.Error(), key) {
			t.Fatalf("%q not named in %v", key, err)
		}
	}
}

const quota = `"quota": {"hook_port": 8091, "hook_token": "t0ken", "store": {"type": "memory"}},
  "logs":`

func TestLoadQuotaAppliesDefaults(t *testing.T) {
	cfg, err := load(t, strings.Replace(example, `"logs":`, quota, 1))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Quota.LimitsPollSeconds != 60 || cfg.Quota.UsagePushSeconds != 60 {
		t.Fatalf("quota %+v", cfg.Quota)
	}
	if strings.Contains(cfg.Summary(), "t0ken") || !strings.Contains(cfg.Summary(), "quota_token=set") {
		t.Fatalf("summary leaks: %s", cfg.Summary())
	}
}

func TestLoadRejectsBadQuota(t *testing.T) {
	cases := map[string]string{
		"low port":       strings.Replace(quota, "8091", "80", 1),
		"no token":       strings.Replace(quota, "t0ken", " ", 1),
		"unknown store":  strings.Replace(quota, `"memory"`, `"memcached"`, 1),
		"redis no url":   strings.Replace(quota, `"memory"`, `"redis"`, 1),
		"redis two urls": strings.Replace(quota, `"type": "memory"`, `"type": "redis", "urls": ["redis://a:6379", "redis://b:6379"]`, 1),
		"redis bad url":  strings.Replace(quota, `"type": "memory"`, `"type": "redis_cluster", "urls": ["http://a:6379"]`, 1),
	}
	for name, section := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := load(t, strings.Replace(example, `"logs":`, section, 1)); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}
