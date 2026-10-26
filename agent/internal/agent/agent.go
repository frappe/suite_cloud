// Package agent wires the config, the state, the tailers, the metrics pipeline and the sending
// quota together.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/frappe/suite_cloud/agent/internal/config"
	"github.com/frappe/suite_cloud/agent/internal/format"
	"github.com/frappe/suite_cloud/agent/internal/hostmetrics"
	"github.com/frappe/suite_cloud/agent/internal/metrics"
	"github.com/frappe/suite_cloud/agent/internal/prometheus"
	"github.com/frappe/suite_cloud/agent/internal/quota"
	"github.com/frappe/suite_cloud/agent/internal/sender"
	"github.com/frappe/suite_cloud/agent/internal/state"
	"github.com/frappe/suite_cloud/agent/internal/tail"
)

// Run ships logs and metrics until the context ends. Every tailer and the metrics pipeline run on
// their own; the first failure stops the rest.
func Run(ctx context.Context, cfg *config.Config, version string, logger *slog.Logger) error {
	store, err := openStore(cfg.StateDir)
	if err != nil {
		return err
	}
	logger.Info("suite-cloud-agent starting", "version", version, "config", cfg.Summary())
	logCursors(store, logger)
	send := sender.New(cfg.SuiteCloud, version, logger)
	var quotaStore quota.Store
	if cfg.Quota != nil {
		if quotaStore, err = quota.Open(cfg.Quota.Store, cfg.StateDir); err != nil {
			return fmt.Errorf("quota store: %w", err)
		}
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	var once sync.Once
	var failure error
	start := func(name string, run func(context.Context) error) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := run(ctx); err != nil && !errors.Is(err, context.Canceled) {
				once.Do(func() { failure = fmt.Errorf("%s: %w", name, err) })
				cancel()
			}
		}()
	}

	for _, def := range cfg.Logs {
		parser, _ := format.Lookup(def.Format)
		tailer := &tail.Tailer{Def: def, Node: cfg.Node, BatchSize: cfg.SuiteCloud.RecordsPerRequest, Parser: parser, Store: store, Sender: send, Logger: logger.With("log", def.Name)}
		start("log "+def.Name, tailer.Run)
	}
	if cfg.Metrics.IsEnabled() {
		pipeline := &metrics.Pipeline{Host: hostmetrics.New(cfg.Metrics.Mounts), Interval: cfg.Metrics.Interval(), BatchSize: cfg.SuiteCloud.RecordsPerRequest, Node: cfg.Node, Sender: send, Logger: logger.With("component", "metrics")}
		if cfg.Metrics.Stalwart != nil {
			pipeline.Scraper = prometheus.NewScraper(*cfg.Metrics.Stalwart)
		}
		start("metrics", pipeline.Run)
	}
	if cfg.Quota != nil {
		startQuota(cfg, quotaStore, send, logger.With("component", "quota"), start)
	}

	wg.Wait()
	logger.Info("suite-cloud-agent stopped", "state_saved", true)
	return failure
}

// startQuota runs the hook, the limit pull, the usage push and the store, each on its own.
func startQuota(cfg *config.Config, store quota.Store, send *sender.Sender, logger *slog.Logger, start func(string, func(context.Context) error)) {
	touched := &quota.Touched{}
	hook := &quota.Hook{Port: cfg.Quota.HookPort, Token: cfg.Quota.HookToken, Store: store, Touched: touched, Logger: logger, Now: time.Now}
	limits := &quota.Limits{Store: store, Caller: send, HostIP: cfg.Node.HostIP, Interval: cfg.Quota.LimitsPollInterval(), Logger: logger, Now: time.Now}
	usage := &quota.Usage{Store: store, Caller: send, HostIP: cfg.Node.HostIP, Touched: touched, Interval: cfg.Quota.UsagePushInterval(), BatchSize: cfg.SuiteCloud.RecordsPerRequest, Logger: logger, Now: time.Now}
	start("quota hook", hook.Run)
	start("quota limits", limits.Run)
	start("quota usage", usage.Run)
	start("quota store", store.Run)
}

// Check reports what would stop the agent from running: the state directory, the log globs or a
// quota.json that cannot be read.
// It makes no request.
func Check(cfg *config.Config) error {
	var problems []error
	if _, err := openStore(cfg.StateDir); err != nil {
		problems = append(problems, err)
	} else if probe, err := os.CreateTemp(cfg.StateDir, "check.*"); err != nil {
		problems = append(problems, fmt.Errorf("state_dir %s is not writable: %w", cfg.StateDir, err))
	} else {
		probe.Close()
		os.Remove(probe.Name())
	}
	for _, def := range cfg.Logs {
		if _, err := os.Stat(filepath.Dir(def.Path)); err != nil {
			problems = append(problems, fmt.Errorf("logs[%s]: %w", def.Name, err))
		}
	}
	if cfg.Quota != nil && cfg.Quota.Store.Type == "memory" {
		if _, err := quota.OpenMemory(cfg.StateDir); err != nil {
			problems = append(problems, fmt.Errorf("quota store: %w", err))
		}
	}
	return errors.Join(problems...)
}

func openStore(dir string) (*state.Store, error) {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("state_dir: %w", err)
	}
	return state.Open(dir)
}

func logCursors(store *state.Store, logger *slog.Logger) {
	store.View(func(s *state.State) {
		for name, log := range s.Logs {
			for _, cursor := range log.Cursors {
				logger.Info("cursor", "log", name, "path", cursor.Path, "offset", cursor.Offset, "finished", cursor.Finished)
			}
			if log.Pending != nil {
				logger.Info("pending batch", "log", name, "batch_id", log.Pending.BatchID, "path", log.Pending.Path, "start", log.Pending.Start, "end", log.Pending.End)
			}
		}
	})
}
