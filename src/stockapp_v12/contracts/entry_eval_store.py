"""EntryEvalStore — INV-1/INV-2 evaluation idempotency seam.

Canonical Redis key shapes:
  - entry:evaluated:{mode}:{YYYYMMDD}  SET of symbols
  - position:{mode}:index              SET (INV-2 open check)
"""

from __future__ import annotations

from typing import Protocol


class EntryEvalStore(Protocol):
    def is_member(self, key: str, member: str) -> bool:
        """SISMEMBER — True if already present."""
        ...

    def try_mark(self, key: str, member: str, ttl_seconds: int) -> bool:
        """Atomically SADD+EXPIRE. Return True if newly added (first wins)."""
        ...

    def mark(self, key: str, member: str, ttl_seconds: int) -> None:
        """Non-atomic mark (legacy mark_evaluated path)."""
        ...


__all__ = ["EntryEvalStore"]
