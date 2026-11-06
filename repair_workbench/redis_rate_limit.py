"""Optional, fail-closed Redis pacing shared by independent task processes."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import threading

from .session import BudgetExceeded


PACE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
local interval = tonumber(ARGV[1])
local previous = redis.call('HGET', KEYS[1], 'interval')
if previous and tonumber(previous) ~= interval then return {-1, 0} end
local next_time = tonumber(redis.call('HGET', KEYS[1], 'next') or '0')
if now < next_time then return {0, math.ceil((next_time - now) / 1000)} end
redis.call('HSET', KEYS[1], 'interval', interval, 'next', now + interval)
redis.call('PEXPIRE', KEYS[1], math.max(120000, math.ceil(interval / 1000) * 2))
return {1, now}
"""


class RedisRateLimiter:
    def __init__(self, client, namespace, qps):
        if (not isinstance(namespace, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', namespace)
                or type(qps) not in {int, float} or not math.isfinite(qps) or not .01 <= qps <= 1000):
            raise ValueError('invalid shared rate policy')
        self.client, self.namespace, self.qps = client, namespace, qps
        self.interval = math.ceil(1000000 / qps)
        self.key = 'repair-tools:{' + namespace + '}:rate'

    @classmethod
    def from_config(cls, path):
        path = Path(path).absolute()
        if (any(p.is_symlink() for p in (path, *path.parents))
                or not path.is_file() or path.stat().st_size > 8192):
            raise ValueError('invalid shared rate configuration')
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or set(data) != {'url', 'namespace', 'qps'}
                or not isinstance(data['url'], str) or not data['url'].startswith(('redis://', 'rediss://'))):
            raise ValueError('invalid shared rate configuration')
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        client = redis.Redis.from_url(data['url'], socket_timeout=1, socket_connect_timeout=1,
                                      retry=Retry(NoBackoff(), 0))
        limiter = cls(client, data['namespace'], data['qps'])
        try:
            client.ping()
        except redis.RedisError:
            raise BudgetExceeded('shared_rate_backend_unavailable') from None
        return limiter

    def describe(self):
        return {'backend': 'redis', 'namespace': self.namespace, 'qps': self.qps,
                'scope': 'all_processes_using_same_redis_namespace',
                'idle_ttl_seconds': max(120, math.ceil(self.interval / 1000) * 2 / 1000),
                'distributed_concurrency': False}

    def acquire(self, check=None):
        import redis
        while True:
            if check:
                check()
            try:
                granted, value = self.client.eval(PACE, 1, self.key, self.interval)
            except redis.RedisError:
                # Never silently fall back to a per-process limiter.
                raise BudgetExceeded('shared_rate_backend_unavailable') from None
            if granted == -1:
                raise BudgetExceeded('shared_rate_policy_conflict')
            if granted == 1:
                return value  # Redis server timestamp, useful for independent admission audits.
            threading.Event().wait(min(.05, max(.001, value / 1000)))
