"""Shared test fakes for Phase A1 tests.

A *stateful* FakeRedis that supports the small subset of Redis operations
used by KillSwitch and LiveOrderGuard.  This avoids MagicMock side_effect
chains that don't track state across calls.
"""

from __future__ import annotations

from typing import Any, Optional


class FakeRedis:
    """Minimal in-memory Redis for unit tests.

    Supports: get/set/setex/sismember/smembers/sadd/srem/exists/ttl/incr/expire
    and pipeline (whose execute() flushes queued ops).
    """

    def __init__(self):
        self.store: dict[str, Any] = {}
        self.sets: dict[str, set] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self._last_pipeline: Optional["_Pipeline"] = None
        self._raises: Optional[BaseException] = None  # set to simulate outage

    # ----- basic ops -----

    def get(self, key: str):
        if self._raises:
            raise self._raises
        return self.store.get(key)

    def set(self, key: str, value: Any, **kw):
        if self._raises:
            raise self._raises
        self.store[key] = value
        return True

    def setex(self, key: str, ttl: int, value: Any):
        if self._raises:
            raise self._raises
        self.store[key] = value  # ttl not enforced in this fake
        return True

    def exists(self, key: str):
        return 1 if key in self.store else 0

    def ttl(self, key: str):
        if key not in self.store:
            return -2
        return 3600  # pretend any set key has 1h remaining

    def incr(self, key: str):
        v = int(self.store.get(key, 0)) + 1
        self.store[key] = v
        return v

    def expire(self, key: str, seconds: int):
        return True

    # ----- sets -----

    def sismember(self, key: str, value: str):
        return value in self.sets.get(key, set())

    def smembers(self, key: str):
        return set(self.sets.get(key, set()))

    def sadd(self, key: str, *values):
        if key not in self.sets:
            self.sets[key] = set()
        self.sets[key].update(values)
        return len(values)

    def srem(self, key: str, *values):
        s = self.sets.get(key, set())
        n = 0
        for v in values:
            if v in s:
                s.discard(v)
                n += 1
        return n

    # ----- sorted sets -----

    def zadd(self, key: str, mapping: dict):
        if self._raises:
            raise self._raises
        if key not in self.zsets:
            self.zsets[key] = {}
        self.zsets[key].update({str(k): float(v) for k, v in mapping.items()})
        return len(mapping)

    def zrange(self, key: str, start: int, end: int, **kw):
        if self._raises:
            raise self._raises
        members = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        names = [m for m, _ in members]
        if end == -1:
            end = len(names) - 1
        return names[start : end + 1] if names else []

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    # ----- delete -----

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
            if k in self.hashes:
                del self.hashes[k]
        return n

    # ----- hashes -----

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        if self._raises:
            raise self._raises
        if key not in self.hashes:
            self.hashes[key] = {}
        cur = int(self.hashes[key].get(field, 0)) + int(amount)
        self.hashes[key][field] = cur
        return cur

    def hgetall(self, key: str):
        if self._raises:
            raise self._raises
        if key not in self.hashes:
            return {}
        # Return as {bytes_key: bytes_value} to mirror real Redis-py decode behavior
        return {
            k.encode(): str(v).encode()
            for k, v in self.hashes[key].items()
        }

    # ----- pipeline -----

    def pipeline(self):
        p = _Pipeline(self)
        self._last_pipeline = p
        return p

    # ----- failure injection -----

    def fail_with(self, exc: BaseException):
        self._raises = exc


class _Pipeline:
    def __init__(self, redis: FakeRedis):
        self._r = redis
        self._queue: list = []

    def set(self, key, value, *a, **kw):
        self._queue.append(("set", (key, value)))
        return self

    def setex(self, key, ttl, value):
        self._queue.append(("setex", (key, ttl, value)))
        return self

    def incr(self, key):
        self._queue.append(("incr", (key,)))
        return self

    def expire(self, key, seconds):
        self._queue.append(("expire", (key, seconds)))
        return self

    def delete(self, *keys):
        self._queue.append(("delete", keys))
        return self

    def hincrby(self, key, field, amount=1):
        self._queue.append(("hincrby", (key, field, amount)))
        return self

    def hgetall(self, key):
        self._queue.append(("hgetall", (key,)))
        return self

    def execute(self):
        results = []
        for op, args in self._queue:
            method = getattr(self._r, op)
            results.append(method(*args))
        self._queue.clear()
        return results
