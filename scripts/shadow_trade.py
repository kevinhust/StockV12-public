#!/usr/bin/env python3
"""
shadow_trade.py — simplified daily-prediction harness for ADR-007 P3 + shadow path.

Why this exists (vs eval_chronos_h2h_v3.py):
  H2H eval is a one-shot batch: for each (model, cutoff) it runs every symbol
  once and aggregates. Shadow-trade simulates the live daily loop instead —
  iterate eval_dates from today-N back through today-1 (walk-forward), and at
  each date predict 10 horizons and compare to next-10-days actual closes.

  The two questions we actually need answered:
    1. Does E05_p3_coldstart's hit_rate survive outside the single
       cutoff=10d OOS test? (walk-forward: hit_rate stability)
    2. Is the directional signal tradable? (simulated P&L using
       forecast_sign × actual_ret as a proxy trade)

Outputs:
  - Per-model per-date JSON to models/chronos/_shadow/{model}/{YYYY-MM-DD}.json
  - Per-model summary: models/chronos/_shadow/SUMMARY.json (hit_rate, signal_hit_rate,
    mase, simulated_pnl_pct, n_trades, win_rate)
  - Console table sorted by simulated_pnl_pct DESC

Usage:
  # Walk-forward backtest on historical 7-day window (no live data needed)
  PYTHONPATH=src:scripts:. python3 scripts/shadow_trade.py \\
      --models phase2_v2 E05_p3_coldstart_seed42 \\
      --walk-days 7 \\
      --symbols AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY \\
      --out models/chronos/_shadow/SUMMARY.json

  # Predict using "today" as eval_date (live mode)
  PYTHONPATH=src:scripts:. python3 scripts/shadow_trade.py \\
      --models phase2_v2 E05_p3_coldstart_seed42 \\
      --predict-today \\
      --symbols AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY

Design notes:
  - No Kafka, MLflow, Grafana, paper-broker hooks. This is intentionally
    boring: load predictor → predict → write JSON → done. The whole point
    of ADR-007 is to remove the OOS-evaluation blind spot before adding
    back the infrastructure that hid it.
  - Walk-forward cutoffs are N=1..N=walk_days, where N=1 means "predict
    using data ending yesterday" (most recent), N=walk_days means "predict
    using data ending 7 days ago". This mimics a real daily loop where
    eval_date shifts forward by 1 each day.
  - Forecast horizon = 10 days, matched to the H2H eval convention.
  - Returns: forecast_ret_pct = (forecast_mean - current_close) / current_close
    × 100, averaged over the first 10 horizons. Same formula as H2H.
  - Simulated P&L: long if forecast > threshold, short if forecast < -threshold,
    flat otherwise. Per-trade pnl_pct = sign(forecast) × actual_ret_pct.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

# Repo paths (matches eval_chronos_h2h_v3.py)
# Must be on sys.path before `from scripts.*` — cron runs
# `python scripts/shadow_trade.py`, which puts scripts/ on path[0] but not
# the package parent unless PYTHONPATH includes repo root.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
_SRC = REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

SHADOW_ROOT = REPO / "models" / "chronos" / "_shadow"

# Reuse everything that already works: AutoGluon loading, eval helpers,
# sign / hit_rate / signal_hit_rate / compute_mase. Avoids drift between
# H2H and shadow scripts.
# B2 (post-launch refactor): `load_model` still lives in
# `scripts.eval_chronos_h2h_v3` (it's I/O glue, not a pure helper).
# All pure metrics + the predict function now come from
# `stockapp_v12.eval.*` (the canonical home).
from scripts.eval_chronos_h2h_v3 import load_model
from stockapp_v12.eval import (
    DEFAULT_TEST_SYMBOLS,
    build_test_tsdf_full,
    compute_mase,
    hit_rate,
    naive_errors_from_actual,
    predict_one_symbol_for_date,
    sign,
    signal_hit_rate,
    simulate_pnl,
)


# Trading-edge parameters
DEFAULT_SIGNAL_THRESHOLD_PCT = 0.5   # |forecast_ret_pct| > 0.5% = "we take a position"
FORECAST_HORIZON = 10               # days; matches H2H convention

# Walk-forward longitudinal health check (ADR-008; Gate Arch L2 precheck / D)
# Legacy JSON keys still use promotion_gate* for backward compatibility.
# v2 walkforward baselines — THREE historical measurements exist:
#   (A) Single-date 7-cutoff test (cutoffs [1..7], 10 symbols, 70 OOS points):
#       v2_walkforward_mase    = 3.8177
#       v2_walkforward_pnl_pct = +286.585%   # misleading: 1-day, lucky
#       v2_walkforward_win_rate = 0.4576
#       — eb9b2b62 baseline; NOT used as gate anchor.
#   (B) 30-day walk-forward backfill on 2026-07-01 (commit 820524ca, original
#       30-day run with 30 dates × 10 symbols = 300 rows):
#       v2_walkforward_mase    = 2.0619
#       v2_walkforward_pnl_pct = +55.09%
#       v2_walkforward_win_rate = 0.5000
#       — Original 30-day baseline; was anchor until 2026-07-10 re-run.
#   (C) 30-day walk-forward backfill on 2026-07-10 (Round-3 council re-run
#       after doc/code drift surfaced; 30 dates × 10 symbols = 300 rows,
#       window 2026-05-19..2026-06-29):
#       v2_walkforward_mase    = 2.0172       ← GATE ANCHOR (re-measured)
#       v2_walkforward_pnl_pct = +46.11%
#       v2_walkforward_win_rate = 0.4971
#       — Used by current gate; (B) and (C) differ because the simulator has
#         ~10pp PnL run-to-run variance on the same window (see ADR-009
#         "Correction 2026-07-10" for full discussion).
# Gate is RELATIVE to v2 walkforward baseline (not single-cutoff H2H) because
# walk-forward mixes horizon lengths and produces higher mase than the
# single-cutoff=10d H2H test. The ADR-007 mase≤2.40 gate fails for BOTH
# models in walk-forward — gate was miscalibrated.
# Corrected MASE threshold: 2.0172 × 1.05 = 2.1181 (was wrongly 4.011 in
# pre-2026-07-10 code; then 2.165 in 820524ca). Re-run any time.
WALKFORWARD_V2_MASE = 2.0172        # 30-day aggregate, re-measured 2026-07-10
WALKFORWARD_V2_PNL_PCT = 46.11       # 30-day aggregate, re-measured 2026-07-10 (informational)
WALKFORWARD_V2_WIN_RATE = 0.4971    # 30-day aggregate, re-measured 2026-07-10
DEFAULT_MAX_MASE_DELTA_PCT_VS_WF_V2 = 5.0   # mase ≤ v2_wf × 1.05
DEFAULT_MIN_WIN_RATE = 0.40         # must win ≥40% of trades (random ≈ 50%)
DEFAULT_MIN_PNL_PCT = 0.0           # must be profitable (PnL > 0) — moratorium since Round 4
DEFAULT_PNL_MORATORIUM = True       # Round 4: PnL is informational-only, not a hard gate
DEFAULT_MIN_N_TRADES = 30           # need ≥30 trades for statistical power


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested in v12/tests/test_shadow_trade.py
# ---------------------------------------------------------------------------

def walk_forward_cutoffs(n_days: int) -> list[int]:
    """Return [1, 2, ..., n_days] — i.e. eval_date shifted back 1..n days.

    n_days=7 → [1, 2, 3, 4, 5, 6, 7] = a 7-trading-day walk-forward window.
    cutoff=1 means "predict using data through yesterday" (most recent eval).
    cutoff=7 means "predict using data through 7 days ago" (oldest eval).
    This way aggregate results reflect a "what would we have predicted each
    of the last N days" simulation, which is what we actually need.

    Raises:
      ValueError: if n_days < 1.
    """
    if n_days < 1:
        raise ValueError(f"walk_forward_cutoffs: n_days must be >= 1, got {n_days}")
    return list(range(1, n_days + 1))


# B2 (post-launch refactor): `simulate_pnl` moved to
# `stockapp_v12.eval.backtest_pnl` (imported above). Tests still pass
# `simulate_pnl` as a symbol — re-export it locally to avoid breaking the
# script-level pickle contract for any stale baselines.


def aggregate_shadow_rows(
    rows: list[dict], threshold: float = DEFAULT_SIGNAL_THRESHOLD_PCT
) -> dict:
    """Combine per-symbol rows from one or more eval dates into a model summary.

    Combines hit_rate (all forecast vs actual pairs), signal_hit_rate (only
    symbols where |forecast| > threshold), MASE (mean across rows that have
    mase), and simulated P&L (long/short/flat on each called symbol).

    Rows missing forecast_ret_pct or actual_ret_pct are skipped from
    directional / PnL metrics. Rows missing mase are skipped from MASE.
    """
    # Directional: forecast vs actual
    pairs = [
        (r["forecast_ret_pct"], r["actual_ret_pct"])
        for r in rows
        if r.get("forecast_ret_pct") is not None and r.get("actual_ret_pct") is not None
    ]
    f_pcts = [p[0] for p in pairs]
    a_pcts = [p[1] for p in pairs]

    # MASE
    mase_vals = [r["mase"] for r in rows if r.get("mase") is not None]
    mase_mean = round(float(np.mean(mase_vals)), 4) if mase_vals else None

    # Eval-date coverage
    dates = sorted({r["eval_date"] for r in rows if r.get("eval_date")})
    symbols = sorted({r["symbol"] for r in rows if r.get("symbol")})

    return {
        "n_rows": len(rows),
        "n_pairs": len(pairs),
        "n_dates": len(dates),
        "n_symbols": len(symbols),
        "dates": dates,
        "symbols": symbols,
        "hit_rate": hit_rate(pairs),
        "signal_hit_rate": signal_hit_rate(f_pcts, a_pcts, threshold=threshold),
        "mase_mean": mase_mean,
        "simulated_pnl": simulate_pnl(f_pcts, a_pcts, threshold=threshold),
    }


def eval_date_str_for_cutoff(today: date, cutoff_days_ago: int) -> str:
    """YYYY-MM-DD string for `today - cutoff_days_ago` calendar days.

    Note: 'cutoff_days_ago' here is calendar days back, not trading days.
    For OOS panels with daily features, this is close enough — weekends just
    become 1-2 extra unused rows in the held-out slice.
    """
    return (today - timedelta(days=int(cutoff_days_ago))).isoformat()


# Per-date JSON filename pattern (matches write_shadow_artifacts output)
_PER_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"


def aggregate_all_per_date_jsons(
    model_dir: Path, threshold: float = DEFAULT_SIGNAL_THRESHOLD_PCT,
    source_filter: Optional[str] = None,
) -> dict:
    """Read every per-date JSON in a model's _shadow directory and aggregate.

    Each per-date JSON has shape {"model", "eval_date", "rows": [...]}, where
    each row already contains eval_date + model keys (set by
    run_shadow_for_model). This helper:
      1. Globs `YYYY-MM-DD.json` files in model_dir (live cron data)
      2. Globs `_backfill/YYYY-MM-DD.json` (synthetic backfill data, written
         by backfill_30_days.py; ONE dir deeper so glob stays shallow)
      3. Tags every row with eval_source ("cron" or "backfill") based on
         which subdir it came from
      4. Optionally filters by source_filter (None = include both)
      5. Concatenates remaining rows and delegates to aggregate_shadow_rows()

    Why the source tag exists (Round 2 closure / ADR-008 followup):
      The 30-day promotion gate should ideally be evaluated on live cron
      data alone to avoid look-ahead bias from a synthetic backfill. The
      source tag lets the operator call this function twice — once with
      source_filter="cron" to gate on live-only evidence, then again with
      source_filter=None to see the combined simulated P&L. The two
      numbers are kept separate so Day-30 decision can be made on the
      cron-slice (per Critic voice in 2026-07-01 council) without
      contaminating it with backfill-tagged rows.

    Args:
      model_dir: path to models/chronos/_shadow/{model_name}
      threshold: signal threshold passed through to aggregate_shadow_rows
      source_filter: None (default, both sources) | "cron" | "backfill"

    Returns:
      {"n_dates": int, "date_range": [first, last] | None, "aggregate": {...},
       "n_dates_by_source": {"cron": int, "backfill": int},
       "n_rows_by_source": {"cron": int, "backfill": int}}
      - aggregate: same shape as aggregate_shadow_rows() output (only
        contains rows from sources that passed the filter)
      - n_dates_by_source / n_rows_by_source: full counts regardless of
        filter, so the operator can see "we have 7 cron days and 23
        backfill days" at a glance
    """
    model_dir = Path(model_dir)
    if not model_dir.exists():
        return {
            "n_dates": 0,
            "date_range": None,
            "aggregate": aggregate_shadow_rows([], threshold=threshold),
            "n_dates_by_source": {"cron": 0, "backfill": 0},
            "n_rows_by_source": {"cron": 0, "backfill": 0},
        }

    # (subdir, source_tag) pairs to walk
    sources_to_walk: list[tuple[Path, str]] = [
        (model_dir, "cron"),
        (model_dir / "_backfill", "backfill"),
    ]

    rows_by_source: dict[str, list[dict]] = {"cron": [], "backfill": []}
    dates_by_source: dict[str, set[str]] = {"cron": set(), "backfill": set()}

    for subdir, source_tag in sources_to_walk:
        if not subdir.exists():
            continue
        for path in sorted(subdir.glob(_PER_DATE_GLOB)):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                print(
                    f"[aggregate_all_per_date_jsons] skip {path}: {e}",
                    file=sys.stderr,
                )
                continue
            rows = payload.get("rows") or []
            eval_date = payload.get("eval_date")
            for r in rows:
                r.setdefault("eval_date", eval_date)
                r.setdefault("model", model_dir.name)
                # CRITICAL: tag with source so downstream callers can
                # separate cron vs backfill rows.
                r["eval_source"] = source_tag
            rows_by_source[source_tag].extend(rows)
            if eval_date:
                dates_by_source[source_tag].add(eval_date)

    # Apply source filter
    if source_filter is not None:
        # Replace the targeted source with empty (drop everything else).
        if source_filter in rows_by_source:
            kept_rows = list(rows_by_source[source_filter])
            kept_dates = set(dates_by_source[source_filter])
        else:
            kept_rows = []
            kept_dates = set()
    else:
        kept_rows = rows_by_source["cron"] + rows_by_source["backfill"]
        kept_dates = dates_by_source["cron"] | dates_by_source["backfill"]

    aggregate = aggregate_shadow_rows(kept_rows, threshold=threshold)
    date_range = sorted(kept_dates) if kept_dates else None
    return {
        "n_dates": len(kept_dates),
        "date_range": [date_range[0], date_range[-1]] if date_range else None,
        "aggregate": aggregate,
        "n_dates_by_source": {
            "cron": len(dates_by_source["cron"]),
            "backfill": len(dates_by_source["backfill"]),
        },
        "n_rows_by_source": {
            "cron": len(rows_by_source["cron"]),
            "backfill": len(rows_by_source["backfill"]),
        },
    }


# ---------------------------------------------------------------------------
# Walk-forward longitudinal health (ADR-008; Gate Decision Architecture)
# ---------------------------------------------------------------------------
# Not a decision-path swap (that is L2 evaluate_path_change + human).
# This check only answers: "is this model's accumulated shadow not wild?"
# JSON still emits promotion_gate / promotion_gates for backward compatibility;
# readers should prefer longitudinal_health / longitudinal_health_by_model.
#
# Conditions (relative to v2 walkforward + win_rate floor + sample size):
#   - PnL > 0 is INFORMATIONAL only when pnl_moratorium (Round 4)
#   - mase ≤ v2_wf × 1.05
#   - win_rate ≥ 0.40
#   - n_trades ≥ 30 and n_dates ≥ 30
# hit_rate is intentionally NOT a hard condition here.

DEFAULT_SHADOW_GATE = {
    "min_pnl_pct": DEFAULT_MIN_PNL_PCT,
    "pnl_moratorium": DEFAULT_PNL_MORATORIUM,
    "max_mase_delta_pct_vs_v2_walkforward": DEFAULT_MAX_MASE_DELTA_PCT_VS_WF_V2,
    "v2_walkforward_mase": WALKFORWARD_V2_MASE,
    "min_win_rate": DEFAULT_MIN_WIN_RATE,
    "min_n_trades": DEFAULT_MIN_N_TRADES,
}


def check_shadow_gate(aggregate: dict, gate: Optional[dict] = None) -> dict:
    """Longitudinal health (ADR-008) — not L2 path-change / not auto-promote.

    Args:
      aggregate: dict from aggregate_shadow_rows (has 'mase_mean',
                 'simulated_pnl' sub-dict with 'total_pnl_pct' / 'win_rate' /
                 'n_trades', 'n_pairs', etc.).
      gate: optional overrides; defaults to DEFAULT_SHADOW_GATE.

    Returns:
      {"cleared": bool, "reasons": list[str], "kind": "longitudinal_health"}

    Path swaps use scripts/evaluate_path_change.py (L2) + human approval.
    """
    g = gate or DEFAULT_SHADOW_GATE
    reasons: list[str] = []
    pnl_obj = aggregate.get("simulated_pnl") or {}
    pnl = pnl_obj.get("total_pnl_pct")
    win_rate = pnl_obj.get("win_rate")
    n_trades = pnl_obj.get("n_trades", 0) or 0
    mase = aggregate.get("mase_mean")
    # n_dates is the count of DISTINCT eval_dates in the aggregate
    # (aggregate_shadow_rows builds `dates` via set-comprehension).
    # Catches the Bug #1 class: if eval_date slicing is wrong, all 30
    # "different dates" collapse to 1 — gate would pass n_trades=30
    # on 30 identical rows without this check.
    n_dates = aggregate.get("n_dates", 0) or 0

    # 1. Must be profitable — BUT Round 4 moratorium: PnL is informational-only.
    #    The check still runs and is reported in `pnl_note`, but it does NOT
    #    add to `reasons` (which determines pass/fail) when moratorium is active.
    min_pnl = g.get("min_pnl_pct", DEFAULT_MIN_PNL_PCT)
    pnl_moratorium = g.get("pnl_moratorium", DEFAULT_PNL_MORATORIUM)
    pnl_note = ""
    if pnl is not None and pnl <= min_pnl:
        pnl_note = (
            f"simulated_pnl_pct {pnl} <= {min_pnl} "
            f"(informational{' — moratorium active' if pnl_moratorium else ''})"
        )
        if not pnl_moratorium:
            reasons.append(
                f"simulated_pnl_pct {pnl} <= {min_pnl} (must be profitable)"
            )

    # 2. MASE not wildly worse than v2 walkforward
    v2_mase = g.get("v2_walkforward_mase", WALKFORWARD_V2_MASE)
    max_delta = g.get("max_mase_delta_pct_vs_v2_walkforward",
                      DEFAULT_MAX_MASE_DELTA_PCT_VS_WF_V2)
    if mase is not None:
        threshold = v2_mase * (1.0 + max_delta / 100.0)
        if mase > threshold:
            reasons.append(
                f"mase {mase:.4f} > {threshold:.4f} "
                f"(v2_walkforward × {1 + max_delta / 100:.3f})"
            )

    # 3. Win rate floor
    min_wr = g.get("min_win_rate", DEFAULT_MIN_WIN_RATE)
    if win_rate is not None and win_rate < min_wr:
        reasons.append(
            f"win_rate {win_rate:.4f} < {min_wr:.2f}"
        )

    # 4. Statistical power (number of trades)
    min_n = g.get("min_n_trades", DEFAULT_MIN_N_TRADES)
    if n_trades < min_n:
        reasons.append(
            f"n_trades {n_trades} < {min_n} (statistical power)"
        )

    # 5. Distinct eval_dates — Bug #1 class guard. Without this, the
    #    walk-forward backfill can pass n_trades=30 on 30 IDENTICAL
    #    rows (all "different eval_dates" collapse to one real date
    #    when cutoffs are mis-sliced). n_dates is computed as the
    #    set-cardinality of eval_dates inside aggregate_shadow_rows,
    #    so duplication produces a smaller n_dates.
    if n_dates < min_n:
        reasons.append(
            f"n_dates {n_dates} < {min_n} "
            f"(distinct eval_dates — Bug #1 guard; see ADR-009 addendum)"
        )

    result = {
        "cleared": len(reasons) == 0,
        "reasons": reasons,
        "kind": "longitudinal_health",
        "layer_hint": "D_or_L2_precheck",
    }
    if pnl_note:
        result["pnl_note"] = pnl_note
    return result


# ---------------------------------------------------------------------------
# I/O glue — orchestration around AutoGluon (not unit-tested directly)
# ---------------------------------------------------------------------------

# B2 (post-launch refactor): `predict_one_symbol_for_date` moved to
# `stockapp_v12.eval.predict` (imported above). The two scripts
# (shadow_trade + shadow_ensemble) had divergent copies; the unified
# canonical version is shadow_trade's body + the ensemble's
# "missing from data" ValueError guard.


def run_shadow_for_model(
    predictor, model_name: str, symbols: list[str], cutoffs: list[int],
    today: date, threshold: float = DEFAULT_SIGNAL_THRESHOLD_PCT,
    eval_dates: Optional[list[date]] = None,
) -> dict:
    """Run shadow-trade loop for one model across all (cutoff|eval_date, symbol) combos.

    Two modes (eval_dates wins if both provided):

    1. `eval_dates` provided (true walk-forward, used by backfill):
       - Iterate over each `eval_date` (a `date` object)
       - Pass `eval_date.isoformat()` to predict_one_symbol_for_date
       - Each (model, symbol, eval_date) tuple gets a distinct data slice

    2. `eval_dates` is None (legacy cron path):
       - Iterate over `cutoffs: list[int]`
       - eval_date label = `today - cutoff days` (decorative; data slice
         varies by cutoff but label doesn't match data — see ADR-009)
       - Pass `cutoff` to predict_one_symbol_for_date

    Returns:
      {"model_name", "today", "per_date": {YYYY-MM-DD: [rows]}, "aggregate": {...}}
    """
    per_date: dict[str, list[dict]] = {}
    all_rows: list[dict] = []
    if eval_dates is not None:
        for ed in eval_dates:
            eval_date_str = ed.isoformat()
            print(f"  [{model_name}] eval_date={eval_date_str} (walk-forward)", file=sys.stderr)
            for sym in symbols:
                row = predict_one_symbol_for_date(
                    predictor, sym, cutoff_days_ago=1, eval_date=eval_date_str,
                )
                if row is None:
                    continue
                row["eval_date"] = eval_date_str
                row["model"] = model_name
                per_date.setdefault(eval_date_str, []).append(row)
                all_rows.append(row)
    else:
        for cutoff in cutoffs:
            # Single-cutoff (--predict-today cron path): eval_date is the
            # decision date (today), not the data cutoff date (today-1).
            # Multi-cutoff (walk-forward backtest): eval_date = today - cutoff,
            # preserving historical backtest semantics. This change makes the
            # daily cron write filename/eval_date == todays date, so n_dates
            # advances by 1 per trading day instead of alternating.
            if len(cutoffs) == 1:
                eval_date = today.isoformat()
            else:
                eval_date = eval_date_str_for_cutoff(today, cutoff)
            print(f"  [{model_name}] cutoff={cutoff}d eval_date={eval_date}", file=sys.stderr)
            for sym in symbols:
                row = predict_one_symbol_for_date(predictor, sym, cutoff)
                if row is None:
                    continue
                row["eval_date"] = eval_date
                row["model"] = model_name
                per_date.setdefault(eval_date, []).append(row)
                all_rows.append(row)
    return {
        "model_name": model_name,
        "today": today.isoformat(),
        "per_date": per_date,
        "aggregate": aggregate_shadow_rows(all_rows, threshold=threshold),
    }


def write_shadow_artifacts(
    result: dict, shadow_root: Path = SHADOW_ROOT,
    threshold: float = DEFAULT_SIGNAL_THRESHOLD_PCT,
) -> list[Path]:
    """Write per-date JSONs and refresh the model-level SUMMARY.

    Idempotent: per-date files are overwritten (same date = same content).
    The per-model SUMMARY.json is rebuilt from ALL per-date JSONs in the
    model directory so it accumulates across daily runs (each cron run only
    adds the new day's eval_date JSON, but the summary reflects every day
    since the shadow loop started).

    Longitudinal health (ADR-008) is applied to the accumulated aggregate,
    not just today's run. This is not an L2 path-change decision.
    """
    written: list[Path] = []
    model_name = result["model_name"]
    model_dir = shadow_root / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    # Per-date JSONs
    for eval_date, rows in result["per_date"].items():
        out = model_dir / f"{eval_date}.json"
        out.write_text(json.dumps({
            "model": model_name,
            "eval_date": eval_date,
            "rows": rows,
        }, indent=2, default=str))
        written.append(out)
    # Per-model SUMMARY (rebuilt from accumulated per-date JSONs so it
    # reflects every day's data, not just today's run).
    accumulated = aggregate_all_per_date_jsons(model_dir, threshold=threshold)
    accumulated_agg = accumulated["aggregate"]
    gate_result = check_shadow_gate(accumulated_agg)
    summary = {
        "model": model_name,
        "today": result["today"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        # Per-run aggregate (today's contribution only) — useful for
        # spotting daily drift without re-running.
        "current_run_aggregate": result["aggregate"],
        # Accumulated across all per-date JSONs in this model_dir.
        "accumulated": {
            "n_dates": accumulated["n_dates"],
            "date_range": accumulated["date_range"],
            "aggregate": accumulated_agg,
        },
        # Longitudinal health on accumulated aggregate (legacy key kept).
        "promotion_gate": gate_result,
        "longitudinal_health": gate_result,
    }
    summary_path = model_dir / "SUMMARY.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    written.append(summary_path)
    return written


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--models", nargs="+", required=False, default=None,
        help="Model dir basenames under models/chronos/ (e.g. phase2_v2 E05_p3_coldstart_seed42). Required unless --status is set.",
    )
    p.add_argument(
        "--symbols", nargs="+", default=DEFAULT_TEST_SYMBOLS,
        help=f"Test symbols (default: {DEFAULT_TEST_SYMBOLS})",
    )
    p.add_argument(
        "--walk-days", type=int, default=7,
        help="Walk-forward window: cutoffs 1..N back from today (default: 7)",
    )
    p.add_argument(
        "--predict-today", action="store_true",
        help=(
            "Single-call mode: only cutoff=1 (predict using data ending "
            "yesterday). No backtest — for the live daily loop. "
            "Mutually exclusive with --walk-days > 1."
        ),
    )
    p.add_argument(
        "--threshold", type=float, default=DEFAULT_SIGNAL_THRESHOLD_PCT,
        help="Signal threshold in % (default: 0.5). |forecast| > threshold => take trade.",
    )
    p.add_argument(
        "--out", type=Path, default=None,
        help=(
            "Output SUMMARY.json path aggregating ALL models (default: "
            "models/chronos/_shadow/SUMMARY.json)"
        ),
    )
    p.add_argument(
        "--today", type=str, default=None,
        help="Override 'today' (YYYY-MM-DD) for reproducibility / backtest. Default: today().",
    )
    p.add_argument(
        "--status", action="store_true",
        help=(
            "Read-only mode: print accumulated status from existing "
            "per-date JSONs in shadow_root. No model loading, no "
            "predictions. Useful for ad-hoc gate checks between cron runs. "
            "Ignores --models, --symbols, --walk-days, --predict-today."
        ),
    )
    args = p.parse_args()

    if args.status:
        cmd_print_status(args.out)
        return

    if not args.models:
        p.error("--models is required (or pass --status for read-only mode)")

    if args.today:
        today = date.fromisoformat(args.today)
    else:
        today = date.today()

    if args.predict_today:
        cutoffs = [1]
    else:
        cutoffs = walk_forward_cutoffs(args.walk_days)

    print(
        f"[config] models={args.models} symbols={len(args.symbols)} "
        f"cutoffs={cutoffs} threshold={args.threshold} today={today}",
        file=sys.stderr,
    )

    chronos_root = REPO / "models" / "chronos"
    predictors: dict[str, object] = {}
    for m in args.models:
        pdir = chronos_root / m
        if not pdir.exists():
            print(f"ERROR: {pdir} does not exist", file=sys.stderr)
            sys.exit(2)
        print(f"[load] {m} from {pdir}", file=sys.stderr)
        predictors[m] = load_model(pdir)

    all_results = []
    for name, predictor in predictors.items():
        print(f"\n[shadow] {name}", file=sys.stderr)
        result = run_shadow_for_model(
            predictor, name, args.symbols, cutoffs, today,
            threshold=args.threshold,
        )
        write_shadow_artifacts(result, SHADOW_ROOT, threshold=args.threshold)
        all_results.append(result)

    # Cross-model summary. Use ACCUMULATED aggregates from per-date JSONs
    # (same logic the per-model SUMMARY.json now uses), so the cross-model
    # view also reflects every day's data, not just today's run.
    cross = {}
    cross_accumulated = {}
    for r in all_results:
        model_dir = SHADOW_ROOT / r["model_name"]
        acc = aggregate_all_per_date_jsons(model_dir, threshold=args.threshold)
        agg = acc["aggregate"]
        cross[r["model_name"]] = {
            "hit_rate": agg["hit_rate"]["hit_rate"],
            "signal_hit_rate": agg["signal_hit_rate"]["hit_rate"],
            "mase_mean": agg["mase_mean"],
            "simulated_pnl_pct": agg["simulated_pnl"]["total_pnl_pct"],
            "n_trades": agg["simulated_pnl"]["n_trades"],
            "win_rate": agg["simulated_pnl"]["win_rate"],
            "n_rows": agg["n_rows"],
            "n_dates": agg["n_dates"],
            "date_range": acc["date_range"],
        }
        cross_accumulated[r["model_name"]] = acc

    summary_out = args.out or (SHADOW_ROOT / "SUMMARY.json")
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    # Cross-model longitudinal health (ADR-008) — not L2 path-change.
    cross_gates = {}
    for name in cross:
        cross_gates[name] = check_shadow_gate(cross_accumulated[name]["aggregate"])
    summary_out.write_text(json.dumps({
        "today": today.isoformat(),
        "cutoffs": cutoffs,
        "threshold_pct": args.threshold,
        "by_model": cross,
        # Preferred name (Gate Decision Architecture).
        "longitudinal_health_by_model": cross_gates,
        # Legacy alias — same payload; do not treat as auto-promote.
        "promotion_gates": cross_gates,
        "ranking_by_simulated_pnl": sorted(
            cross.keys(),
            key=lambda n: cross[n]["simulated_pnl_pct"] or 0.0,
            reverse=True,
        ),
        "ranking_by_hit_rate": sorted(
            cross.keys(),
            key=lambda n: cross[n]["hit_rate"] or 0.0,
            reverse=True,
        ),
        "health_check_defaults": DEFAULT_SHADOW_GATE,
        "gate_defaults": DEFAULT_SHADOW_GATE,  # legacy alias
        "note": (
            "longitudinal_health is ADR-008 health only; "
            "path change requires L2 evaluate_path_change + human"
        ),
    }, indent=2, default=str))
    print(f"\n[write] {summary_out}", file=sys.stderr)

    # Console table — sorted by accumulated PnL descending
    sorted_names = sorted(
        cross.keys(),
        key=lambda n: cross[n]["simulated_pnl_pct"] or 0.0,
        reverse=True,
    )
    print(
        "\n=== SHADOW-TRADE (accumulated PnL; health=longitudinal ADR-008, not L2 promote) ==="
    )
    for i, name in enumerate(sorted_names, 1):
        c = cross[name]
        gate = cross_gates[name]
        health_str = "HEALTH_OK" if gate["cleared"] else "HEALTH_FAIL"
        date_range = c.get("date_range") or ["?", "?"]
        print(
            f"  #{i} {name}: "
            f"pnl={c['simulated_pnl_pct']}% "
            f"win_rate={c['win_rate']} "
            f"hit_rate={c['hit_rate']} "
            f"signal_hit_rate={c['signal_hit_rate']} "
            f"mase={c['mase_mean']} "
            f"(n_trades={c['n_trades']}, n_rows={c['n_rows']}, "
            f"n_dates={c['n_dates']} {date_range[0]}..{date_range[-1]}) "
            f"[longitudinal={health_str}]"
        )
    for name, gate in cross_gates.items():
        if not gate["cleared"]:
            print(
                f"\n  [longitudinal_health] {name} FAIL: "
                f"{'; '.join(gate['reasons'])}"
            )


def cmd_print_status(out_path: Optional[Path]) -> None:
    """Read-only status: aggregate per-date JSONs + longitudinal health.

    Not an L2 path-change verdict. No model loading. Snapshot to STATUS.json.
    """
    if not SHADOW_ROOT.exists():
        print(f"[status] shadow_root does not exist: {SHADOW_ROOT}", file=sys.stderr)
        sys.exit(2)

    model_dirs = sorted([p for p in SHADOW_ROOT.iterdir() if p.is_dir()])
    if not model_dirs:
        print(f"[status] no model dirs under {SHADOW_ROOT}", file=sys.stderr)
        return

    by_model: dict[str, dict] = {}
    gates: dict[str, dict] = {}
    for mdir in model_dirs:
        acc = aggregate_all_per_date_jsons(mdir)
        agg = acc["aggregate"]
        by_model[mdir.name] = {
            "hit_rate": agg["hit_rate"]["hit_rate"],
            "signal_hit_rate": agg["signal_hit_rate"]["hit_rate"],
            "mase_mean": agg["mase_mean"],
            "simulated_pnl_pct": agg["simulated_pnl"]["total_pnl_pct"],
            "n_trades": agg["simulated_pnl"]["n_trades"],
            "win_rate": agg["simulated_pnl"]["win_rate"],
            "n_rows": agg["n_rows"],
            "n_dates": acc["n_dates"],
            "date_range": acc["date_range"],
        }
        gates[mdir.name] = check_shadow_gate(agg)

    sorted_names = sorted(
        by_model.keys(),
        key=lambda n: by_model[n]["simulated_pnl_pct"] or 0.0,
        reverse=True,
    )

    print(
        f"\n=== SHADOW-TRADE STATUS (accumulated, "
        f"{datetime.now().isoformat(timespec='seconds')}) ==="
    )
    print(f"  shadow_root: {SHADOW_ROOT}")
    print(f"  models: {len(model_dirs)}")
    for name in sorted_names:
        c = by_model[name]
        gate = gates[name]
        health_str = "HEALTH_OK" if gate["cleared"] else "HEALTH_FAIL"
        date_range = c.get("date_range") or ["?", "?"]
        print(
            f"  {name}: "
            f"pnl={c['simulated_pnl_pct']}% "
            f"win_rate={c['win_rate']} "
            f"hit_rate={c['hit_rate']} "
            f"mase={c['mase_mean']} "
            f"(n_trades={c['n_trades']}, n_rows={c['n_rows']}, "
            f"n_dates={c['n_dates']} {date_range[0]}..{date_range[-1]}) "
            f"[longitudinal={health_str}]"
        )
    for name, gate in gates.items():
        if not gate["cleared"]:
            print(
                f"\n  [longitudinal_health] {name} FAIL: "
                f"{'; '.join(gate['reasons'])}"
            )

    status_path = out_path or (SHADOW_ROOT / "STATUS.json")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "shadow_root": str(SHADOW_ROOT),
        "by_model": by_model,
        "longitudinal_health_by_model": gates,
        "promotion_gates": gates,  # legacy alias
        "ranking_by_simulated_pnl": sorted_names,
        "health_check_defaults": DEFAULT_SHADOW_GATE,
        "gate_defaults": DEFAULT_SHADOW_GATE,
        "note": "health only — path change via evaluate_path_change + human",
    }, indent=2, default=str))
    print(f"\n[status] wrote {status_path}", file=sys.stderr)


if __name__ == "__main__":
    main()