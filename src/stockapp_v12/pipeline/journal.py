"""
Append-only trade decision journal.
Spec: v12/docs/TRADING_PIPELINE.md#decision-explainability-contract-pipeline

P1-7 (council 2026-07-09): ``append`` was fail-open — Redis errors were
swallowed and every decision silently dropped from the audit trail (INV-5
violation). Now ``append`` raises ``JournalWriteError`` by default.
``log_allow`` and ``log_buy`` use the strict path (post-execution records
that MUST be in the audit trail). ``log_pass`` and ``log_deny`` use the
lenient path (pre-execution — the trade isn't happening, so we don't crash
the orchestrator over a missing PASS/DENY record).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Union

from zoneinfo import ZoneInfo

from stockapp_v12.adapters.stores.redis_stores import RedisJournalStore
from stockapp_v12.contracts.journal_store import JournalStore

logger = logging.getLogger(__name__)
EST = ZoneInfo("America/New_York")


class JournalWriteError(RuntimeError):
    """Raised when a journal append fails and ``strict=True``.

    Callers that need INV-5 enforcement (log_allow, log_buy) should let
    this propagate so the orchestrator can abort. Callers that can
    tolerate a missing record (log_pass, log_deny) should catch it.
    """


class TradeJournal:
    SCHEMA_VERSION = 1

    def __init__(
        self,
        redis_client=None,
        *,
        store: Optional[JournalStore] = None,
    ):
        if store is not None:
            self._store: JournalStore = store
        elif redis_client is not None:
            self._store = RedisJournalStore(redis_client)
        else:
            raise ValueError("TradeJournal requires redis_client or store")
        self._r = redis_client  # BC
    @staticmethod
    def _today_key() -> str:
        return datetime.now(EST).strftime("%Y-%m-%d")

    def _key(self, symbol: str, date: Optional[str] = None) -> str:
        d = date or self._today_key()
        return f"journal:{d}:{symbol.upper()}"

    def append(
        self,
        symbol: str,
        *,
        mode: str,
        stage: str,
        decision: str,
        reason_code: str,
        reason_detail: str,
        extra: Optional[Dict[str, Any]] = None,
        date: Optional[str] = None,
        strict: bool = True,
    ) -> None:
        """Append a journal record. Raises ``JournalWriteError`` on Redis
        failure when ``strict=True`` (default).

        P1-7: previously the exception was swallowed and the record silently
        disappeared from the audit trail. Now callers can choose:
        - ``strict=True`` (log_allow, log_buy): raise → orchestrator aborts
        - ``strict=False`` (log_pass, log_deny): log + swallow → non-critical
        """
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "symbol": symbol.upper(),
            "mode": mode,
            "stage": stage,
            "decision": decision,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "ts": datetime.now(EST).isoformat(),
        }
        if extra:
            record.update(extra)

        key = self._key(symbol, date)
        try:
            self._store.append(
                key,
                json.dumps(record, ensure_ascii=False),
                ttl_seconds=86400 * 365,  # 365d retention per audit spec
            )
        except Exception as e:
            logger.error(
                "TradeJournal append failed for %s (%s/%s): %s",
                symbol, decision, reason_code, e,
            )
            if strict:
                raise JournalWriteError(
                    f"Journal append failed for {symbol} ({decision}/{reason_code}): "
                    f"{type(e).__name__}: {e}"
                ) from e

    def read_entries(self, symbol: str, *, date: Optional[str] = None) -> list:
        """Read all journal entries for a symbol on a given date.

        Uses LRANGE (compatible with the RPUSH-based append). For backwards
        compatibility with old GET/SET keys, falls back to GET if LRANGE
        returns empty but the key exists as a string.
        """
        key = self._key(symbol, date)
        try:
            raw_entries = self._store.lrange_all(key)
            if raw_entries:
                return [json.loads(e) for e in raw_entries]
            # Backwards compat: old GET/SET format stored a JSON list string
            existing = self._store.get_string(key)
            if existing:
                parsed = json.loads(existing)
                return parsed if isinstance(parsed, list) else [parsed]
            return []
        except Exception as e:
            logger.error("TradeJournal read_entries failed for %s: %s", symbol, e)
            return []

    def log_pass(
        self,
        symbol: str,
        mode: str,
        reason_code: str,
        reason_detail: str,
        analyst: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Non-critical: the trade isn't happening. Log leniently (strict=False)."""
        extra = {"analyst": analyst} if analyst else None
        self.append(
            symbol,
            mode=mode,
            stage="entry",
            decision="PASS",
            reason_code=reason_code,
            reason_detail=reason_detail,
            extra=extra,
            strict=False,  # P1-7: PASS is pre-execution — don't crash on journal blip
        )

    def log_deny(
        self,
        symbol: str,
        mode: str,
        reason_code: str,
        reason_detail: str,
        *,
        requested_size_pct: float = 0.0,
        analyst: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Non-critical: the trade was denied. Log leniently (strict=False)."""
        extra = {
            "requested_size_pct": requested_size_pct,
            "allowed_size_pct": 0.0,
        }
        if analyst:
            extra["analyst"] = analyst
        self.append(
            symbol,
            mode=mode,
            stage="entry",
            decision="DENY",
            reason_code=reason_code,
            reason_detail=reason_detail,
            extra=extra,
            strict=False,  # P1-7: DENY is pre-execution — don't crash on journal blip
        )

    def log_allow(
        self,
        symbol: str,
        mode: str,
        risk_meta: Dict[str, Any],
        analyst: Optional[Dict[str, Any]] = None,
    ) -> None:
        extra = {
            "allowed_size_pct": risk_meta.get("size_pct"),
            "risk_amount": risk_meta.get("risk_amount"),
            "stop_distance": risk_meta.get("stop_distance"),
            "atr_14": risk_meta.get("atr_14"),
            "regime": risk_meta.get("regime"),
        }
        if analyst:
            extra["analyst"] = analyst
        self.append(
            symbol,
            mode=mode,
            stage="entry",
            decision="ALLOW",
            reason_code="OK",
            reason_detail="Gatekeeper approved",
            extra=extra,
        )

    def log_buy(
        self,
        symbol: str,
        mode: str,
        *,
        parent_order_id: str,
        sl_order_id: Optional[str],
        size_pct: float,
        analyst: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Filled BUY record (after PositionManager.register)."""
        rec_extra = {
            "parent_order_id": parent_order_id,
            "sl_order_id": sl_order_id,
            "allowed_size_pct": size_pct,
        }
        if analyst:
            rec_extra["analyst"] = analyst
        if extra:
            rec_extra.update(extra)
        self.append(
            symbol,
            mode=mode,
            stage="entry",
            decision="BUY",
            reason_code="FILLED",
            reason_detail=f"parent={parent_order_id}",
            extra=rec_extra,
        )

    def log_exit(
        self,
        symbol: str,
        mode: str,
        *,
        exit_reason: str,
        pnl_pct: Optional[float] = None,
        hold_days: Optional[int] = None,
        mfe_pct: Optional[float] = None,
        mae_pct: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        rec_extra: Dict[str, Any] = {"exit_reason": exit_reason}
        if pnl_pct is not None:
            rec_extra["pnl_pct"] = pnl_pct
        if hold_days is not None:
            rec_extra["hold_days"] = hold_days
        if mfe_pct is not None:
            rec_extra["mfe_pct"] = mfe_pct
        if mae_pct is not None:
            rec_extra["mae_pct"] = mae_pct
        if extra:
            rec_extra.update(extra)
        self.append(
            symbol,
            mode=mode,
            stage="exit",
            decision="CLOSED",
            reason_code=exit_reason.upper(),
            reason_detail="position closed",
            extra=rec_extra,
        )


def make_journal(redis_client, *, audit_enabled: Optional[bool] = None) -> "TradeJournal":
    """Factory: create a TradeJournal, optionally wrapped with AuditLedger mirror.

    When ``audit_enabled`` is None (default), reads the env var
    ``STOCKAPP_V12_AUDIT_MIRROR`` (default ``"0"`` = disabled).

    Returns a TradeJournal (or an audit-wrapped proxy that is duck-type
    compatible) suitable for passing to EntryOrchestrator / DailyReview.
    """
    journal = TradeJournal(redis_client)
    if audit_enabled is None:
        from stockapp_v12.core.limits import _env_truthy
        audit_enabled = _env_truthy("STOCKAPP_V12_AUDIT_MIRROR", default="0")
    if audit_enabled:
        try:
            from stockapp_v12.audit.audit_ledger import audit_wrapped_journal, AuditLedger
            ledger = AuditLedger()
            logger.info("AuditLedger mirror enabled for TradeJournal (NDJSON at data/audit/)")
            return audit_wrapped_journal(journal, ledger)
        except Exception as e:
            logger.warning("Failed to init AuditLedger mirror, continuing without: %s", e)
    return journal

