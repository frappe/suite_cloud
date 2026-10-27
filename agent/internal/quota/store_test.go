package quota

import (
	"context"
	"os"
	"slices"
	"sort"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

var october = PeriodsAt(time.Date(2026, 10, 26, 23, 59, 0, 0, time.UTC))

// stores gives every store under test a fresh, empty instance. Redis runs only when
// SCA_TEST_REDIS_URL names a server the test may write sca:quota:* keys to.
func stores(t *testing.T) map[string]func(t *testing.T) Store {
	t.Helper()
	all := map[string]func(t *testing.T) Store{
		"memory": func(t *testing.T) Store {
			store, err := OpenMemory(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			return store
		},
	}
	if url := os.Getenv("SCA_TEST_REDIS_URL"); url != "" {
		all["redis"] = func(t *testing.T) Store {
			store, err := OpenRedis(config.QuotaStore{Type: "redis", URLs: []string{url}})
			if err != nil {
				t.Fatal(err)
			}
			clearRedis(t, store)
			t.Cleanup(func() { clearRedis(t, store); store.client.Close() })
			return store
		}
	}
	return all
}

func clearRedis(t *testing.T, store *RedisStore) {
	ctx := context.Background()
	keys, err := store.client.Keys(ctx, "sca:quota:*").Result()
	if err != nil {
		t.Fatal(err)
	}
	if len(keys) > 0 {
		store.client.Del(ctx, keys...)
	}
}

func setLimit(t *testing.T, store Store, account string, daily, monthly int64, version string) {
	t.Helper()
	if err := store.SetLimits(context.Background(), map[string]Limit{account: {daily, monthly, version}}); err != nil {
		t.Fatal(err)
	}
}

func add(t *testing.T, store Store, account string, n int64, periods Periods) Verdict {
	t.Helper()
	verdict, _, err := store.Add(context.Background(), account, n, periods)
	if err != nil {
		t.Fatal(err)
	}
	return verdict
}

func TestStoreReachingTheLimitIsAcceptedAndOneOverIsNot(t *testing.T) {
	for name, open := range stores(t) {
		t.Run(name, func(t *testing.T) {
			store := open(t)
			setLimit(t, store, "a@example.com", 10, 0, "1")
			if v := add(t, store, "a@example.com", 10, october); v != Accepted {
				t.Fatalf("reaching the limit: %v", v)
			}
			verdict, limit, _ := store.Add(context.Background(), "a@example.com", 1, october)
			if verdict != OverDaily || limit.Daily != 10 {
				t.Fatalf("one over: %v %+v", verdict, limit)
			}
			counts, _ := store.Counts(context.Background(), []string{"a@example.com"}, october)
			if counts["a@example.com"] != (Count{Day: 10, Month: 10, RefusedDay: 1, RefusedMonth: 1}) {
				t.Fatalf("a refused message counted as sent, or not as refused: %+v", counts)
			}
		})
	}
}

func TestStoreMonthlyLimitAndNoLimit(t *testing.T) {
	for name, open := range stores(t) {
		t.Run(name, func(t *testing.T) {
			store := open(t)
			setLimit(t, store, "a@example.com", 0, 5, "1")
			if v := add(t, store, "a@example.com", 6, october); v != OverMonthly {
				t.Fatalf("monthly: %v", v)
			}
			if v := add(t, store, "free@example.com", 1000000, october); v != Accepted {
				t.Fatalf("no limit: %v", v)
			}
			counts, _ := store.Counts(context.Background(), []string{"free@example.com"}, october)
			if counts["free@example.com"].Month != 1000000 {
				t.Fatalf("an account without a limit is still counted: %+v", counts)
			}
		})
	}
}

func TestStoreCountersRollOverWithThePeriod(t *testing.T) {
	for name, open := range stores(t) {
		t.Run(name, func(t *testing.T) {
			store := open(t)
			setLimit(t, store, "a@example.com", 10, 15, "1")
			add(t, store, "a@example.com", 10, october)
			nextDay := PeriodsAt(time.Date(2026, 10, 27, 0, 0, 0, 0, time.UTC))
			if v := add(t, store, "a@example.com", 5, nextDay); v != Accepted {
				t.Fatalf("a new day: %v", v)
			}
			if v := add(t, store, "a@example.com", 1, nextDay); v != OverMonthly {
				t.Fatalf("the month still counts: %v", v)
			}
			november := PeriodsAt(time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC))
			if v := add(t, store, "a@example.com", 10, november); v != Accepted {
				t.Fatalf("a new month: %v", v)
			}
		})
	}
}

func TestStoreListsTheAccountsCountedOnGivenDays(t *testing.T) {
	for name, open := range stores(t) {
		t.Run(name, func(t *testing.T) {
			store := open(t)
			today := PeriodsAt(time.Date(2026, 10, 27, 0, 5, 0, 0, time.UTC))
			yesterday := PeriodsAt(time.Date(2026, 10, 26, 23, 0, 0, 0, time.UTC))
			add(t, store, "sent@example.com", 2, today)
			setLimit(t, store, "refused@example.com", 1, 0, "1")
			if v := add(t, store, "refused@example.com", 2, yesterday); v != OverDaily {
				t.Fatalf("not refused: %v", v)
			}
			add(t, store, "old@example.com", 1, PeriodsAt(time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)))

			accounts, err := store.CountedAccounts(context.Background(), today.Day, yesterday.Day)
			if err != nil {
				t.Fatal(err)
			}
			sort.Strings(accounts)
			if want := []string{"refused@example.com", "sent@example.com"}; !slices.Equal(accounts, want) {
				t.Fatalf("accounts %v, want %v", accounts, want)
			}
		})
	}
}

