"""
Validate candidate regime-aware gates against two Eval reports that span
different market regimes (one uptrend, one drawdown).

For each (symbol, cutoff) row we attach:
  - spy_ret_5d_anchor: SPY 5d return at the eval anchor date
  - sector_ret_5d_anchor: sector proxy 5d return at the eval anchor date
  - sym_realized_vol_20d: 20d std-dev of daily returns at the anchor date
  - sym_above_sma_20: 1 if close > 20d SMA, else 0

We then test several gates (single-rule and combined) and report the
hit_rate after gating on each of the two regimes.

The ideal gate: hit_rate stays high in BOTH regimes.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
EXP_DIR = REPO / "models/chronos/_experiments"

SECTOR_PROXIES = {
    "Technology": "QQQ",
    "Financial": "JPM",
    "Energy": "XOM",
    "Consumer": "AMZN",
    "Healthcare": "JNJ",
    "Industrial": "HON",
}
SECTOR_KEYWORDS = {
    "Technology": {"AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD", "INTC", "ORCL", "CSCO", "ADBE", "CRM"},
    "Financial": {"JPM", "BAC", "GS", "MS", "WFC", "C"},
    "Energy": {"XOM", "CVX", "COP"},
    "Consumer": {"AMZN", "TSLA", "WMT", "HD", "NKE"},
    "Healthcare": {"JNJ", "PFE", "MRK", "UNH"},
    "Industrial": {"HON", "BA", "CAT", "GE"},
}


def _infer_sector(sym: str) -> str:
    for sec, members in SECTOR_KEYWORDS.items():
        if sym in members:
            return sec
    return "Technology"  # default for index ETFs etc.


def _load_returns(sym: str, anchor: pd.Timestamp, lookback: int = 30) -> pd.Series | None:
    p = REPO / f"data/features/daily/{sym}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp").sort_index()
    if anchor not in df.index:
        # use the closest available index <= anchor
        asof = df.index[df.index <= anchor]
        if len(asof) == 0:
            return None
        anchor_used = asof[-1]
    else:
        anchor_used = anchor
    # window ending at anchor_used: previous N trading days
    win = df.loc[df.index <= anchor_used].tail(lookback + 5)
    return win["close"]


def _ret_n(closes: pd.Series, anchor: pd.Timestamp, n: int) -> float | None:
    if closes is None or len(closes) < 2:
        return None
    asof = closes[closes.index <= anchor]
    if len(asof) < n + 1:
        return None
    last = asof.iloc[-1]
    prev = asof.iloc[-(n + 1)]
    if prev == 0 or pd.isna(prev):
        return None
    return float((last - prev) / prev * 100.0)


def _realized_vol_20d(closes: pd.Series, anchor: pd.Timestamp) -> float | None:
    if closes is None or len(closes) < 21:
        return None
    asof = closes[closes.index <= anchor]
    if len(asof) < 21:
        return None
    window = asof.iloc[-21:]
    rets = window.pct_change().dropna()
    if len(rets) < 10:
        return None
    return float(rets.std() * np.sqrt(252) * 100.0)  # annualized vol in %


def _above_sma20(closes: pd.Series, anchor: pd.Timestamp) -> int | None:
    if closes is None or len(closes) < 20:
        return None
    asof = closes[closes.index <= anchor]
    if len(asof) < 20:
        return None
    last = float(asof.iloc[-1])
    sma = float(asof.iloc[-20:].mean())
    if pd.isna(sma) or sma == 0:
        return None
    return 1 if last > sma else 0


def attach_regime(row: dict, anchor: pd.Timestamp) -> dict:
    sym = row["symbol"]
    spy_closes = _load_returns("SPY", anchor)
    sector = _infer_sector(sym)
    proxy = SECTOR_PROXIES.get(sector, "SPY")
    sector_closes = _load_returns(proxy, anchor)
    sym_closes = _load_returns(sym, anchor)

    out = dict(row)
    out["anchor_date"] = anchor.strftime("%Y-%m-%d")
    out["sector"] = sector
    out["spy_ret_5d"] = _ret_n(spy_closes, anchor, 5)
    out["spy_ret_10d"] = _ret_n(spy_closes, anchor, 10)
    out["sector_ret_5d"] = _ret_n(sector_closes, anchor, 5)
    out["sector_ret_10d"] = _ret_n(sector_closes, anchor, 10)
    out["sym_realized_vol_20d"] = _realized_vol_20d(sym_closes, anchor)
    out["sym_above_sma20"] = _above_sma20(sym_closes, anchor)
    return out


def _anchor(fallback: str) -> pd.Timestamp:
    """Make a tz-aware (US/Eastern) Timestamp matching parquet datetimes."""
    return pd.Timestamp(fallback).tz_localize("US/Eastern")


def hit_rate_of(rows: list[dict]) -> tuple[int, int, float]:
    n_correct, n = 0, 0
    for r in rows:
        f = r.get("forecast_ret_pct")
        a = r.get("actual_ret_pct")
        if f is None or a is None:
            continue
        # sign match (zero counts as no-signal — skip)
        if f == 0 or a == 0:
            continue
        n += 1
        if (f > 0) == (a > 0):
            n_correct += 1
    return n_correct, n, (n_correct / n if n else 0.0)


def gate(rows: list[dict], predicate) -> list[dict]:
    return [r for r in rows if predicate(r)]


def summarize(rows: list[dict], gate_name: str) -> dict:
    nc, n, hr = hit_rate_of(rows)
    return {
        "gate": gate_name,
        "n_after_gate": n,
        "n_correct": nc,
        "hit_rate": round(hr, 4),
    }


# Gate predicates -----------------------------------------------------------

def g_no_gate(r): return True


def g_forecast_above(r, thr):
    f = r.get("forecast_ret_pct")
    return f is not None and abs(f) > thr


def g_spy_5d_above(r, thr):
    v = r.get("spy_ret_5d")
    return v is not None and v > thr


def g_sector_5d_above(r, thr):
    v = r.get("sector_ret_5d")
    return v is not None and v > thr


def g_sym_above_sma20(r):
    return r.get("sym_above_sma20") == 1


def g_low_vol(r, max_vol):
    v = r.get("sym_realized_vol_20d")
    return v is not None and v < max_vol


def g_combined(r, spy_thr=0.0, sector_thr=-1.5, fc_thr=0.5):
    """Combined gate: only take long signals when SPY & sector momentum
    are not actively bearish, and forecast has at least some conviction."""
    if not g_spy_5d_above(r, spy_thr):
        return False
    if not g_sector_5d_above(r, sector_thr):
        return False
    if not g_forecast_above(r, fc_thr):
        return False
    return True


def g_combined_short(r, fc_thr=0.5):
    """If forecast is short (negative), only allow when sector is already
    declining (i.e., we're riding the trend, not predicting against it)."""
    if r.get("forecast_ret_pct") is None:
        return False
    if r["forecast_ret_pct"] > 0:
        return g_combined(r)
    # short signal: allow only if sector is also weak
    return (r.get("sector_ret_5d") is not None
            and r["sector_ret_5d"] < -0.5
            and abs(r["forecast_ret_pct"]) > fc_thr)


def g_or(r, p1, p2):
    return p1(r) or p2(r)


def g_and(r, *ps):
    return all(p(r) for p in ps)


# Main ----------------------------------------------------------------------

def process_report(report_path: Path, anchor_date_fallback: str, label: str):
    report = json.loads(report_path.read_text())
    by_cutoff = report["by_cutoff"]
    out = {"label": label, "anchor_date_fallback": anchor_date_fallback, "cutoffs": {}}
    for ck, cobj in by_cutoff.items():
        rows = cobj["per_model_rows"][list(cobj["per_model_rows"].keys())[0]]
        # derive anchor date from data: use last current_close's row indirectly
        # We don't have timestamps in the report, so we trust the caller-supplied
        # fallback date as the eval "today" minus cutoff_days.
        anchor = _anchor(anchor_date_fallback)
        rows = [attach_regime(r, anchor) for r in rows]
        out["cutoffs"][ck] = {
            "all_rows": rows,
            "headline": {
                "mase": cobj["per_model"][list(cobj["per_model"].keys())[0]]["mase"],
                "hit_rate": cobj["per_model"][list(cobj["per_model"].keys())[0]]["directional"]["hit_rate"]["hit_rate"],
                "signal_hit_rate": cobj["per_model"][list(cobj["per_model"].keys())[0]]["directional"]["signal_hit_rate"]["hit_rate"],
            },
        }
    return out


def evaluate_gates(processed: dict, cutoff_key: str) -> list[dict]:
    rows = processed["cutoffs"][cutoff_key]["all_rows"]
    summaries = []
    summaries.append(summarize(rows, "NO_GATE (baseline)"))
    # single-feature gates
    for thr in [0.5, 1.0, 1.5, 2.0]:
        summaries.append(summarize(gate(rows, lambda r, t=thr: g_forecast_above(r, t)),
                                   f"forecast_abs > {thr}%"))
    for thr in [-2.0, -1.0, -0.5, 0.0, 0.5]:
        summaries.append(summarize(gate(rows, lambda r, t=thr: g_spy_5d_above(r, t)),
                                   f"spy_5d > {thr}%"))
    for thr in [-2.0, -1.0, -0.5, 0.0]:
        summaries.append(summarize(gate(rows, lambda r, t=thr: g_sector_5d_above(r, t)),
                                   f"sector_5d > {thr}%"))
    summaries.append(summarize(gate(rows, g_sym_above_sma20), "sym_close > sma20"))
    for thr in [25, 30, 35, 40, 50]:
        summaries.append(summarize(gate(rows, lambda r, t=thr: g_low_vol(r, t)),
                                   f"sym_vol20 < {thr}%"))
    # combined gates
    summaries.append(summarize(gate(rows, lambda r: g_combined(r, 0.0, -1.5, 0.5)),
                               "COMBINED: spy>0, sector>-1.5, |fc|>0.5"))
    summaries.append(summarize(gate(rows, lambda r: g_combined(r, -1.0, -1.0, 0.5)),
                               "COMBINED-soft: spy>-1, sector>-1, |fc|>0.5"))
    summaries.append(summarize(gate(rows, lambda r: g_combined_short(r, 0.5)),
                               "COMBINED-2way: long-only in uptrend, short-only in decline"))
    # OR gates — union of best single-feature gates
    summaries.append(summarize(
        gate(rows, lambda r: g_or(r,
            lambda x: g_sym_above_sma20(x),
            lambda x: g_sector_5d_above(x, -1.0))),
        "OR(sym>sma20, sector_5d>-1.0)"))
    summaries.append(summarize(
        gate(rows, lambda r: g_or(r,
            lambda x: g_sym_above_sma20(x),
            lambda x: g_forecast_above(x, 1.5))),
        "OR(sym>sma20, |fc|>1.5)"))
    summaries.append(summarize(
        gate(rows, lambda r: g_or(r,
            lambda x: g_sym_above_sma20(x),
            lambda x: g_forecast_above(x, 2.0))),
        "OR(sym>sma20, |fc|>2.0)"))
    # AND gates — strict combination
    summaries.append(summarize(
        gate(rows, lambda r: g_and(r,
            lambda x: g_sym_above_sma20(x),
            lambda x: g_sector_5d_above(x, -1.0))),
        "AND(sym>sma20, sector_5d>-1.0)"))
    summaries.append(summarize(
        gate(rows, lambda r: g_and(r,
            lambda x: g_sym_above_sma20(x),
            lambda x: g_low_vol(x, 35))),
        "AND(sym>sma20, vol<35)"))
    summaries.append(summarize(
        gate(rows, lambda r: g_and(r,
            lambda x: g_sym_above_sma20(x),
            lambda x: g_forecast_above(x, 0.5))),
        "AND(sym>sma20, |fc|>0.5)"))
    return summaries


def main():
    # Eval 1 (14:35): 9 syms, anchor today minus cutoff 10/20 days
    # We know current_close was Jun 10/16-area, so today ~Jul 1 → anchor ~ Jun 16 (cutoff=10)
    # We use the actual calendar anchor: eval day = 2026-07-01, cutoff=10 → anchor Jun 16
    R1 = EXP_DIR / "round3_E05_ctx512_p3_coldstart_Round_3_—_E05_ctx=512,_P3_cold-start_(5_seeds)/round3_E05_ctx512_p3_coldstart_seed7/round3_E05_ctx512_p3_coldstart_seed7_full_report.json"
    R2 = EXP_DIR / "round3_E05_ctx512_p3_coldstart_Round_3_—_E05_ctx=512,_P3_cold-start_(5_seeds)/round3_E05_ctx512_p3_coldstart_seed7/round3_E05_ctx512_p3_coldstart_seed7_full_report_with_meta.json"

    # Eval 1 anchor (14:35): cutoff=10 ~ Jun 15, cutoff=20 ~ Jun 5
    p1 = process_report(R1, "2026-06-15", "REGIME_A_uptrend_14:35_9syms")
    p1_cut20 = process_report(R1, "2026-06-05", "REGIME_A_uptrend_14:35_9syms")
    # Eval 2 anchor (17:11): cutoff=10 ~ Jun 16, cutoff=20 ~ Jun 6
    p2 = process_report(R2, "2026-06-16", "REGIME_B_drawdown_17:11_10syms")
    p2_cut20 = process_report(R2, "2026-06-06", "REGIME_B_drawdown_17:11_10syms")

    # Use a per-cutoff dictionary (override cutoff=20 anchor by re-processing)
    # Simpler: just evaluate gates on each cutoff independently using its own anchor
    datasets = []
    for label, proc in [("REGIME_A_uptrend_14:35_9syms", p1),
                        ("REGIME_B_drawdown_17:11_10syms", p2)]:
        for ck in proc["cutoffs"]:
            anchor = "2026-06-15" if ck == "10" else "2026-06-05"
            if "drawdown" in label:
                anchor = "2026-06-16" if ck == "10" else "2026-06-06"
            datasets.append((f"{label}_cutoff{ck}", proc["cutoffs"][ck]["all_rows"], anchor))

    # For simplicity, we instead build the report data structures once per (label, cutoff)
    # by calling process_report with the right anchor for each cutoff. Since our helper
    # already stores a single anchor, we re-derive here:
    out_per = {}
    for (label, proc, ck) in [
        ("REGIME_A_uptrend_14:35_9syms", p1, "10"),
        ("REGIME_A_uptrend_14:35_9syms", p1_cut20, "20"),
        ("REGIME_B_drawdown_17:11_10syms", p2, "10"),
        ("REGIME_B_drawdown_17:11_10syms", p2_cut20, "20"),
    ]:
        out_per[f"{label}_cutoff{ck}"] = {
            "all_rows": proc["cutoffs"][ck]["all_rows"],
            "headline": proc["cutoffs"][ck]["headline"],
        }

    # Compute gate summaries
    rows_table = []
    for ds_label, d in out_per.items():
        # Re-derive per-cutoff anchor (different from what process_report stored)
        ck = ds_label.rsplit("cutoff", 1)[1]
        anchor = ("2026-06-15" if ck == "10" else "2026-06-05")
        if "drawdown" in ds_label:
            anchor = ("2026-06-16" if ck == "10" else "2026-06-06")
        # Re-attach regime with the per-cutoff anchor (we attached a uniform anchor in
        # process_report; recompute here):
        rows = []
        for r in d["all_rows"]:
            r2 = dict(r)
            r2.pop("spy_ret_5d", None); r2.pop("spy_ret_10d", None)
            r2.pop("sector_ret_5d", None); r2.pop("sector_ret_10d", None)
            r2.pop("sym_realized_vol_20d", None); r2.pop("sym_above_sma20", None)
            r2.pop("anchor_date", None)
            rows.append(attach_regime(r2, _anchor(anchor)))
        summaries = evaluate_gates({"cutoffs": {ck: {"all_rows": rows}}}, ck)
        for s in summaries:
            rows_table.append({
                "dataset": ds_label,
                "cutoff": ck,
                "gate": s["gate"],
                "n_after_gate": s["n_after_gate"],
                "n_correct": s["n_correct"],
                "hit_rate": s["hit_rate"],
                "headline_hr": d["headline"]["hit_rate"],
            })

    df = pd.DataFrame(rows_table)
    # Show: per gate, the hit_rate in each of the 4 datasets
    pivot = df.pivot_table(index="gate", columns="dataset", values="hit_rate", aggfunc="first")
    pivot_n = df.pivot_table(index="gate", columns="dataset", values="n_after_gate", aggfunc="first")
    pivot_correct = df.pivot_table(index="gate", columns="dataset", values="n_correct", aggfunc="first")
    print("\n=== HIT_RATE after each gate (per dataset) ===")
    print(pivot.to_string())
    print("\n=== n_after_gate ===")
    print(pivot_n.to_string())
    print("\n=== n_correct ===")
    print(pivot_correct.to_string())

    # Compute stability score per gate = min(hit_rate) across the 4 datasets
    # (worst-case regime), and average n_after_gate to assess tradable volume
    score = pd.DataFrame({
        "min_hr_across_regimes": pivot.min(axis=1),
        "mean_hr_across_regimes": pivot.mean(axis=1),
        "min_n_after_gate": pivot_n.min(axis=1),
        "mean_n_after_gate": pivot_n.mean(axis=1),
    }).sort_values("min_hr_across_regimes", ascending=False)
    print("\n=== GATE STABILITY (sorted by worst-case hit_rate) ===")
    print(score.to_string())

    # Also save detailed table for later
    out_path = REPO / "models/chronos/_experiments/regime_gate_validation.csv"
    df.to_csv(out_path, index=False)
    print(f"\nFull table saved to: {out_path}")


if __name__ == "__main__":
    main()