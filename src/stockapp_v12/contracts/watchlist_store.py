"""WatchlistStore — scanner / entry-window watchlist seam.

Canonical Redis key shapes:
  - watchlist:tier1            ZSET score
  - watchlist:tier1:details    HASH symbol -> JSON
  - watchlist:tier2            ZSET
"""

from __future__ import annotations

from typing import Dict, List, Protocol


class WatchlistStore(Protocol):
    def replace_tier1(
        self,
        zset_key: str,
        details_key: str,
        scores: Dict[str, float],
        details: Dict[str, str],
        *,
        ttl_seconds: int,
    ) -> None:
        """Overwrite tier1 ZSET + details HASH atomically (delete then write)."""
        ...

    def list_members(self, zset_key: str) -> List[str]:
        """ZRANGE 0 -1 — all members (no scores)."""
        ...


__all__ = ["WatchlistStore"]
