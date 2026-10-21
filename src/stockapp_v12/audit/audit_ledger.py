"""
AuditLedger — durable mirror of TradeJournal decisions to local disk.

Spec: Phase A1 (council verdict 2026-07-07).

Design choices:
- Storage: NDJSON (one JSON record per line) at ``data/audit/YYYY-MM-DD.jsonl``.
  NDJSON is append-only, grep / jq friendly, and language-agnostic.  Parquet
  was considered but introduces pyarrow as a hard runtime dep that would
  couple the entry path to a heavy C++ library.  NDJSON keeps the ledger
  readable with `tail` even when the server is on fire.
- Atomicity: every record is written via append (single ``write``+``flush``
  on a per-process file handle); failures are logged but never block the
  trading path (TradeJournal is the source of truth, this is a mirror).
- Schema versioning: each record carries ``schema_version=1``.  Bumping
  the schema is a code change here + a one-shot migration in `replay()`.
- File rotation: one file per UTC-trading date (NY-local); the file handle
  re-opens lazily if the day changes mid-process.

This module does **not** depend on Redis beyond receiving the TradeJournal
record from a caller.  It is intentionally side-effect-only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
EST = ZoneInfo("America/New_York")

DEFAULT_ROOT = Path("data/audit")
SCHEMA_VERSION = 1


class AuditLedger:
    """Append-only mirror of TradeJournal records to per-day NDJSON files.

    Thread-safe via a single re-entrant lock.  One file handle per day is
    cached and reopened lazily on date rollover.
    """

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        now_provider=None,
        enabled: bool = True,
    ):
        self._root = Path(root) if root else DEFAULT_ROOT
        self._root.mkdir(parents=True, exist_ok=True)
        self._enabled = enabled
        self._now_provider = now_provider or (lambda: datetime.now(EST))
        self._lock = threading.Lock()
        self._current_date: Optional[str] = None
        self._current_path: Optional[Path] = None
        self._current_handle = None

    # ----- public API -----

    def record(
        self,
        *,
        symbol: str,
        mode: str,
        stage: str,
        decision: str,
        reason_code: str,
        reason_detail: str,
        extra: Optional[Dict[str, Any]] = None,
        source: str = "trade_journal",
    ) -> bool:
        """Append one record.  Returns True on success, False on failure."""
        if not self._enabled:
            return False
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "ts": self._now_provider().isoformat(),
                "source": source,
                "symbol": symbol.upper(),
                "mode": mode,
                "stage": stage,
                "decision": decision,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
                "extra": extra or {},
            }
            line = json.dumps(payload, ensure_ascii=False, default=str)
            with self._lock:
                path = self._open_for_now()
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    # P1-6 (council 2026-07-09): flush + fsync after every
                    # write so a crash doesn't lose the last buffered records.
                    # The docstring claimed "atomicity via single write+flush"
                    # but the code only relied on the `with` block's implicit
                    # close-flush — which buffers in the OS page cache.
                    fh.flush()
                    os.fsync(fh.fileno())
            return True
        except Exception as e:
            logger.error("AuditLedger.record failed for %s: %s", symbol, e)
            return False

    def replay(self, date: str) -> list:
        """Read all records for a single trading date.  Useful for audits."""
        path = self._path_for(date)
        if not path.exists():
            return []
        out = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("AuditLedger.replay corrupt line in %s: %s", path, e)
        return out

    # ----- internal -----

    def _open_for_now(self) -> Path:
        """Return today's path.  Caller must hold the lock."""
        today = self._now_provider().strftime("%Y-%m-%d")
        if self._current_date != today or self._current_path is None:
            self._current_path = self._path_for(today)
            self._current_date = today
        return self._current_path

    def _path_for(self, date: str) -> Path:
        return self._root / f"{date}.jsonl"


def audit_wrapped_journal(journal, ledger: AuditLedger):
    """Decorator-like helper that mirrors TradeJournal writes to AuditLedger.

    TradeJournal methods (`append`, `log_pass`, `log_deny`, `log_allow`,
    `log_buy`, `log_exit`) all funnel through `append()` (or the existing
    `log_pass/log_deny` wrappers).  We wrap the bound methods so every
    journal write fans out to the ledger.

    Usage:
        journal = TradeJournal(redis_client)
        wrapped = audit_wrapped_journal(journal, AuditLedger(root=Path("data/audit")))
        # Use `wrapped` in place of `journal` everywhere downstream.
    """
    return _AuditJournalProxy(journal, ledger)


class _AuditJournalProxy:
    """Thin proxy that delegates every TradeJournal call to the ledger
    as well, recording the *result* of the call (no mutation)."""

    KNOWN_JOURNAL_METHODS = frozenset({
        "append", "log_pass", "log_deny", "log_allow", "log_buy", "log_exit",
    })

    def __init__(self, journal, ledger: AuditLedger):
        self._j = journal
        self._l = ledger

    def __getattr__(self, name):
        # Only proxy known journal methods; private/dunder attrs pass through
        # directly to the underlying journal without mirroring to the ledger.
        if name.startswith("_") or name not in self.KNOWN_JOURNAL_METHODS:
            return getattr(self._j, name)
        return _AuditMethod(self, name)


class _AuditMethod:
    def __init__(self, proxy: "_AuditJournalProxy", name: str):
        self._proxy = proxy
        self._name = name

    def __call__(self, *args, **kwargs):
        result = getattr(self._proxy._j, self._name)(*args, **kwargs)
        try:
            record = self._build_record(self._name, args, kwargs)
            if record is not None:
                self._proxy._l.record(**record)
        except Exception as e:  # never break the trading path
            logger.warning("AuditProxy mirror failed for %s: %s", self._name, e)
        return result

    @staticmethod
    def _build_record(method_name: str, args, kwargs) -> Optional[Dict[str, Any]]:
        symbol = args[0] if args else kwargs.get("symbol")
        mode = kwargs.get("mode") or (args[1] if len(args) > 1 else "unknown")
        if symbol is None:
            return None
        extra: Dict[str, Any] = {}
        # Best-effort: pull the most common kwargs into extra.
        for k in (
            "analyst",
            "requested_size_pct",
            "parent_order_id",
            "sl_order_id",
            "size_pct",
            "exit_reason",
            "pnl_pct",
            "hold_days",
            "mfe_pct",
            "mae_pct",
            "risk_meta",
        ):
            if k in kwargs:
                extra[k] = kwargs[k]
        return {
            "symbol": symbol,
            "mode": mode,
            "stage": kwargs.get("stage", "entry" if method_name != "log_exit" else "exit"),
            "decision": _decision_for_method(method_name),
            "reason_code": kwargs.get("reason_code", ""),
            "reason_detail": kwargs.get("reason_detail", ""),
            "extra": extra,
            "source": "trade_journal_proxy",
        }


def _decision_for_method(name: str) -> str:
    if name == "log_buy":
        return "BUY"
    if name == "log_allow":
        return "ALLOW"
    if name == "log_deny":
        return "DENY"
    if name == "log_pass":
        return "PASS"
    if name == "log_exit":
        return "CLOSED"
    return "APPEND"