func TestRedisClusterAndSentinelTakeTheLoginFromTheirURLsWhenTheFieldsAreEmpty(t *testing.T) {
	urls := []string{"redis://quota:s3cret@10.0.0.21:6379", "redis://10.0.0.22:6379"}
	options := func(cfg config.QuotaStore) *redis.UniversalOptions {
		t.Helper()
		cfg.URLs = urls
		opts, err := redisOptions(cfg)
		if err != nil {
			t.Fatal(err)
		}
		return opts
	}
	logins := func(o *redis.UniversalOptions) [4]string {
		return [4]string{o.Username, o.Password, o.SentinelUsername, o.SentinelPassword}
	}
	cases := []struct {
		name string
		cfg  config.QuotaStore
		want [4]string
	}{
		// A cluster's URLs name its nodes; Sentinel's name the sentinels, not the master.
		{"cluster", config.QuotaStore{Type: "redis_cluster"}, [4]string{"quota", "s3cret", "", ""}},
		{"sentinel", config.QuotaStore{Type: "redis_sentinel", ServiceName: "mymaster"}, [4]string{"", "", "quota", "s3cret"}},
		{"cluster fields win", config.QuotaStore{Type: "redis_cluster", Username: "stalwart", Password: "field"}, [4]string{"stalwart", "field", "", ""}},
		{"sentinel fields win", config.QuotaStore{Type: "redis_sentinel", SentinelUsername: "watch", SentinelPassword: "field", Username: "stalwart", Password: "master"}, [4]string{"stalwart", "master", "watch", "field"}},
	}
	for _, c := range cases {
		if got := logins(options(c.cfg)); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
	if name := options(config.QuotaStore{Type: "redis_sentinel", ServiceName: "mymaster"}).MasterName; name != "mymaster" {
		t.Errorf("sentinel master %q", name)
	}

	// Each store builds its own kind of client: a failover client is a plain *redis.Client.
	for storeType, isCluster := range map[string]bool{"redis_cluster": true, "redis_sentinel": false} {
		client, err := redisClient(config.QuotaStore{Type: storeType, URLs: urls, ServiceName: "mymaster"})
		if err != nil {
			t.Fatal(err)
		}
		_, cluster := client.(*redis.ClusterClient)
		_, single := client.(*redis.Client)
		if cluster != isCluster || single == isCluster {
			t.Errorf("%s built %T", storeType, client)
		}
		client.Close()
	}
}

func TestStoreOlderLimitNeverReplacesANewerOne(t *testing.T) {
	for name, open := range stores(t) {
		t.Run(name, func(t *testing.T) {
			store := open(t)
			setLimit(t, store, "a@example.com", 10, 0, "2026-10-26 10:00:00.000002")
			setLimit(t, store, "a@example.com", 99, 0, "2026-10-26 10:00:00.000001")
			if v := add(t, store, "a@example.com", 11, october); v != OverDaily {
				t.Fatalf("the older limit won: %v", v)
			}
			if err := store.ClearLimits(context.Background(), []string{"a@example.com"}); err != nil {
				t.Fatal(err)
			}
			if v := add(t, store, "a@example.com", 11, october); v != Accepted {
				t.Fatalf("cleared limit still applies: %v", v)
			}
			if accounts, _ := store.Accounts(context.Background()); len(accounts) != 0 {
				t.Fatalf("a cleared account is still listed: %v", accounts)
			}
			// An account a pull missed and returns again takes its limit back, at the same version.
			setLimit(t, store, "a@example.com", 1, 0, "2026-10-26 10:00:00.000002")
			if v := add(t, store, "a@example.com", 1, october); v != OverDaily {
				t.Fatalf("the limit was not restored: %v", v)
			}
		})
	}
}

func TestStoreNeverPassesTheLimitUnderConcurrency(t *testing.T) {
	for name, open := range stores(t) {
		t.Run(name, func(t *testing.T) {
			store := open(t)
			setLimit(t, store, "a@example.com", 100, 0, "1")
			var accepted atomic.Int64
			var wg sync.WaitGroup
			for i := 0; i < 200; i++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					verdict, _, err := store.Add(context.Background(), "a@example.com", 1, october)
					if err == nil && verdict == Accepted {
						accepted.Add(1)
					}
				}()
			}
			wg.Wait()
			if accepted.Load() != 100 {
				t.Fatalf("%d of 200 accepted, want 100", accepted.Load())
			}
		})
	}
}

func TestMemoryStoreSurvivesARestart(t *testing.T) {
	dir := t.TempDir()
	store, _ := OpenMemory(dir)
	store.now = func() time.Time { return time.Date(2026, 10, 26, 23, 59, 0, 0, time.UTC) }
	setLimit(t, store, "a@example.com", 10, 0, "1")
	add(t, store, "a@example.com", 4, october)
	add(t, store, "a@example.com", 2, PeriodsAt(time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC)))
	add(t, store, "a@example.com", 1, PeriodsAt(time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)))
	if err := store.Save(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenMemory(dir)
	if err != nil {
		t.Fatal(err)
	}
	if v := add(t, reopened, "a@example.com", 7, october); v != OverDaily {
		t.Fatalf("limit or count lost: %v", v)
	}
	// Yesterday stays for the usage push; the day before goes.
	if usage := reopened.data.Usage["a@example.com"]; len(usage) != 3 || usage["d:20261025"] != 2 || usage["d:20261024"] != 0 {
		t.Fatalf("usage %v", usage)
	}
}

func TestMemoryStoreRefusesAnUnreadableFile(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(dir+"/quota.json", []byte("{"), 0o600)
	if _, err := OpenMemory(dir); err == nil {
		t.Fatal("opened")
	}
}
