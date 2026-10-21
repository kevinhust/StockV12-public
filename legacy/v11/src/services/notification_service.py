"""
Centralized Notification Service.

Responsibilities:
- Send critical operational events to Telegram (and future channels).
- Structured, non-spammy notifications for:
  - Stock pool / watchlist updates (full list)
  - Order lifecycle (intent -> submitted -> filled)
  - Position / DailyReview decisions
  - Other high-signal events

This is the "radar" channel. Complex approval and deep review should go to web dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("notification_service")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("OPERATOR_TELEGRAM_ID") or os.getenv("TELEGRAM_CHAT_ID")

# Simple in-memory rate limit (per event_type + key)
_rate_limits: Dict[str, float] = {}


def _should_send(event_key: str, min_interval_sec: int = 60) -> bool:
    """Basic dedup to avoid spam for the same event."""
    now = time.time()
    last = _rate_limits.get(event_key, 0)
    if now - last < min_interval_sec:
        return False
    _rate_limits[event_key] = now
    return True


def _md_escape(s: str) -> str:
    """Escape Telegram Markdown (legacy) special chars in user-supplied data.

    Telegram's parser greedily interprets * and _ as bold/italic markers; if a
    `*` appears inside a JSON-serialized details dict (e.g. the heartbeat
    "price:*:current" pattern), the parser sees an unclosed bold entity and
    rejects the whole message with HTTP 400 "can't parse entities".

    We escape the user data fields (details/extra dicts) only — the formatter's
    own intentional bold/italic/code spans are constructed on plain literal
    strings and don't need escaping.
    """
    if not s:
        return s
    return (
        s.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def _send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """Core sender. Reuses pattern from existing alert code."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured, skipping notification")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],  # Telegram hard limit
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            return True
        logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logger.error(f"Telegram send exception: {e}")
        return False


