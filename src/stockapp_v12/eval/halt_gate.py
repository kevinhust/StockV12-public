"""HaltGate — Round 4 council paper-trade instrument.

Council Round 4 verdict (2026-07-10): adopt as paper-trade gate with
kill-switch + 60-day OOS validation. Selection-swap rejected.

A HaltGate decides per-date whether to skip all trades based on trailing
rolling accuracy. Two modes:
  - Reactive (default): each date independently evaluated. If rolling_acc
    < threshold, halt that date only.
  - Persistent: once triggered, stay halted until rolling_acc >=
    recovery_threshold. Prevents "1-day recovery → re-enter → lose again"
    patterns.

Design contract (CRITICAL — production correctness depends on this):
  - `should_halt(date)` MUST NOT include `date`'s trades (lookahead-safe).
    This is enforced by design: callers MUST call `should_halt(date)`
    BEFORE `update(date, trades)` for the same date.
  - Use `decide(date, trades)` as the recommended entry point — it
    atomically does should_halt → update in that order.

Usage:
    gate = HaltGate(window=3, threshold=0.40, persistent=True)
    # Load prior history from per-date JSONs
    for d, rows in prior_per_date_trades:
        gate.update(d, rows)
    # For today: decide BEFORE consuming today's trades
    should_halt, ctx = gate.decide(today, todays_trades)
    if should_halt:
        log_skip(today, ctx)
    # else: take trades normally

Thread safety: NOT thread-safe. Use one HaltGate instance per worker,
or wrap with an external lock.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class HaltDecision:
    """Structured halt decision with full audit context. Immutable."""

    halt: bool
    rolling_acc: Optional[float]
    n_prior_trades: int
    reason: Optional[str]

    def to_dict(self) -> dict:
        return {
            "halt": self.halt,
            "rolling_acc": self.rolling_acc,
            "n_prior_trades": self.n_prior_trades,
            "reason": self.reason,
        }


class HaltGate:
    """Per-date halt decision based on trailing rolling accuracy.

    Args:
      window: rolling accuracy window in trading days (default 3).
      threshold: halt if rolling accuracy < threshold (default 0.40).
      persistent: if True, stay halted until recovery_threshold met.
                  If False (reactive), each date evaluated independently.
      recovery_threshold: in persistent mode, exit halt when
                  rolling_acc >= recovery_threshold (default 0.50).
      min_history_days: require at least this many prior dates before
                  evaluating (default 3). Below this → no halt.
      min_trades_in_window: require at least this many settled trades
                  in the rolling window before evaluating (default 10).
                  Below this → no halt (insufficient evidence).

    State:
      Internally tracks (date_str, [trades]) pairs in insertion order.
      No persistence — caller is responsible for re-loading history
      across processes (typically by reading per-date JSON files).
    """

    def __init__(
        self,
        window: int = 3,
        threshold: float = 0.40,
        persistent: bool = False,
        recovery_threshold: float = 0.50,
        min_history_days: int = 3,
        min_trades_in_window: int = 10,
    ):
        if window < 1:
            raise ValueError(f"window must be >= 1, got {window}")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")
        if not 0.0 <= recovery_threshold <= 1.0:
            raise ValueError(f"recovery_threshold must be in [0, 1], got {recovery_threshold}")
        if min_history_days < 1:
            raise ValueError(f"min_history_days must be >= 1, got {min_history_days}")
        if min_trades_in_window < 1:
            raise ValueError(f"min_trades_in_window must be >= 1, got {min_trades_in_window}")

        self._window = window
        self._threshold = threshold
        self._persistent = persistent
        self._recovery_threshold = recovery_threshold
        self._min_history_days = min_history_days
        self._min_trades_in_window = min_trades_in_window

        # History: date_str -> [trades]. OrderedDict to preserve insertion order.
        self._history: OrderedDict[str, list[dict]] = OrderedDict()

        # Persistent-mode latch: True once halt is triggered, cleared on recovery.
        self._in_persistent_halt: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, date_str: str, trades: list[dict]) -> None:
        """Add a date's trades to history.

        Call this AFTER `should_halt(date_str)` for the same date to maintain
        lookahead safety. Or use `decide()` which atomically does both.

        If `date_str` is already in history, the new trades replace the
        previous ones (last-writer-wins; useful for re-loading a corrected
        JSON file).

        Args:
          date_str: ISO date string (e.g., "2026-05-19").
          trades: list of trade dicts. Each must have `forecast_ret_pct`
                  and optionally `actual_ret_pct` (None if unsettled).
                  Both fields must be finite numbers when not None.
        """
        # Pre-validate trade dict shape — fail fast at ingestion with a
        # clear error pointing at the offending date+index.
        for i, t in enumerate(trades):
            fcst = t.get("forecast_ret_pct")
            if fcst is None:
                raise ValueError(
                    f"HaltGate.update: trade[{i}] on {date_str} missing 'forecast_ret_pct'"
                )
            if not _is_finite_number(fcst):
                raise ValueError(
                    f"HaltGate.update: trade[{i}] on {date_str} has non-finite "
                    f"forecast_ret_pct={fcst!r}"
                )
            actual = t.get("actual_ret_pct")
            if actual is not None and not _is_finite_number(actual):
                raise ValueError(
                    f"HaltGate.update: trade[{i}] on {date_str} has non-finite "
                    f"actual_ret_pct={actual!r}"
                )

        # Store as list copy so caller mutations don't affect us
        self._history[date_str] = list(trades)

    def should_halt(self, date_str: str) -> tuple[bool, dict]:
        """Decide whether to halt for `date_str`.

        MUST be called BEFORE `update(date_str, ...)` for the same date to
        preserve lookahead safety.

        Returns:
          (halt_bool, context_dict) where context_dict has:
            - halt: bool (echoed for convenience)
            - rolling_acc: float | None (None if insufficient history)
            - n_prior_trades: int (count of settled trades in window)
            - reason: str | None (None = no halt, "insufficient_history",
              "insufficient_trades", "acc<{threshold}", or
              "persistent_cooldown")
        """
        decision = self._decide_internal(date_str)
        return decision.halt, decision.to_dict()

    def decide(self, date_str: str, trades: list[dict]) -> tuple[bool, dict]:
        """Atomically: should_halt(date_str) using PRIOR state, then update.

        Recommended entry point for production use — eliminates the risk
        of calling update before should_halt by accident.

        Returns:
          (halt_bool, context_dict) — see `should_halt` for schema.
        """
        decision = self._decide_internal(date_str)
        self.update(date_str, trades)
        return decision.halt, decision.to_dict()

    @property
    def is_in_persistent_halt(self) -> bool:
        """True if currently in a persistent halt (testing/debug only)."""
        return self._in_persistent_halt

    @property
    def history_size(self) -> int:
        """Number of dates in history (testing/debug only)."""
        return len(self._history)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _decide_internal(self, date_str: str) -> HaltDecision:
        """Core decision logic. Assumes `date_str` is NOT in history yet.

        If `date_str` IS in history (caller misuse), the decision is still
        made using only dates strictly before `date_str` to avoid leakage.
        """
        # Build rolling window from dates strictly before date_str
        prior_dates = [(d, t) for d, t in self._history.items() if d < date_str]

        # Guard 1: minimum history
        if len(prior_dates) < self._min_history_days:
            return HaltDecision(
                halt=False,
                rolling_acc=None,
                n_prior_trades=0,
                reason="insufficient_history",
            )

        # Take last `window` dates
        window_dates = prior_dates[-self._window:]

        # Aggregate settled trades from the window.
        # Filter: forecast_ret_pct must be finite (NaN/inf treated as missing),
        # actual_ret_pct must be finite-or-None. update() pre-validates shape,
        # so by the time we reach here all values are valid numbers or None.
        all_trades: list[dict] = []
        for _, trades in window_dates:
            for t in trades:
                if (
                    t.get("actual_ret_pct") is not None
                    and _is_finite_number(t["actual_ret_pct"])
                    and _is_finite_number(t["forecast_ret_pct"])
                ):
                    all_trades.append(t)

        # Guard 2: minimum trades in window
        if len(all_trades) < self._min_trades_in_window:
            return HaltDecision(
                halt=False,
                rolling_acc=None,
                n_prior_trades=len(all_trades),
                reason="insufficient_trades",
            )

        # Compute rolling accuracy
        rolling_acc = self._compute_accuracy(all_trades)

        # Apply halt logic
        if self._persistent and self._in_persistent_halt:
            # We're in persistent halt. Check if recovery threshold is met.
            if rolling_acc >= self._recovery_threshold:
                self._in_persistent_halt = False
                return HaltDecision(
                    halt=False,
                    rolling_acc=rolling_acc,
                    n_prior_trades=len(all_trades),
                    reason=None,
                )
            # Stay halted
            return HaltDecision(
                halt=True,
                rolling_acc=rolling_acc,
                n_prior_trades=len(all_trades),
                reason="persistent_cooldown",
            )

        # Not in persistent halt (or persistent=False). Check threshold.
        if rolling_acc < self._threshold:
            if self._persistent:
                self._in_persistent_halt = True
            return HaltDecision(
                halt=True,
                rolling_acc=rolling_acc,
                n_prior_trades=len(all_trades),
                reason=f"acc<{self._threshold}",
            )

        # Above threshold — no halt
        return HaltDecision(
            halt=False,
            rolling_acc=rolling_acc,
            n_prior_trades=len(all_trades),
            reason=None,
        )

    @staticmethod
    def _compute_accuracy(trades: list[dict]) -> float:
        """Fraction of settled trades where sign(forecast) == sign(actual)."""
        if not trades:
            return 0.0
        correct = sum(
            1 for t in trades
            if (t["forecast_ret_pct"] > 0) == (t["actual_ret_pct"] > 0)
        )
        return correct / len(trades)


def _is_finite_number(x) -> bool:
    """True if x is a finite number (rejects None, NaN, inf, strings, etc.)."""
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(x)