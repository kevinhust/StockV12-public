"""JournalStore — append-only audit persistence seam.

Canonical Redis key shape:
  - journal:{YYYY-MM-DD}:{SYMBOL}  LIST of JSON strings (RPUSH)
  - legacy STRING form still readable via get_string
"""

from __future__ import annotations

from typing import List, Optional, Protocol


class JournalStore(Protocol):
    def append(self, key: str, payload: str, *, ttl_seconds: int) -> None:
        """RPUSH payload and refresh TTL. Raises on hard failure."""
        ...

    def lrange_all(self, key: str) -> List[str]:
        """Return all list entries as strings (may be empty)."""
        ...

    def get_string(self, key: str) -> Optional[str]:
        """Legacy GET for pre-RPUSH journal keys."""
        ...


__all__ = ["JournalStore"]
