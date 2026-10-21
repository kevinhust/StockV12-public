"""
Entry evaluation idempotency — one eval per (mode, symbol, trading_date).
Spec: v12/docs/SYSTEM_OVERVIEW.md INV-1

P1-1 (council 2026-07-09): ``already_evaluated`` + ``mark_evaluated`` was a
check-then-act race. Two concurrent orchestrators could both pass INV-1 and
both proceed to evaluate the same symbol. The fix collapses SADD + EXPIRE
into a single atomic Lua ``EVAL`` so the first caller wins and subsequent
callers see the set already populated.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from stockapp_v12.adapters.stores.redis_stores import RedisEntryEvalStore
from stockapp_v12.contracts.entry_eval_store import EntryEvalStore

logger = logging.getLogger(__name__)

EST = ZoneInfo("America/New_York")

#: TTL for the evaluated-set key (2 days = 172800s — survives overnight reruns).
_EVALUATED_TTL_SECONDS = 172800


class EntryEvaluationGuard:
    def __init__(
        self,
        redis_client=None,
        *,
        mode: str = "paper_ibkr",
        store: Optional[EntryEvalStore] = None,
    ):
        if store is not None:
            self._store: EntryEvalStore = store
        elif redis_client is not None:
            self._store = RedisEntryEvalStore(redis_client)
        else:
            raise ValueError("EntryEvaluationGuard requires redis_client or store")
        self._r = redis_client  # BC
        self._mode = mode

    def _date_str(self) -> str:
        return datetime.now(EST).strftime("%Y%m%d")

    def _key(self, date: Optional[str] = None) -> str:
        d = date or self._date_str()
        return f"entry:evaluated:{self._mode}:{d}"

    def _index_key(self, mode: Optional[str] = None) -> str:
        m = mode or self._mode
        return f"position:{m}:index"

    def already_evaluated(self, symbol: str, date: Optional[str] = None) -> bool:
        return self._store.is_member(self._key(date), symbol.upper())

    def mark_evaluated(self, symbol: str, date: Optional[str] = None) -> None:
        self._store.mark(
            self._key(date), symbol.upper(), _EVALUATED_TTL_SECONDS
        )

    def has_open_position(self, symbol: str, mode: Optional[str] = None) -> bool:
        return self._store.is_member(self._index_key(mode), symbol.upper())

    def should_skip_entry(
        self,
        symbol: str,
        *,
        mode: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        sym = symbol.upper()
        m = mode or self._mode

        if self.has_open_position(sym, m):
            return True, "INV-2_OPEN_POSITION"

        if self.already_evaluated(sym, date):
            return True, "INV-1_ALREADY_EVALUATED"

        return False, None

    def begin_entry_evaluation(
        self,
        symbol: str,
        *,
        mode: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Atomically check INV-1/INV-2 and mark as evaluated.

        P1-1: the INV-1 check (already_evaluated) + mark (mark_evaluated) is
        now an atomic Lua SADD+EXPIRE. The first caller gets SADD==1 (proceed),
        subsequent callers get SADD==0 (skip). INV-2 (open position) is checked
        first via SISMEMBER — it's a separate key and can't be atomically
        combined with INV-1 in a single script without significant complexity.
        The INV-2 check is still racy in theory but in practice positions
        change much less frequently than evaluations.

        Fail-closed posture: any Redis error → return ``(False, "INV-1_EVAL_ERROR")``
        so the caller SKIPS the evaluation (doesn't enter). This prevents a
        Redis blip from allowing duplicate evaluations.
        """
        sym = symbol.upper()

        # INV-2: open position check (still a separate SISMEMBER)
        m = mode or self._mode
        try:
            if self.has_open_position(sym, m):
                return False, "INV-2_OPEN_POSITION"
        except Exception as e:
            logger.warning(
                "[entry_guard] INV-2 check failed for %s: %s: %s — failing closed",
                sym, type(e).__name__, e,
            )
            return False, "INV-2_EVAL_ERROR"

        # INV-1: atomic mark via EntryEvalStore.try_mark (Lua on Redis adapter)
        key = self._key(date)
        try:
            first = self._store.try_mark(key, sym, _EVALUATED_TTL_SECONDS)
        except Exception as e:
            logger.warning(
                "[entry_guard] INV-1 atomic mark failed for %s: %s: %s — failing closed",
                sym, type(e).__name__, e,
            )
            return False, "INV-1_EVAL_ERROR"

        if first:
            return True, None
        return False, "INV-1_ALREADY_EVALUATED"
