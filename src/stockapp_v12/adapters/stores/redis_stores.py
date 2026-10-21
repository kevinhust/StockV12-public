"""Redis implementations of store ports — key shapes unchanged."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class RedisPositionStore:
    def __init__(self, redis_client: Any):
        self._r = redis_client

    def get_record(self, key: str) -> Optional[str]:
        raw = self._r.get(key)
        if raw is None:
            return None
        return _as_str(raw)

    def set_record(
        self, key: str, value: str, *, ttl_seconds: Optional[int] = None
    ) -> None:
        self._r.set(key, value)
        if ttl_seconds:
            self._r.expire(key, ttl_seconds)

    def index_members(self, index_key: str) -> Set[str]:
        raw = self._r.smembers(index_key) or set()
        return {_as_str(m).upper() for m in raw}

    def index_contains(self, index_key: str, member: str) -> bool:
        return bool(self._r.sismember(index_key, member.upper()))

    def register_atomic(
        self, pos_key: str, payload: str, index_key: str, symbol: str
    ) -> None:
        pipe = self._r.pipeline()
        pipe.set(pos_key, payload)
        pipe.sadd(index_key, symbol.upper())
        pipe.execute()

    def close_atomic(
        self,
        pos_key: str,
        payload: str,
        index_key: str,
        symbol: str,
        cooldown_key: str,
        cooldown_ttl_seconds: int,
        *,
        record_ttl_seconds: Optional[int] = None,
    ) -> None:
        pipe = self._r.pipeline()
        pipe.srem(index_key, symbol.upper())
        pipe.setex(cooldown_key, int(cooldown_ttl_seconds), "1")
        pipe.set(pos_key, payload)
        if record_ttl_seconds:
            pipe.expire(pos_key, int(record_ttl_seconds))
        pipe.execute()


class RedisJournalStore:
    def __init__(self, redis_client: Any):
        self._r = redis_client

    def append(self, key: str, payload: str, *, ttl_seconds: int) -> None:
        self._r.rpush(key, payload)
        self._r.expire(key, int(ttl_seconds))

    def lrange_all(self, key: str) -> List[str]:
        raw = self._r.lrange(key, 0, -1) or []
        return [_as_str(e) for e in raw]

    def get_string(self, key: str) -> Optional[str]:
        raw = self._r.get(key)
        if raw is None:
            return None
        return _as_str(raw)


_TRY_MARK_EVALUATED_LUA = """
local key = KEYS[1]
local member = ARGV[1]
local ttl = tonumber(ARGV[2])
local added = redis.call('SADD', key, member)
if added == 1 then
    redis.call('EXPIRE', key, ttl)
end
return added
"""


class RedisEntryEvalStore:
    def __init__(self, redis_client: Any):
        self._r = redis_client

    def is_member(self, key: str, member: str) -> bool:
        return bool(self._r.sismember(key, member.upper()))

    def try_mark(self, key: str, member: str, ttl_seconds: int) -> bool:
        added = self._r.eval(
            _TRY_MARK_EVALUATED_LUA,
            1,
            key,
            member.upper(),
            str(int(ttl_seconds)),
        )
        return int(added) == 1

    def mark(self, key: str, member: str, ttl_seconds: int) -> None:
        pipe = self._r.pipeline()
        pipe.sadd(key, member.upper())
        pipe.expire(key, int(ttl_seconds))
        pipe.execute()


class RedisWatchlistStore:
    def __init__(self, redis_client: Any):
        self._r = redis_client

    def replace_tier1(
        self,
        zset_key: str,
        details_key: str,
        scores: Dict[str, float],
        details: Dict[str, str],
        *,
        ttl_seconds: int,
    ) -> None:
        pipe = self._r.pipeline()
        pipe.delete(zset_key)
        pipe.delete(details_key)
        for sym, score in scores.items():
            pipe.zadd(zset_key, {sym: score})
        for sym, detail in details.items():
            pipe.hset(details_key, sym, detail)
        pipe.expire(zset_key, int(ttl_seconds))
        pipe.expire(details_key, int(ttl_seconds))
        pipe.execute()

    def list_members(self, zset_key: str) -> List[str]:
        members = self._r.zrange(zset_key, 0, -1) or []
        return [_as_str(m) for m in members]
