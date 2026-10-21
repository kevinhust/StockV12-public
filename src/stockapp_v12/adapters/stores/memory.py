"""In-memory store adapters — zero Redis dependency for unit tests."""

from __future__ import annotations

from typing import Dict, List, Optional, Set


class InMemoryPositionStore:
    def __init__(self) -> None:
        self.strings: Dict[str, str] = {}
        self.sets: Dict[str, Set[str]] = {}

    def get_record(self, key: str) -> Optional[str]:
        return self.strings.get(key)

    def set_record(
        self, key: str, value: str, *, ttl_seconds: Optional[int] = None
    ) -> None:
        self.strings[key] = value  # TTL ignored in memory

    def index_members(self, index_key: str) -> Set[str]:
        return set(self.sets.get(index_key, set()))

    def index_contains(self, index_key: str, member: str) -> bool:
        return member.upper() in self.sets.get(index_key, set())

    def register_atomic(
        self, pos_key: str, payload: str, index_key: str, symbol: str
    ) -> None:
        self.strings[pos_key] = payload
        self.sets.setdefault(index_key, set()).add(symbol.upper())

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
        self.sets.setdefault(index_key, set()).discard(symbol.upper())
        self.strings[cooldown_key] = "1"
        self.strings[pos_key] = payload


class InMemoryJournalStore:
    def __init__(self) -> None:
        self.lists: Dict[str, List[str]] = {}
        self.strings: Dict[str, str] = {}

    def append(self, key: str, payload: str, *, ttl_seconds: int) -> None:
        self.lists.setdefault(key, []).append(payload)

    def lrange_all(self, key: str) -> List[str]:
        return list(self.lists.get(key, []))

    def get_string(self, key: str) -> Optional[str]:
        return self.strings.get(key)


class InMemoryEntryEvalStore:
    def __init__(self) -> None:
        self.sets: Dict[str, Set[str]] = {}

    def is_member(self, key: str, member: str) -> bool:
        return member.upper() in self.sets.get(key, set())

    def try_mark(self, key: str, member: str, ttl_seconds: int) -> bool:
        s = self.sets.setdefault(key, set())
        m = member.upper()
        if m in s:
            return False
        s.add(m)
        return True

    def mark(self, key: str, member: str, ttl_seconds: int) -> None:
        self.sets.setdefault(key, set()).add(member.upper())


class InMemoryWatchlistStore:
    def __init__(self) -> None:
        self.zsets: Dict[str, Dict[str, float]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}

    def replace_tier1(
        self,
        zset_key: str,
        details_key: str,
        scores: Dict[str, float],
        details: Dict[str, str],
        *,
        ttl_seconds: int,
    ) -> None:
        self.zsets[zset_key] = dict(scores)
        self.hashes[details_key] = dict(details)

    def list_members(self, zset_key: str) -> List[str]:
        return list(self.zsets.get(zset_key, {}).keys())
