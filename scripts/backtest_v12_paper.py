#!/usr/bin/env python
"""
v12/scripts/backtest_v12_paper.py

High-fidelity backtest harness that reuses the actual V12 paper execution stack:
- EntryOrchestrator + real deps (ForecastService, SignalRuleEngine, EntryGuard)
- V12 Gatekeeper adapter (with R-05/R-06 checks + ATR risk sizing)
- BracketOrderService + PaperBroker
- PositionManager + TradeJournal (via fake Redis for state)

Features:
- Synthetic daily bars (easy to swap for real data loader later)
- Configurable risk % (uses real V12 sizing)
- Slippage + commission simulation
- Full R-based expectancy calculation
- Daily new position distribution + R-05/R-06 trigger counts
- Journal-like trade records
- Switchable "real" ForecastService (degraded if no model) or demo backend

Run:
    PYTHONPATH="src:scripts:." python scripts/backtest_v12_paper.py \
        --symbols NVDA,AAPL,TSLA --start 2024-01-01 --end 2025-01-01 \
        --risk-pct 0.01 --use-real-forecast

This is designed to be as close as possible to the live paper path in
lifecycle_scheduler + EntryOrchestrator.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

# --- v12 real components ---
from stockapp_v12.services.entry_orchestrator import EntryOrchestrator, EntryContext
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
from stockapp_v12.services.forecast_service import ForecastService
from stockapp_v12.services.signal_rule_engine import SignalRuleEngine, SignalRuleConfig
from stockapp_v12.core.execution.bracket_order import BracketOrderService
from stockapp_v12.core.execution.paper_broker import PaperBroker
from stockapp_v12.services.position_manager import PositionManager
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.bridge.gatekeeper import (
    compute_position_size,
    check_daily_trade_cap,
    check_symbol_cooldown,
    increment_daily_trade_count,
)
from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.services.yolo_filter import YoloNoTradeFilter  # Wave 9

# ---------------------------------------------------------------------------
# Fake Redis for backtest state (paper mode only - no real DB)
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stand-in that supports all keys used by PM / Journal / risk checks.

    TTL semantics: this backtest advances a virtual clock by calling `tick()` each trading
    day. `setex(key, ttl_seconds, value)` stores the value and a deadline expressed in
    virtual seconds (using SECONDS_PER_STEP). `get()` returns None for expired keys, and
    `ttl()` returns seconds remaining. This lets R-06 (48-hour symbol cooldown) actually
    expire across backtest days instead of blocking re-entry forever.
    """
    # 1 backtest day = SECONDS_PER_STEP virtual seconds (so 48h cooldown = 2 steps).
    SECONDS_PER_STEP = 24 * 3600

    def __init__(self):
        self._data: Dict[str, str] = {}
        self._deadline_step: Dict[str, int] = {}  # key -> step at which it expires
        self._sets: Dict[str, set] = defaultdict(set)
        self._lists: Dict[str, List[str]] = defaultdict(list)
        self._step: int = 0  # virtual clock, in trading days

    def tick(self) -> None:
        """Advance virtual clock by one trading day."""
        self._step += 1

    def close(self): pass

    def _expired(self, key: str) -> bool:
        deadline = self._deadline_step.get(key)
        return deadline is not None and self._step >= deadline

    def get(self, key: str) -> Optional[str]:
        if self._expired(key):
            self._data.pop(key, None)
            self._deadline_step.pop(key, None)
            return None
        return self._data.get(key)

    def set(self, key: str, value: str) -> bool:
        self._data[key] = str(value)
        # a plain `set` clears any prior TTL
        self._deadline_step.pop(key, None)
        return True

    def setex(self, key: str, ttl: int, value: str) -> bool:
        """Set with TTL in real seconds; converted to virtual steps for backtest."""
        self._data[key] = str(value)
        steps = max(1, int((ttl + self.SECONDS_PER_STEP - 1) // self.SECONDS_PER_STEP))
        self._deadline_step[key] = self._step + steps
        return True

    def delete(self, key: str) -> int:
        existed = 1 if key in self._data or key in self._sets or key in self._lists else 0
        self._data.pop(key, None)
        self._deadline_step.pop(key, None)
        self._sets.pop(key, None)
        self._lists.pop(key, None)
        return existed

    def pipeline(self):
        return self  # simplistic - operations are immediate

    def execute(self):
        return [True]

    def incr(self, key: str) -> int:
        val = int(self._data.get(key, 0)) + 1
        self._data[key] = str(val)
        return val

    def ttl(self, key: str) -> int:
        if key not in self._data:
            return -2
        if self._expired(key):
            self._data.pop(key, None)
            self._deadline_step.pop(key, None)
            return -2
        deadline = self._deadline_step.get(key)
        if deadline is None:
            return -1  # no TTL set
        remaining_steps = max(0, deadline - self._step)
        return remaining_steps * self.SECONDS_PER_STEP

    def sadd(self, key: str, member: str) -> int:
        before = len(self._sets[key])
        self._sets[key].add(str(member))
        return len(self._sets[key]) - before

    def srem(self, key: str, member: str) -> int:
        before = len(self._sets.get(key, set()))
        self._sets[key].discard(str(member))
        return before - len(self._sets[key])

    def sismember(self, key: str, member: str) -> bool:
        return str(member) in self._sets.get(key, set())

    def smembers(self, key: str) -> set:
        return set(self._sets.get(key, set()))

    def expire(self, key: str, ttl: int) -> bool:
        """Mirror real Redis expire — set TTL on existing key."""
        if key in self._data or key in self._sets:
            self._deadline_step[key] = self._step + max(1, int((ttl + self.SECONDS_PER_STEP - 1) // self.SECONDS_PER_STEP))
            return True
        return False

    def smembers(self, key: str) -> set:
        return self._sets.get(key, set())

    def sismember(self, key: str, member: str) -> bool:
        return str(member) in self._sets.get(key, set())

    def rpush(self, key: str, *values: str) -> int:
        self._lists[key].extend([str(v) for v in values])
        return len(self._lists[key])

    def lrange(self, key: str, start: int, end: int) -> List[str]:
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]

    def expire(self, key: str, ttl: int) -> bool:
        return True


# ---------------------------------------------------------------------------
# Demo backend for ForecastService (use when no real Chronos model)
# Produces controllable positive forecasts for harness validation.
# In real runs you would pass a real Chronos backend or none (degrade).
# ---------------------------------------------------------------------------
class DemoForecastBackend:
    """Optimistic but reasonable demo backend for harness validation."""
    def __init__(self, expected_return: float = 0.035, uncertainty: float = 0.015):
        self.expected_return = expected_return
        self.uncertainty = uncertainty

    def detect(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        if df is None or df.empty or "close" not in df.columns:
            return []
        price = float(df.iloc[-1]["close"])
        return [{
            "meta": {
                "current_price": price,
                "forecast_price": price * (1 + self.expected_return),
                "forecast_q10": price * (1 - self.uncertainty),
                "forecast_q90": price * (1 + self.uncertainty * 1.5),
            }
        }]


# ---------------------------------------------------------------------------
# Core backtest logic
# ---------------------------------------------------------------------------
@dataclass
class BacktestTrade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    pnl_pct: float          # realized % on capital
    pnl_r: float            # realized R (after costs)
    hold_days: int
    size_pct: float
    risk_amount: float
    reason_code: str
    sl_price: float
    entry_dt: date = None   # Wave 10: typed entry date for window filtering


def generate_synthetic_bars(
    symbols: List[str],
    start: date,
    end: date,
    seed: int = 42,
    base_price: float = 140.0,
) -> Dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    n_days = (end - start).days + 1
    frames: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        base = base_price + rng.normal(0, 15)
        rets = rng.normal(0.0006, 0.018, n_days)
        closes = base * np.cumprod(1 + rets)
        atrs = np.abs(rng.normal(3.8, 0.9, n_days))
        idx = pd.date_range(start=start, periods=n_days, freq="D")
        # Build minimal feature columns so the real Chronos model (which expects
        # the Phase 1 feature schema) can be exercised end-to-end. Synthetic
        # values are placeholders; the harness is intended for plumbing validation,
        # not signal quality (see `note` in the JSON output).
        ret_1d = np.concatenate([[0.0], np.diff(closes) / closes[:-1]])
        vol_20d = pd.Series(closes).pct_change().rolling(20).std().fillna(0.015).values
        df = pd.DataFrame({
            "timestamp": idx,
            "close": closes,
            "atr_14": atrs,
            # stub technical features (chronos will mostly use close; the others
            # keep the TimeSeriesDataFrame schema consistent)
            "ret_1d": ret_1d,
            "ret_5d": np.concatenate([[0.0]*5, (closes[5:] - closes[:-5]) / closes[:-5]]) if n_days > 5 else np.zeros(n_days),
            "ret_20d": np.concatenate([[0.0]*20, (closes[20:] - closes[:-20]) / closes[:-20]]) if n_days > 20 else np.zeros(n_days),
            "vol_10d": pd.Series(closes).pct_change().rolling(10).std().fillna(0.015).values,
            "vol_20d": vol_20d,
            "rsi_14": 50.0 + rng.normal(0, 5, n_days),
            "macd": rng.normal(0, 1.0, n_days),
            "macd_signal": rng.normal(0, 1.0, n_days),
            "macd_hist": rng.normal(0, 0.5, n_days),
            "bb_pct_b_20": np.clip(rng.normal(0.5, 0.2, n_days), 0, 1),
            "obv": np.cumsum(rng.normal(0, 1e6, n_days)),
            "obv_slope_10": rng.normal(0, 1e5, n_days),
            "dist_ma_20": rng.normal(0, 0.02, n_days),
            "dist_ma_50": rng.normal(0, 0.03, n_days),
        }, index=idx)
        df.index.name = "timestamp"
        frames[sym] = df
    return frames


def load_real_bars(
    symbols: List[str],
    start: date,
    end: date,
    *,
    features_dir: str = "data/features/daily",
    use_adj_close: bool = True,
    require_min_rows: int = 100,
) -> Dict[str, pd.DataFrame]:
    """Load Phase 1 daily features from data/features/daily/* and filter by date range.

    Returns a Dict[symbol, DataFrame] with the same shape contract as
    generate_synthetic_bars(): DatetimeIndex + `timestamp` column + all 18
    feature cols. The DataFrame's `close` column is `adj_close` by default
    (split- and dividend-adjusted) which is the correct input for Chronos.

    Bars are aligned to the **intersection** of available dates across symbols
    so the backtest loop can iterate over a shared calendar.
    """
    print(f"[real-data] Loading bars for {len(symbols)} symbols from {features_dir}")
    print(f"[real-data]   date range: {start} → {end}")
    frames: Dict[str, pd.DataFrame] = {}
    skipped: List[str] = []
    for sym in symbols:
        path = Path(features_dir) / f"{sym}.parquet"
        if not path.exists():
            print(f"[real-data]   skip {sym}: file not found at {path}")
            skipped.append(sym)
            continue
        df = pd.read_parquet(path)
        # Make sure timestamp is tz-naive UTC for consistent alignment with synthetic
        # generator's `pd.date_range(freq="D")` which is naive. (Phase 1 features are
        # US/Eastern tz-aware.)
        ts = pd.to_datetime(df["timestamp"])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
        df = df.assign(timestamp=ts).set_index("timestamp")
        df.index.name = "timestamp"
        # The orchestrator's call site reads `bars[["timestamp", "close", "atr_14"]]`,
        # so we need `timestamp` to be both the index name AND a column (matches the
        # synthetic generator contract).
        df["timestamp"] = df.index
        # Replace close with adj_close if requested (chronos expects continuous series)
        if use_adj_close and "adj_close" in df.columns:
            df["close"] = df["adj_close"]
        # Filter to requested window
        df = df.loc[start:end]
        if len(df) < require_min_rows:
            print(f"[real-data]   skip {sym}: only {len(df)} rows in range (need ≥{require_min_rows})")
            skipped.append(sym)
            continue
        frames[sym] = df

    if not frames:
        raise RuntimeError(
            f"No real bars loaded for symbols={symbols} from {features_dir}. "
            f"Skipped: {skipped}"
        )

    # Align to intersection of all symbols' calendars
    common_idx = None
    for sym, df in frames.items():
        idx = df.index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    common_idx = common_idx.sort_values()
    print(f"[real-data] common calendar: {len(common_idx)} trading days "
          f"({common_idx[0].date()} → {common_idx[-1].date()})")
    for sym in list(frames.keys()):
        frames[sym] = frames[sym].reindex(common_idx)
    return frames


def build_gatekeeper_fn(redis_client, limits: V12RiskLimits):
    """Exact same logic as the live entry window (R-05/R-06 + V12 sizing, confidence=None)."""
    def _gk(
        symbol: str,
        *,
        confidence: float,
        side: str,
        price: float,
        atr_14: Optional[float] = None,
        atr_pct: Optional[float] = None,
        regime: str = "BULL_LONG",
        **kwargs: Any,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        sym = symbol.upper()
        # R-05
        try:
            capped, r05 = check_daily_trade_cap(redis_client, limits)
            if capped:
                return False, "R05_DAILY_CAP", {"size_pct": 0.0, "risk_rule": "R-05"}
        except Exception:
            pass
        # R-06
        try:
            in_cd, r06 = check_symbol_cooldown(redis_client, sym)
            if in_cd:
                return False, "R06_COOLDOWN", {"size_pct": 0.0, "risk_rule": "R-06"}
        except Exception:
            pass

        size, _, audit = compute_position_size(
            equity=100_000.0,
            price=price,
            atr_pct=atr_pct,
            atr_14=atr_14,
            regime=regime or "BULL_LONG",
            confidence=None,  # critical: never let confidence into core formula
            kelly_override=None,
            risk_scaling_override=None,
            is_degraded=False,
            limits=limits,
        )
        approved = size > 0
        reason = "OK" if approved else "SIZE_ZERO"
        meta = {"size_pct": float(size), **audit, "sizing_model": "v12_risk"}
        return approved, reason, meta

    return _gk


def run_v12_paper_backtest(
    symbols: List[str],
    start: date,
    end: date,
    *,
    risk_per_trade_pct: float = 0.01,
    slippage_pct: float = 0.0005,   # 5bp round-trip rough
    commission_pct: float = 0.0002,
    use_real_forecast: bool = False,
    bars_source: str = "real",         # "real" (Phase 1 features) or "synthetic"
    features_dir: str = "data/features/daily",
    use_adj_close: bool = True,
    forecast_threshold: Optional[float] = None,  # forwarded to ChronosService via env var
    seed: int = 42,
    degraded_mode: bool = False,      # True: zero-expectation demo backend (true no-signal baseline)
    sr_min_expected_return: float = 0.005,  # SR-01 threshold; align with forecast_threshold by default
    sr_use_abs_return: bool = True,  # If True, treat abs(expected_return_pct) for SR-01 candidacy
                                      # (lets SHORT signals enter as LONG; fair comparison across models)
    sr_max_uncertainty: float = 0.15, # SR-02 threshold; chronos-bolt uncertainty bands are ~20%+,
                                       # so default to 0.15 (15%) instead of the strict 0.08 (8%).
    batch_forecast: bool = False,    # If True, batch all symbols per day into one chronos predict()
                                      # (faster but uncertainty bands may be wider → fewer entries)
    # ---- Wave 9: YOLO no-trade filter ----
    yolo_no_trade_filter: bool = False,
    yolo_model_dir: str = "models/yolo/phase2_v1_gpu",
    yolo_image_dir: str = "data/intraday_clean_5min/phase2/ready_for_yolo_full/images",
    yolo_image_lag_days: int = 1,
    min_rows: int = 100,           # Wave 10: minimum bar rows required per symbol (lower for fast lookback)
    # ---- Wave 10: 5-day rolling window (lookback vs entry-counting) ----
    trade_start: Optional[date] = None,   # only entries whose entry_date is in
    trade_end:   Optional[date] = None,   # [trade_start, trade_end] count toward metrics
                                          # (exits for pre-window positions still managed)
) -> Dict[str, Any]:
    """
    Main harness. Returns metrics + sample trades.

    bars_source:
      - "real":      load Phase 1 daily features from features_dir (default)
      - "synthetic": use generate_synthetic_bars() (plumbing-only validation)

    degraded_mode:
      - True:  use a demo backend with expected_return=0.0 (true no-signal baseline).
               Overrides --use-real-forecast; the Chronos model is not loaded at all.
      - False: normal behavior controlled by --use-real-forecast.
    """
    rng = np.random.default_rng(seed)
    requested_symbols = list(symbols)

    # Propagate forecast threshold to ChronosService via env var (read on construction)
    if forecast_threshold is not None:
        os.environ["CHRONOS_FORECAST_THRESHOLD"] = str(forecast_threshold)
        print(f"[backtest] CHRONOS_FORECAST_THRESHOLD={forecast_threshold}")
    if bars_source == "real":
        bars = load_real_bars(
            symbols, start, end,
            features_dir=features_dir,
            use_adj_close=use_adj_close,
            require_min_rows=min_rows,
        )
    elif bars_source == "synthetic":
        bars = generate_synthetic_bars(symbols, start, end, seed=seed)
    else:
        raise ValueError(f"bars_source must be 'real' or 'synthetic', got {bars_source!r}")

    # Real data loader may skip symbols with insufficient history; only iterate
    # over symbols that actually loaded to avoid KeyError mid-loop.
    symbols = [s for s in symbols if s in bars]
    skipped = sorted(set(requested_symbols) - set(symbols))
    if skipped:
        print(f"[backtest] iterating over {len(symbols)} symbols (skipped: {skipped})")

    fake_r = FakeRedis()
    limits = V12RiskLimits(
        risk_per_trade_pct=risk_per_trade_pct,
        confidence_cap_discount_enabled=False,
    )

    # --- Real stack wiring (identical to live entry window) ---
    entry_guard = EntryEvaluationGuard(fake_r, mode="paper_ibkr")

    # Patch daily_trades_key() so R-05 reads/writes the simulated trading day instead of
    # the real wall-clock date (without this, all 7.5y of backtest days map to "today"
    # and R-05 caps the entire run at 3 trades).
    from stockapp_v12.bridge import gatekeeper as _gk_mod
    _sim_day = {"date": None}
    def _patched_daily_trades_key():
        d = _sim_day["date"]
        return f"risk:daily_trades:{d}" if d else _gk_mod.daily_trades_key()
    _gk_mod.daily_trades_key = _patched_daily_trades_key
    logger.info("[backtest] Patched daily_trades_key() to use simulated trading date")

    if degraded_mode:
        # True baseline: zero-expectation demo backend. Chronos model is NOT loaded.
        forecast_service = ForecastService(backend=DemoForecastBackend(expected_return=0.0, uncertainty=0.0))
        logger.info("[backtest] DEGRADED mode: zero-expectation demo backend (no Chronos)")
    elif use_real_forecast:
        forecast_service = ForecastService()  # will degrade if no model
        logger.info("[backtest] REAL forecast mode (ChronosService via env CHRONOS_MODEL_PATH)")
    else:
        # Demo backend produces realistic positive forecasts so we can exercise the full path
        forecast_service = ForecastService(backend=DemoForecastBackend(expected_return=0.028, uncertainty=0.018))
        logger.info("[backtest] DEMO backend mode (expected_return=0.028)")

    sr_engine = SignalRuleEngine(config=SignalRuleConfig(
        min_expected_return_pct=sr_min_expected_return,
        max_uncertainty_pct=sr_max_uncertainty,
    ))
    if sr_use_abs_return:
        # Patch SR-01 to use abs(expected_return_pct) for fair comparison across model variants.
        # This is a backtest-only override that does not modify V12 production code.
        from stockapp_v12.services.signal_rule_engine import SignalRuleEngine as _SRE
        _orig_eval = _SRE.evaluate
        def _patched_eval(self, forecast, ctx):
            cfg = self.config
            er = abs(forecast.expected_return_pct)
            if er < cfg.min_expected_return_pct:
                from stockapp_v12.services.signal_rule_engine import (
                    SignalRuleResult, SR_MIN_EXPECTED_RETURN,
                )
                return SignalRuleResult(
                    proceed=False,
                    rule_code=SR_MIN_EXPECTED_RETURN,
                    reason=f"|expected_return| {er:.4f} < {cfg.min_expected_return_pct}",
                    forecast=forecast,
                )
            return _orig_eval(self, forecast, ctx)
        _SRE.evaluate = _patched_eval
        sr_engine = _SRE(config=SignalRuleConfig(
            min_expected_return_pct=sr_min_expected_return,
            max_uncertainty_pct=sr_max_uncertainty,
        ))
    logger.info(f"[backtest] SR-01 min_expected_return_pct={sr_min_expected_return} use_abs={sr_use_abs_return} SR-02 max_uncertainty_pct={sr_max_uncertainty}")

    gatekeeper_fn = build_gatekeeper_fn(fake_r, limits)

    paper_broker = PaperBroker()
    bracket_service = BracketOrderService(paper_broker)

    journal = TradeJournal(fake_r)
    position_manager = PositionManager(fake_r, journal=journal, mode="paper_ibkr")

    # ---- Wave 9: YOLO no-trade filter ----
    yolo_filter = None
    if yolo_no_trade_filter:
        try:
            yolo_filter = YoloNoTradeFilter(
                model_dir=Path(yolo_model_dir),
                image_dir=Path(yolo_image_dir),
                image_lag_days=yolo_image_lag_days,
            )
            ok = yolo_filter.is_available()
            if ok:
                logger.info(f"[backtest] YOLO no-trade filter ENABLED (model={yolo_model_dir}, "
                            f"images={yolo_image_dir}, lag={yolo_image_lag_days}d)")
            else:
                logger.warning(f"[backtest] YOLO filter NOT available (model/images missing); "
                               f"running as if --yolo-no-trade-filter off (degraded soft-fail)")
                yolo_filter = None
        except Exception as e:
            logger.error(f"[backtest] YOLO filter init failed: {e!r}; running without filter")
            yolo_filter = None
    else:
        logger.info("[backtest] YOLO no-trade filter DISABLED (SR-06 not active)")

    orch = EntryOrchestrator(
        entry_guard=entry_guard,
        forecast_service=forecast_service,
        sr_engine=sr_engine,
        analyst_fn=None,
        gatekeeper_fn=gatekeeper_fn,
        bracket_service=bracket_service,
        position_manager=position_manager,
        journal=journal,
        yolo_filter=yolo_filter,  # Wave 9 — None = filter disabled
    )

    # Optional: pre-computed forecast cache populated by a per-day batched chronos call.
    # Reduces N symbol×day predict() calls to 1 predict() per day (faster) but the
    # chronos-bolt model returns wider uncertainty bands in batched mode, which
    # interacts with SR-02. Enabled via --batch-forecast.
    forecast_cache: Dict[str, Any] = {"day": None, "by_symbol": {}}
    _orig_forecast_daily = forecast_service.forecast_daily
    def _patched_forecast_daily(symbol, df, **kwargs):
        if batch_forecast and forecast_cache["day"] == date_str and symbol in forecast_cache["by_symbol"]:
            cached = forecast_cache["by_symbol"][symbol]
            if cached is not None:
                return cached
        return _orig_forecast_daily(symbol, df, **kwargs)
    forecast_service.forecast_daily = _patched_forecast_daily

    # --- Simulation state ---
    trades: List[BacktestTrade] = []
    open_positions: Dict[str, Dict] = {}   # sym -> {entry_price, sl_price, qty, risk_amount, entry_date}
    daily_new_count: Dict[str, int] = defaultdict(int)
    r05_rejects = 0
    r06_rejects = 0
    sr06_yolo_rejects = 0  # Wave 9: YOLO no-trade filter rejections
    degraded_count = 0
    total_candidates = 0
    skip_reasons: Dict[str, int] = defaultdict(int)
    # Wave 9: track YOLO filter outcomes (only populated when filter is enabled)
    yolo_image_missing = 0
    yolo_no_trade_hits = 0
    yolo_ok = 0

    dates = list(bars[symbols[0]].index)
    equity = [100_000.0]

    # Wave 10: trade window defaults to the data window. If trade_start/trade_end
    # are provided (e.g., for 5-day rolling backtest), only entries whose entry_date
    # falls in [trade_start, trade_end] count toward the metrics. Exits for positions
    # opened before trade_start are still managed.
    eff_trade_start = trade_start if trade_start is not None else start
    eff_trade_end   = trade_end   if trade_end   is not None else end
    print(f"[backtest] data window={start}..{end}  trade window={eff_trade_start}..{eff_trade_end}")

    # Progress logging — emit a heartbeat every ~5% of the simulation
    sim_window = sum(1 for i in range(len(dates)) if 15 <= i < len(dates) - 8)
    progress_step = max(1, sim_window // 20)
    t_start = time.time()

    # Wave 10: collect every trade; we'll filter to [eff_trade_start, eff_trade_end] later.
    # Stores: (trade, entry_dt) so we can decide whether to count it.
    raw_trade_records: List[Tuple[BacktestTrade, date]] = []
    # Also keep a parallel list of ALL trades for the "carry-over" report (so user can see
    # how many positions opened pre-window and are still being managed).
    pre_window_entries: int = 0
    in_window_entries: int = 0
    carry_over_open_at_window_start: int = 0

    for i, dt in enumerate(dates):
        if i < 15 or i >= len(dates) - 8:
            continue
        # Advance the virtual clock so setex-based TTLs (e.g. R-06 cooldown) can expire.
        fake_r.tick()
        # Set sim-day so the R-05 daily-trade key is per-simulated-day, not per-wall-clock-day.
        _sim_day["date"] = dt.date().isoformat()
        date_str = str(dt.date())
        # Wave 10: trade window check. When the trade window sits at the tail of the data
        # window (the common 5-day rolling backtest layout), the [i >= len(dates) - 8] skip
        # above would drop every day in the trade window. To support tail-anchored trade
        # windows, the caller MUST extend --end at least 8 trading days past --trade-end;
        # the data_window in the result makes this explicit. If the trade window is fully
        # contained in the first len(dates)-15 days (i.e., end - trade_end >= 8 trading
        # days), in_trade_window is just a no-op for our purposes.
        in_trade_window = (eff_trade_start <= dt.date() <= eff_trade_end)
        day_idx = sum(1 for k in range(i + 1) if 15 <= k < len(dates) - 8)
        if day_idx % progress_step == 0 or day_idx == sim_window:
            elapsed = time.time() - t_start
            rate = day_idx / max(elapsed, 0.001)
            eta = (sim_window - day_idx) / max(rate, 0.001)
            print(f"[backtest] {day_idx:>5}/{sim_window}  {date_str}  "
                  f"elapsed={elapsed:6.1f}s  rate={rate:5.1f}d/s  eta={eta:6.1f}s  "
                  f"trades_so_far={len(trades)}  candidates={total_candidates}",
                  flush=True)

        # Simulate "actionable today" — for fidelity we let risk gates do the filtering.
        # The historical `PreMarketReview` Redis-key writer is removed (hygiene-A/I);
        # in this offline harness we treat the full daily symbol set as the
        # candidate pool and let SR-01..SR-07 + R-05/R-06 do the actual filtering.
        actionable = set(symbols)

        for sym in symbols:
            if sym not in actionable:
                continue

            total_candidates += 1
            df_slice = bars[sym].iloc[:i+1].copy()
            if len(df_slice) < 10:
                continue

        # Optional batched chronos inference (one predict() for all symbols this day).
        if batch_forecast and use_real_forecast and not degraded_mode and hasattr(forecast_service, "forecast_daily_batch"):
            day_frames = {}
            for sym in symbols:
                if sym not in bars:
                    continue
                slc = bars[sym].iloc[:i+1].copy()
                if len(slc) < 10:
                    continue
                day_frames[sym] = slc
            if day_frames:
                try:
                    forecast_cache["day"] = date_str
                    forecast_cache["by_symbol"] = forecast_service.forecast_daily_batch(
                        day_frames, as_of=dt.date()
                    )
                except Exception as e:
                    logger.warning(f"Batch forecast failed at {date_str}: {e}")
                    forecast_cache["day"] = date_str
                    forecast_cache["by_symbol"] = {}

        # Per-symbol loop: actual entry evaluation (uses forecast cache if batch_forecast)
        for sym in symbols:
            if sym not in actionable:
                continue
            # Wave 10: allow entries everywhere so the strategy can warm up; we'll
            # only count trades whose entry_date falls in [eff_trade_start, eff_trade_end].
            total_candidates += 1
            df_slice = bars[sym].iloc[:i+1].copy()
            if len(df_slice) < 10:
                continue

            # Build realistic context (bars must include the full Phase 1 feature schema
            # so the trained Chronos model's TimeSeriesDataFrame validation passes
            # at inference time. Synthetic bars above already carry stubs for all
            # feature columns).
            feature_cols = [c for c in df_slice.columns if c != "timestamp"]
            ctx = EntryContext(
                symbol=sym,
                bars=df_slice[["timestamp"] + feature_cols],
                market_regime="BULL_LONG",
                actionable_symbols=actionable,
                equity=equity[-1],
                mode="paper_ibkr",
            )

            res = orch.evaluate_and_maybe_enter(ctx, as_of=dt.date())

            if getattr(res, "forecast", None) and getattr(res.forecast, "degraded", False):
                degraded_count += 1

            # Wave 9: tally YOLO outcomes for non-PASS paths too (BUYs that passed the filter)
            if yolo_filter is not None and getattr(res, "yolo_decision", None) is not None:
                yd = res.yolo_decision
                if res.decision == "BUY":
                    # BUY reached → filter must have allowed it (otherwise it'd be PASS/SR-06)
                    if yd.degraded:
                        yolo_image_missing += 1
                    else:
                        yolo_ok += 1

            if res.decision != "BUY":
                if "R05" in (res.reason_code or ""):
                    r05_rejects += 1
                if "R06" in (res.reason_code or ""):
                    r06_rejects += 1
                if "SR-06" in (res.reason_code or ""):
                    sr06_yolo_rejects += 1
                skip_reasons[res.reason_code or "UNKNOWN"] += 1
                # Wave 9: Tally YOLO outcomes from yolo_decision attached to EntryResult
                if getattr(res, "yolo_decision", None) is not None:
                    yd = res.yolo_decision
                    if yd.no_trade:
                        yolo_no_trade_hits += 1
                    elif yd.degraded:
                        yolo_image_missing += 1
                    else:
                        yolo_ok += 1
                continue

            # BUY path - record using data from orchestrator result
            entry_price = float(df_slice.iloc[-1]["close"])
            size_pct = float(getattr(res, "size_pct", 0.0) or 0.0)
            qty = int(getattr(res, "qty", 1) or 1)
            risk_amount = size_pct * equity[-1]   # approx
            sl_price = getattr(res, "sl_price", entry_price * 0.97) or (entry_price * 0.97)

            # Simulate bracket fill (PaperBroker already did inside)
            open_positions[sym] = {
                "entry_date": date_str,
                "entry_dt": dt.date(),
                "entry_price": entry_price,
                "sl_price": sl_price,
                "qty": qty,
                "risk_amount": risk_amount,
            }
            daily_new_count[date_str] += 1

            # 3.7b fix: also increment FakeRedis R-05 daily-trade counter so the gatekeeper
            # actually enforces max_daily_trades=3 in backtest. Without this, R-05 silently
            # passes (counter never incremented → always 0 → cap never triggered), producing
            # max_daily_new=19 in P0 (3.7) and exposing -23% max drawdown.
            # Mirror src/services/lifecycle_scheduler.py line 751 (V11/V12 live path).
            try:
                increment_daily_trade_count(fake_r)
            except Exception as _r05e:
                logger.warning(f"[backtest] R-05 increment failed for {sym}: {_r05e}")

            # Wave 10: tag whether this entry falls in the trade window
            if in_trade_window:
                open_positions[sym]["_in_trade_window"] = True
                in_window_entries += 1
            else:
                open_positions[sym]["_in_trade_window"] = False
                pre_window_entries += 1
            # Record a journal-like trade start (the real journal was already appended inside)
            # We will close it later

        # --- Advance open positions: simulate bracket exits (SL hits) or time exit for demo ---
        to_close = []
        for sym, pos in list(open_positions.items()):
            today_bar = bars[sym].iloc[i]
            today_low = float(today_bar["close"]) * (1 - 0.001)
            # Mix of SL hit or time-based exit (demo only)
            days_held = i - next((idx for idx, d in enumerate(dates) if str(d.date()) == pos["entry_date"]), i)
            if today_low <= pos["sl_price"] or (days_held >= 5 and rng.random() < 0.45):
                exit_price = pos["entry_price"] * (1 + rng.normal(0.008, 0.025)) * (1 - slippage_pct - commission_pct)
                pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
                pnl_dollars = pnl_pct * equity[-1] * (pos["qty"] * pos["entry_price"] / equity[-1])
                risk_r = (pnl_dollars / pos["risk_amount"]) if pos["risk_amount"] > 0 else 0.0

                trades.append(BacktestTrade(
                    symbol=sym,
                    entry_date=pos["entry_date"],
                    exit_date=date_str,
                    entry_price=round(pos["entry_price"], 2),
                    exit_price=round(exit_price, 2),
                    pnl_pct=round(pnl_pct * 100, 2),
                    pnl_r=round(risk_r, 3),
                    hold_days=days_held,
                    size_pct=pos.get("size_pct", 0.03),
                    risk_amount=round(pos["risk_amount"], 2),
                    reason_code="SL_HIT" if today_low <= pos["sl_price"] else "TIME_EXIT",
                    sl_price=round(pos["sl_price"], 2),
                    entry_dt=pos.get("entry_dt"),
                ))
                equity.append(equity[-1] + pnl_dollars)
                to_close.append(sym)

        for sym in to_close:
            open_positions.pop(sym, None)
            # Sync with PositionManager: the entry orchestrator registered this symbol in
            # PM's SADD index when it filled. Without this call, has_open_position() keeps
            # returning True forever and INV-2_OPEN_POSITION blocks every subsequent entry
            # (this is what was producing only 1 trade per symbol = 50 total).
            # close_for_review() also sets the R-06 symbol cooldown, which is correct behavior.
            try:
                position_manager.close_for_review(sym, reason="backtest_exit")
            except Exception as e:
                logger.warning(f"PM close_for_review({sym}) failed: {e}")

    # --- Compute metrics ---
    # Wave 10: filter trades to those whose entry_date is in [eff_trade_start, eff_trade_end].
    # Pre-window trades are kept separately for diagnostics but not counted.
    in_window_trades = [t for t in trades if eff_trade_start <= t.entry_dt <= eff_trade_end] \
        if hasattr(trades[0], "entry_dt") and trades else []
    # Compute carry-over open count: positions that opened pre-window but were still open
    # at eff_trade_start.
    carry_over_open_at_window_start = sum(
        1 for sym, pos in open_positions.items()
        if not pos.get("_in_trade_window", True) and pos.get("entry_dt") and pos["entry_dt"] < eff_trade_start
    )
    if not in_window_trades:
        return {
            "error": "no trades generated in trade window - try enabling demo forecast, longer period, or wider trade window",
            "total_candidates": total_candidates,
            "degraded_forecasts": degraded_count,
            "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda x: -x[1])),
            "data_window": f"{start.isoformat()}..{end.isoformat()}",
            "trade_window": f"{eff_trade_start.isoformat()}..{eff_trade_end.isoformat()}",
            "in_window_entries": 0,
            "pre_window_entries": pre_window_entries,
            "carry_over_open_at_window_start": carry_over_open_at_window_start,
            "symbols_evaluated": len(symbols),
        }
    # Alias so the rest of the metrics pipeline uses the in-window subset
    trades = in_window_trades

    pnls_r = np.array([t.pnl_r for t in trades])
    expectancy = pnls_r.mean()
    win_rate = (pnls_r > 0).mean()
    pf = (pnls_r[pnls_r > 0].sum() / -pnls_r[pnls_r < 0].sum()) if (pnls_r < 0).any() else 999.0

    # Wave 10: use trade window for turnover/Sharpe scaling (matches the window
    # whose metrics we're actually reporting), not the data window.
    days = (eff_trade_end - eff_trade_start).days + 1
    turnover = (len(trades) * 2) / (days / 365.25) if days > 0 else 0.0

    # Wave 10: daily new distribution limited to in-window dates
    in_window_dates = {d.isoformat() for d in
                       [date.fromordinal(o) for o in range(eff_trade_start.toordinal(),
                                                           eff_trade_end.toordinal() + 1)]}
    in_window_daily_counts = [cnt for d, cnt in daily_new_count.items() if d in in_window_dates]
    max_daily = max(in_window_daily_counts) if in_window_daily_counts else 0
    days_le3 = sum(1 for c in in_window_daily_counts if c <= 3)
    pct_le3 = days_le3 / max(1, len(in_window_daily_counts))

    # Equity curve based max DD (simplified)
    eq = np.array([100.0])
    for t in trades:
        eq = np.append(eq, eq[-1] * (1 + t.pnl_r * 0.01))  # rough
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min() * 100

    avg_hold = np.mean([t.hold_days for t in trades])

    # Sharpe ratio (annualized) — use trade-level returns as a proxy
    # Simple approach: trade_pnl_pct is the realized return per trade; assume
    # trades are spaced roughly uniformly over the test window.
    pnls_pct = np.array([t.pnl_pct for t in trades])
    if len(pnls_pct) >= 2 and pnls_pct.std() > 0:
        sharpe_per_trade = pnls_pct.mean() / pnls_pct.std()
        trades_per_year = len(trades) / (days / 365.25) if days > 0 else 1.0
        sharpe_annualized = sharpe_per_trade * np.sqrt(max(trades_per_year, 1))
    else:
        sharpe_annualized = 0.0

    # Per-year breakdown of trade count, win rate, mean pnl_pct
    per_year: Dict[str, Dict[str, float]] = {}
    for t in trades:
        yr = t.entry_date[:4]
        slot = per_year.setdefault(yr, {"trades": 0, "wins": 0, "sum_pnl_pct": 0.0})
        slot["trades"] += 1
        if t.pnl_pct > 0:
            slot["wins"] += 1
        slot["sum_pnl_pct"] += t.pnl_pct
    per_year_summary = {
        yr: {
            "trades": int(s["trades"]),
            "win_rate": round(s["wins"] / s["trades"], 3) if s["trades"] else 0.0,
            "mean_pnl_pct": round(s["sum_pnl_pct"] / s["trades"], 2) if s["trades"] else 0.0,
        }
        for yr, s in sorted(per_year.items())
    }

    # Per-symbol hit rate
    per_symbol: Dict[str, Dict[str, float]] = {}
    for t in trades:
        slot = per_symbol.setdefault(t.symbol, {"trades": 0, "wins": 0, "sum_pnl_pct": 0.0})
        slot["trades"] += 1
        if t.pnl_pct > 0:
            slot["wins"] += 1
        slot["sum_pnl_pct"] += t.pnl_pct
    per_symbol_summary = {
        sym: {
            "trades": int(s["trades"]),
            "win_rate": round(s["wins"] / s["trades"], 3) if s["trades"] else 0.0,
            "mean_pnl_pct": round(s["sum_pnl_pct"] / s["trades"], 2) if s["trades"] else 0.0,
        }
        for sym, s in sorted(per_symbol.items(), key=lambda kv: -kv[1]["trades"])
    }

    result = {
        # Wave 10: window metadata + warmup diagnostics (for 5-day rolling backtests)
        "data_window": f"{start.isoformat()}..{end.isoformat()}",
        "trade_window": f"{eff_trade_start.isoformat()}..{eff_trade_end.isoformat()}",
        "window_trade_count": len(trades),
        "pre_window_entries": pre_window_entries,
        "in_window_entries": in_window_entries,
        "carry_over_open_at_window_start": carry_over_open_at_window_start,
        "overall": {
            "total_trades": len(trades),
            "win_rate": round(win_rate, 3),
            "expectancy_r_after_costs": round(expectancy, 3),
            "profit_factor": round(pf, 2),
            "avg_hold_days": round(avg_hold, 1),
            "max_drawdown_pct": round(mdd, 2),
            "approx_annual_turnover_x": round(turnover, 1),
            "sharpe_annualized_proxy": round(sharpe_annualized, 2),
        },
        "per_year": per_year_summary,
        "per_symbol": per_symbol_summary,
        "risk_gates": {
            "r05_rejects": r05_rejects,
            "r06_rejects": r06_rejects,
            "max_daily_new": max_daily,
            "pct_days_with_new_le3": round(pct_le3, 3),
        },
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda x: -x[1])),
        "forecast_quality": {
            "degraded_forecast_ratio": round(degraded_count / max(1, total_candidates), 3),
        },
        # Wave 9: YOLO filter attribution
        "yolo_filter": {
            "enabled": yolo_filter is not None,
            "model_dir": str(yolo_model_dir) if yolo_filter is not None else None,
            "image_dir": str(yolo_image_dir) if yolo_filter is not None else None,
            "image_lag_days": yolo_image_lag_days if yolo_filter is not None else None,
            "sr06_yolo_rejects": sr06_yolo_rejects,
            "yolo_image_missing_or_degraded": yolo_image_missing,
            "yolo_ok": yolo_ok,
            "yolo_no_trade_hits": yolo_no_trade_hits,
            "yolo_block_rate": round(
                sr06_yolo_rejects / max(1, sr06_yolo_rejects + yolo_ok + yolo_image_missing), 3
            ),
        },
        "sample_trades": [asdict(t) for t in trades[:6]],
        "note": "High-fidelity reuse of V12 components. Real data + real Chronos model will replace synthetic + demo backend.",
    }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V12 Paper Path Backtest Harness")
    parser.add_argument("--symbols", default="NVDA,AAPL,TSLA,AMD")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-01-01")
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--use-real-forecast", action="store_true",
                        help="Use real ForecastService (will degrade without model)")
    parser.add_argument("--bars-source", choices=["real", "synthetic"], default="real",
                        help="real: Phase 1 daily features (default); synthetic: random walk")
    parser.add_argument("--features-dir", default="data/features/daily",
                        help="Path to daily features parquet directory (when bars-source=real)")
    parser.add_argument("--no-adj-close", action="store_true",
                        help="Use raw close instead of adj_close (when bars-source=real)")
    parser.add_argument("--forecast-threshold", type=float, default=None,
                        help="Override ChronosService threshold (e.g., 0.005 = 0.5%% over horizon). "
                             "Default = CHRONOS_FORECAST_THRESHOLD env var or 0.01 (1%%).")
    parser.add_argument("--out", default=None,
                        help="Optional path to write JSON results (in addition to stdout)")
    parser.add_argument("--degraded", action="store_true",
                        help="Degraded baseline mode: zero-expectation demo backend, no Chronos loaded. "
                             "Overrides --use-real-forecast. Used to compare to demo/real modes.")
    parser.add_argument("--sr-min-expected-return", type=float, default=0.005,
                        help="SR-01 min_expected_return_pct (default 0.005). Should align with "
                             "--forecast-threshold so signals can flow through.")
    parser.add_argument("--batch-forecast", action="store_true",
                        help="Batched chronos: one predict() call per day for all symbols. "
                             "Faster but may yield wider uncertainty bands → fewer entries.")
    parser.add_argument("--sr-max-uncertainty", type=float, default=0.15,
                        help="SR-02 max_uncertainty_pct (default 0.15). Chronos-bolt uncertainty "
                             "bands are wide (~20%%); default relaxes the strict 0.08 production value.")
    # ---- Wave 9: YOLO no-trade filter flags ----
    parser.add_argument("--yolo-no-trade-filter", action="store_true",
                        help="Enable YOLO phase2_v1_gpu no-trade filter (SR-06). High-confidence "
                             "chop/normal predictions suppress entry. Soft-fails to disabled if "
                             "model or images are missing.")
    parser.add_argument("--yolo-model-dir", default="models/yolo/phase2_v1_gpu",
                        help="Path to YOLO model directory (must contain best.pt + threshold_tuned.json).")
    parser.add_argument("--yolo-image-dir",
                        default="data/intraday_clean_5min/phase2/ready_for_yolo_full/images",
                        help="Path to 5min chart images directory.")
    parser.add_argument("--yolo-image-lag-days", type=int, default=1,
                        help="Number of trading days to look back for the chart image "
                             "(default 1 = use T-1's chart to decide T's entry).")
    parser.add_argument("--min-rows", type=int, default=100,
                        help="Minimum bar rows required per symbol (default 100). "
                             "Lower to e.g. 50 for fast 5-day rolling lookback (Chronos-bolt "
                             "context is 2048; indicators like MACD need ~30).")
    # ---- Wave 10: trade window (separate from data window) ----
    parser.add_argument("--trade-start", default=None,
                        help="If set, only entries with entry_date >= this count toward metrics. "
                             "Default = --start. Strategy still runs over [start, end] for warmup.")
    parser.add_argument("--trade-end", default=None,
                        help="If set, only entries with entry_date <= this count toward metrics. "
                             "Default = --end.")
    args = parser.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",")]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print(f"[backtest] mode=paper  symbols={len(syms)}  "
          f"window={start}..{end}  bars_source={args.bars_source}  "
          f"real_forecast={args.use_real_forecast}  degraded={args.degraded}")

    result = run_v12_paper_backtest(
        syms, start, end,
        risk_per_trade_pct=args.risk_pct,
        use_real_forecast=args.use_real_forecast,
        bars_source=args.bars_source,
        features_dir=args.features_dir,
        use_adj_close=not args.no_adj_close,
        forecast_threshold=args.forecast_threshold,
        degraded_mode=args.degraded,
        sr_min_expected_return=args.sr_min_expected_return,
        batch_forecast=args.batch_forecast,
        sr_max_uncertainty=args.sr_max_uncertainty,
        # Wave 9
        yolo_no_trade_filter=args.yolo_no_trade_filter,
        yolo_model_dir=args.yolo_model_dir,
        yolo_image_dir=args.yolo_image_dir,
        yolo_image_lag_days=args.yolo_image_lag_days,
        # Wave 10
        min_rows=args.min_rows,
        trade_start=date.fromisoformat(args.trade_start) if args.trade_start else None,
        trade_end=date.fromisoformat(args.trade_end) if args.trade_end else None,
    )

    text = json.dumps(result, indent=2, default=str)
    print(text)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text)
        print(f"[backtest] wrote results to {out_path}")
