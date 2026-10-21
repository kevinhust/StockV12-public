"""PositionStore — persistence seam for position lifecycle keys.

Canonical Redis key shapes (byte-compatible with POSITION_LIFECYCLE.md):
  - position:{mode}:{SYMBOL}   STRING JSON
  - position:{mode}:index      SET of open symbols
  - risk:cooldown:{SYMBOL}     STRING "1" with TTL
"""

from __future__ import annotations

from typing import Optional, Protocol, Set


class PositionStore(Protocol):
    """Storage port for PositionManager — no Redis types in the signature."""

    def get_record(self, key: str) -> Optional[str]:
        """Return raw JSON payload or None if missing."""
        ...

    def set_record(
        self, key: str, value: str, *, ttl_seconds: Optional[int] = None
    ) -> None:
        """Overwrite record; optional TTL (seconds)."""
        ...

    def index_members(self, index_key: str) -> Set[str]:
        """Members of the open-position index (uppercased symbols)."""
        ...

    def index_contains(self, index_key: str, member: str) -> bool:
        """True if symbol is in the open-position index."""
        ...

    def register_atomic(
        self, pos_key: str, payload: str, index_key: str, symbol: str
    ) -> None:
        """Atomic SET record + SADD index (register on fill)."""
        ...

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
        """Atomic SREM index + SETEX cooldown + SET/EXPIRE record."""
        ...


__all__ = ["PositionStore"]
