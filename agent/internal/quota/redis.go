package quota

import (
	"context"
	"crypto/tls"
	"fmt"
	"net/url"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/frappe/suite_cloud/agent/internal/config"
)

const (
	accountsKey = "sca:quota:accounts"
	dayTTL      = 2 * 24 * time.Hour
	monthTTL    = 32 * 24 * time.Hour
	// Short enough that Stalwart's 2-second hook timeout sees the error and accepts.
	redisTimeout = 500 * time.Millisecond
)

// Every key of an account shares the hash tag {account}, so the scripts work on Redis Cluster.
var addScript = redis.NewScript(`
local n = tonumber(ARGV[1])
local lim = redis.call('HMGET', KEYS[3], 'daily', 'monthly')
local dl, ml = tonumber(lim[1]) or 0, tonumber(lim[2]) or 0
local d = tonumber(redis.call('GET', KEYS[1]) or '0')
local m = tonumber(redis.call('GET', KEYS[2]) or '0')
if dl > 0 and d + n > dl then return {1, dl, ml} end
if ml > 0 and m + n > ml then return {2, dl, ml} end
redis.call('INCRBY', KEYS[1], n)
redis.call('EXPIRE', KEYS[1], ARGV[2])
redis.call('INCRBY', KEYS[2], n)
redis.call('EXPIRE', KEYS[2], ARGV[3])
return {0, dl, ml}
`)

var setLimitScript = redis.NewScript(`
local stored = redis.call('HGET', KEYS[1], 'version')
if stored and stored >= ARGV[3] then return 0 end
redis.call('HSET', KEYS[1], 'daily', ARGV[1], 'monthly', ARGV[2], 'version', ARGV[3])
return 1
`)

// RedisStore is the store of a multi-node cluster: the cluster's own in-memory store.
type RedisStore struct {
	client redis.UniversalClient
}

func OpenRedis(cfg config.QuotaStore) (*RedisStore, error) {
	client, err := redisClient(cfg)
	if err != nil {
		return nil, err
	}
	return &RedisStore{client: client}, nil
}

func redisClient(cfg config.QuotaStore) (redis.UniversalClient, error) {
	if cfg.Type == "redis" {
		options, err := redis.ParseURL(cfg.URLs[0])
		if err != nil {
			return nil, fmt.Errorf("quota.store.urls: %w", err)
		}
		options.DialTimeout, options.ReadTimeout, options.WriteTimeout = redisTimeout, redisTimeout, redisTimeout
		return redis.NewClient(options), nil
	}
	addresses, tlsConfig, err := redisAddresses(cfg.URLs)
	if err != nil {
		return nil, err
	}
	options := &redis.UniversalOptions{
		Addrs:            addresses,
		Username:         cfg.Username,
		Password:         cfg.Password,
		MasterName:       cfg.ServiceName,
		SentinelUsername: cfg.SentinelUsername,
		SentinelPassword: cfg.SentinelPassword,
		TLSConfig:        tlsConfig,
		DialTimeout:      redisTimeout,
		ReadTimeout:      redisTimeout,
		WriteTimeout:     redisTimeout,
	}
	if cfg.Type == "redis_cluster" {
		return redis.NewClusterClient(options.Cluster()), nil
	}
	return redis.NewFailoverClient(options.Failover()), nil
}

// redisAddresses turns Stalwart's node URLs into host:port; rediss:// on any of them means TLS.
func redisAddresses(urls []string) ([]string, *tls.Config, error) {
	var addresses []string
	var tlsConfig *tls.Config
	for _, raw := range urls {
		parsed, err := url.Parse(raw)
		if err != nil {
			return nil, nil, fmt.Errorf("quota.store.urls: %w", err)
		}
		addresses = append(addresses, parsed.Host)
		if parsed.Scheme == "rediss" {
			tlsConfig = &tls.Config{MinVersion: tls.VersionTLS12}
		}
	}
	return addresses, tlsConfig, nil
}

func limitKey(account string) string { return "sca:quota:limit:{" + account + "}" }

func counterKey(account, period string) string { return "sca:quota:{" + account + "}:" + period }

func (r *RedisStore) Add(ctx context.Context, account string, n int64, periods Periods) (Verdict, Limit, error) {
	keys := []string{counterKey(account, periods.Day), counterKey(account, periods.Month), limitKey(account)}
	result, err := addScript.Run(ctx, r.client, keys, n, int(dayTTL.Seconds()), int(monthTTL.Seconds())).Int64Slice()
	if err != nil {
		return Accepted, Limit{}, err
	}
	if len(result) != 3 {
		return Accepted, Limit{}, fmt.Errorf("quota script answered %v", result)
	}
	return Verdict(result[0]), Limit{Daily: result[1], Monthly: result[2]}, nil
}

func (r *RedisStore) SetLimits(ctx context.Context, limits map[string]Limit) error {
	if len(limits) == 0 {
		return nil
	}
	pipe := r.client.Pipeline()
	members := make([]any, 0, len(limits))
	for account, limit := range limits {
		setLimitScript.Eval(ctx, pipe, []string{limitKey(account)}, limit.Daily, limit.Monthly, limit.Version)
		members = append(members, account)
	}
	pipe.SAdd(ctx, accountsKey, members...)
	_, err := pipe.Exec(ctx)
	return err
}

func (r *RedisStore) ClearLimits(ctx context.Context, accounts []string) error {
	if len(accounts) == 0 {
		return nil
	}
	pipe := r.client.Pipeline()
	for _, account := range accounts {
		pipe.HSet(ctx, limitKey(account), "daily", 0, "monthly", 0)
	}
	_, err := pipe.Exec(ctx)
	return err
}

func (r *RedisStore) Accounts(ctx context.Context) ([]string, error) {
	return r.client.SMembers(ctx, accountsKey).Result()
}

func (r *RedisStore) Counts(ctx context.Context, accounts []string, periods Periods) (map[string]Count, error) {
	pipe := r.client.Pipeline()
	days := make([]*redis.StringCmd, len(accounts))
	months := make([]*redis.StringCmd, len(accounts))
	for i, account := range accounts {
		days[i] = pipe.Get(ctx, counterKey(account, periods.Day))
		months[i] = pipe.Get(ctx, counterKey(account, periods.Month))
	}
	if _, err := pipe.Exec(ctx); err != nil && err != redis.Nil {
		return nil, err
	}
	counts := make(map[string]Count, len(accounts))
	for i, account := range accounts {
		day, _ := days[i].Int64() // a missing counter is 0
		month, _ := months[i].Int64()
		counts[account] = Count{Day: day, Month: month}
	}
	return counts, nil
}

// Run closes the client when the context ends; Redis keeps the data.
func (r *RedisStore) Run(ctx context.Context) error {
	<-ctx.Done()
	return r.client.Close()
}