class NotificationService:
    """Unified notification interface for critical trading events."""

    def __init__(self, mode: str = "paper_ibkr"):
        self.mode = mode
        self.prefix = "📢" if "paper" in mode.lower() else "🔴 LIVE"

    # ---------------- Stock Pool ----------------
    def notify_stock_pool_updated(
        self,
        tier: str,
        symbols: List[str],
        source: str = "scanner",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Full list as requested."""
        if not symbols:
            return

        key = f"stock_pool:{tier}"
        if not _should_send(key, min_interval_sec=300):  # 5 min dedup
            return

        count = len(symbols)
        symbol_list = ", ".join(symbols)

        text = (
            f"{self.prefix} *Stock Pool Updated*\n"
            f"Tier: `{tier}` | Count: `{count}` | Source: {source}\n"
            f"Symbols: {symbol_list}"
        )

        if extra:
            text += f"\nExtra: {_md_escape(json.dumps(extra, ensure_ascii=False)[:200])}"

        _send_telegram(text)

    # ---------------- Order Lifecycle ----------------
    def notify_order_intent(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: Optional[float],
        size_pct: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Decision to buy/sell made (before actual submission)."""
        key = f"order_intent:{symbol}:{side}"
        if not _should_send(key, min_interval_sec=30):
            return

        price_str = f"@{price:.2f}" if price else "MKT"
        size_str = f" ({size_pct*100:.1f}% NAV)" if size_pct else ""

        text = (
            f"{self.prefix} *Order Intent*\n"
            f"{side} {qty} {symbol} {price_str}{size_str}\n"
        )
        if reason:
            text += f"Reason: {reason}"

        _send_telegram(text)

    def notify_order_submitted(
        self,
        symbol: str,
        side: str,
        qty: int,
        parent_order_id: str,
        sl_price: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Bracket / order actually sent to broker."""
        key = f"order_submitted:{parent_order_id}"
        if not _should_send(key, min_interval_sec=10):
            return

        sl_str = f" SL={sl_price:.2f}" if sl_price else ""
        text = (
            f"{self.prefix} *Order Submitted*\n"
            f"{side} {qty} {symbol}\n"
            f"Parent: `{parent_order_id}`{sl_str}"
        )

        if extra:
            text += f"\n{_md_escape(json.dumps(extra, ensure_ascii=False)[:150])}"

        _send_telegram(text)

    def notify_order_filled(
        self,
        symbol: str,
        side: str,
        qty: int,
        fill_price: float,
        order_id: str,
        is_sl: bool = False,
    ) -> None:
        """Fill confirmation (entry or stop loss)."""
        key = f"order_filled:{order_id}"
        if not _should_send(key, min_interval_sec=5):
            return

        fill_type = "SL Fill" if is_sl else "Fill"
        text = (
            f"{self.prefix} *{fill_type}*\n"
            f"{side} {qty} {symbol} @ {fill_price:.2f}\n"
            f"Order: `{order_id}`"
        )
        _send_telegram(text)

    def notify_order_rejected(
        self, symbol: str, side: str, reason: str, order_id: Optional[str] = None
    ) -> None:
        key = f"order_rejected:{symbol}:{side}"
        if not _should_send(key, min_interval_sec=30):
            return

        text = (
            f"{self.prefix} *Order Rejected*\n"
            f"{side} {symbol}\n"
            f"Reason: {reason}"
        )
        _send_telegram(text)

    # ---------------- Daily Review & Position Lifecycle ----------------
    def notify_daily_review(
        self,
        symbol: str,
        action: str,  # HOLD / TRIM / EXIT / TIGHTEN / ALERT
        reason_code: str,
        pnl_pct: Optional[float] = None,
        days_held: Optional[int] = None,
    ) -> None:
        key = f"daily_review:{symbol}:{action}"
        if not _should_send(key, min_interval_sec=60):
            return

        pnl_str = f" PnL={pnl_pct:+.1f}%" if pnl_pct is not None else ""
        days_str = f" Days={days_held}" if days_held is not None else ""

        text = (
            f"{self.prefix} *DailyReview*\n"
            f"{symbol}: {action}{pnl_str}{days_str}\n"
            f"Reason: {reason_code}"
        )
        _send_telegram(text)

    def notify_position_state_change(
        self,
        symbol: str,
        old_state: Optional[str],
        new_state: str,
        entry_price: Optional[float] = None,
        qty: Optional[int] = None,
    ) -> None:
        key = f"position_state:{symbol}:{new_state}"
        if not _should_send(key, min_interval_sec=30):
            return

        entry_str = f" @ {entry_price:.2f}" if entry_price else ""
        qty_str = f" x{qty}" if qty else ""

        text = (
            f"{self.prefix} *Position {new_state}*\n"
            f"{symbol}{entry_str}{qty_str}"
        )
        if old_state:
            text += f" (from {old_state})"

        _send_telegram(text)

    # ---------------- Generic critical event ----------------
    def notify_critical(self, title: str, message: str, severity: str = "INFO") -> None:
        """For one-off important events."""
        key = f"critical:{title}"
        if not _should_send(key, min_interval_sec=60):
            return

        icon = "🔴" if severity.upper() in ("ERROR", "CRITICAL") else self.prefix
        text = f"{icon} *{title}*\n{message}"
        _send_telegram(text)

    # ---------------- System Health ----------------
    def notify_system_health(
        self,
        issue: str,
        details: Dict[str, Any],
        severity: str = "ERROR",
        source: str = "system",
    ) -> None:
        """Push abnormal system health conditions.
        
        Examples:
        - High rejection rate
        - Broker disconnect
        - Redis / data freshness issues
        - Scanner / review failures
        - Self-heal events
        """
        key = f"health:{source}:{issue}"
        # Health issues can be important, dedup less aggressively
        if not _should_send(key, min_interval_sec=120):
            return

        icon = "🔴" if severity.upper() in ("ERROR", "CRITICAL", "HALT") else "⚠️"
        detail_str = _md_escape(json.dumps(details, ensure_ascii=False, default=str)[:300])

        text = (
            f"{icon} *SYSTEM HEALTH: {issue}*\n"
            f"Source: {source} | Severity: {severity}\n"
            f"Details: {detail_str}\n"
            f"Time: {datetime.now().isoformat()}\n"
            f"View full details: run `streamlit run scripts/review_dashboard.py`"
        )
        _send_telegram(text)


# Convenience global instance (most code can just import and use)
notifier = NotificationService(mode=os.getenv("TRADING_MODE", "paper_ibkr"))

# Re-export for convenience in both main and v12 code
__all__ = ["NotificationService", "notifier"]
