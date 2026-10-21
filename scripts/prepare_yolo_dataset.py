#!/usr/bin/env python3
"""
YOLO Dataset Preparation — render 5min OHLCV as 640x640 chart JPGs.

Reads partitioned parquet from data/intraday_clean_5min/phase{1,2}/,
optionally filters to RTH (09:30-16:00 US/Eastern), and renders each
(symbol, datetime) pair as a candlestick + volume chart matching the
existing data/yolo_training_images/ convention.

Output:
  <output-dir>/images/<SYMBOL>_<YYYYMMDD>_<HHMMSS>.jpg
  <output-dir>/labels.csv   (synthetic pattern labels; optional)
  <output-dir>/manifest.json

Usage:
  PYTHONPATH=src:scripts:. python3 scripts/prepare_yolo_dataset.py \
      --source-phase 1 \
      --symbols AAPL,MSFT,NVDA \
      --start 2024-01-01 --end 2024-12-31 \
      --rth-only false \
      --samples-per-symbol 50 \
      --output-dir data/intraday_clean_5min/phase2/ready_for_yolo
"""

import argparse
import json
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional, List, Dict

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from loguru import logger

# Matplotlib must use Agg (no display)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IMAGE_SIZE = 640
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# RTH window in US/Eastern
RTH_OPEN = dt_time(9, 30)
RTH_CLOSE = dt_time(16, 0)

# Synthetic-label rules (synthetic_v1)
LABEL_RULES = {
    "trend_up":   lambda ctx: ctx["ret_1h"] >  0.003,
    "trend_down": lambda ctx: ctx["ret_1h"] < -0.003,
    "breakout":   lambda ctx: ctx["high"] > ctx["prior_day_high"] + ctx["atr_14"],
    "reversal":   lambda ctx: (np.sign(ctx["ret_1h"]) != np.sign(ctx["prev_ret_1h"])
                                and abs(ctx["ret_1h"]) > 0.005),
    "chop":       lambda ctx: (abs(ctx["ret_1h"]) < 0.001
                                and ctx["vol_ratio"] < 0.7),
}
# Default fallback
DEFAULT_LABEL = "normal"

# Style
BG_COLOR = "#0E1117"
FG_COLOR = "#E6E6E6"
GRID_COLOR = "#1F2937"
UP_COLOR = "#26A69A"
DOWN_COLOR = "#EF5350"
VOL_UP = "#26A69A80"
VOL_DOWN = "#EF535080"


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------
def discover_partitions(source_root: Path, symbols: Optional[List[str]] = None) -> Dict[str, List[Path]]:
    """Return mapping symbol -> [parquet files] under date=* partitions."""
    if not source_root.exists():
        raise FileNotFoundError(f"source phase root not found: {source_root}")

    partitions: Dict[str, List[Path]] = {}
    for date_dir in sorted(source_root.glob("date=*")):
        if not date_dir.is_dir():
            continue
        for sym_dir in date_dir.glob("symbol=*"):
            sym = sym_dir.name.split("symbol=")[-1].upper()
            if symbols and sym not in {s.upper() for s in symbols}:
                continue
            pq_path = sym_dir / "data.parquet"
            if pq_path.exists():
                partitions.setdefault(sym, []).append(pq_path)
    return partitions


# ---------------------------------------------------------------------------
# Label computation
# ---------------------------------------------------------------------------
def compute_features_for_day(day_df: pd.DataFrame, prior_day_high: Optional[float]) -> Dict:
    """Compute the features needed by LABEL_RULES from one trading day's 5min bars."""
    if day_df.empty:
        return {}

    # Ensure tz-aware US/Eastern
    ts = pd.to_datetime(day_df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("US/Eastern")
    else:
        ts = ts.dt.tz_convert("US/Eastern")
    day_df = day_df.assign(_ts=ts).sort_values("_ts")

    # 1h return (12 bars)
    if len(day_df) >= 13:
        ret_1h = float(day_df["close"].iloc[-1] / day_df["close"].iloc[-13] - 1)
        prev_ret_1h = float(day_df["close"].iloc[-13] / day_df["close"].iloc[-25] - 1) if len(day_df) >= 25 else 0.0
    else:
        ret_1h = 0.0
        prev_ret_1h = 0.0

    high = float(day_df["high"].max())
    atr_14 = float((day_df["high"] - day_df["low"]).rolling(14, min_periods=1).mean().iloc[-1])
    vol_today = float(day_df["volume"].sum())
    # 20d median vol: caller precomputes and passes
    return {
        "ret_1h": ret_1h,
        "prev_ret_1h": prev_ret_1h,
        "high": high,
        "atr_14": atr_14,
        "vol_total": vol_today,
        "prior_day_high": prior_day_high or high,
        "vol_ratio": 1.0,  # patched later if 20d median available
    }


def assign_label(ctx: Dict) -> tuple:
    """Return (label, rule_id, confidence) using LABEL_RULES."""
    # First match wins; rules ordered by priority
    rule_order = ["breakout", "reversal", "trend_up", "trend_down", "chop"]
    for rule in rule_order:
        try:
            if LABEL_RULES[rule](ctx):
                # Confidence = magnitude proxy
                if rule in ("trend_up", "trend_down"):
                    conf = min(1.0, abs(ctx.get("ret_1h", 0)) / 0.02)
                elif rule == "breakout":
                    conf = min(1.0, max(0.0,
                        (ctx["high"] - ctx["prior_day_high"]) / max(ctx["atr_14"], 1e-6)) / 2)
                elif rule == "reversal":
                    conf = min(1.0, abs(ctx.get("ret_1h", 0)) / 0.01)
                elif rule == "chop":
                    conf = min(1.0, 1.0 - abs(ctx.get("ret_1h", 0)) / 0.001)
                else:
                    conf = 0.5
                return rule, f"{rule}_v1", conf
        except Exception:
            continue
    return DEFAULT_LABEL, "default", 0.5


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_chart(df_day: pd.DataFrame, symbol: str, date_str: str,
                 out_path: Path) -> Optional[str]:
    """Render one trading day as 640x640 JPG. Returns the filename."""
    if df_day.empty or len(df_day) < 5:
        return None

    # Ensure tz-aware
    ts = pd.to_datetime(df_day["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("US/Eastern")
    else:
        ts = ts.dt.tz_convert("US/Eastern")

    # Use HH:MM:SS of last bar as filename time (mirrors existing convention)
    last_bar_ts = ts.iloc[-1]
    time_str = last_bar_ts.strftime("%H%M%S")
    fname = f"{symbol}_{date_str}_{time_str}.jpg"

    fig = plt.figure(figsize=(IMAGE_SIZE / 100, IMAGE_SIZE / 100), dpi=100)
    gs = fig.add_gridspec(4, 1, hspace=0.05,
                          left=0.05, right=0.97, top=0.93, bottom=0.06)
    ax_price = fig.add_subplot(gs[:3, 0])
    ax_vol = fig.add_subplot(gs[3, 0], sharex=ax_price)

    # Price candlesticks
    opens = df_day["open"].values
    highs = df_day["high"].values
    lows = df_day["low"].values
    closes = df_day["close"].values
    xs = np.arange(len(df_day))
    width = 0.7

    for i in range(len(df_day)):
        up = closes[i] >= opens[i]
        color = UP_COLOR if up else DOWN_COLOR
        # Wick
        ax_price.plot([xs[i], xs[i]], [lows[i], highs[i]],
                      color=color, linewidth=0.8, zorder=2)
        # Body
        body_low = min(opens[i], closes[i])
        body_high = max(opens[i], closes[i])
        rect = Rectangle((xs[i] - width / 2, body_low), width,
                         max(body_high - body_low, 1e-6),
                         facecolor=color, edgecolor=color, zorder=3)
        ax_price.add_patch(rect)

    ax_price.set_facecolor(BG_COLOR)
    ax_price.set_title(f"{symbol} {date_str}", color=FG_COLOR,
                       fontsize=14, fontweight="bold", loc="left")
    ax_price.tick_params(colors=FG_COLOR, labelsize=8)
    for spine in ax_price.spines.values():
        spine.set_color(GRID_COLOR)
    ax_price.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.5)
    ax_price.set_ylabel("Price", color=FG_COLOR, fontsize=10)
    plt.setp(ax_price.get_xticklabels(), visible=False)

    # Volume bars
    volumes = df_day["volume"].values
    for i in range(len(df_day)):
        up = closes[i] >= opens[i]
        color = VOL_UP if up else VOL_DOWN
        ax_vol.bar(xs[i], volumes[i], width=width, color=color, zorder=2)

    ax_vol.set_facecolor(BG_COLOR)
    ax_vol.tick_params(colors=FG_COLOR, labelsize=8)
    for spine in ax_vol.spines.values():
        spine.set_color(GRID_COLOR)
    ax_vol.grid(True, color=GRID_COLOR, linewidth=0.5, alpha=0.5)
    ax_vol.set_ylabel("Vol", color=FG_COLOR, fontsize=10)

    # X-axis labels: show HH:MM for ~8 evenly spaced bars
    n = len(df_day)
    label_idx = np.linspace(0, n - 1, min(8, n)).astype(int)
    label_times = [ts.iloc[i].strftime("%H:%M") for i in label_idx]
    ax_vol.set_xticks(label_idx)
    ax_vol.set_xticklabels(label_times, rotation=0, fontsize=8)

    fig.patch.set_facecolor(BG_COLOR)
    try:
        # Use bbox_inches=None to preserve exact 640x640 figure dimensions.
        fig.savefig(out_path / fname, dpi=100, facecolor=BG_COLOR,
                    bbox_inches=None, pad_inches=0)
    finally:
        plt.close(fig)

    return fname


# ---------------------------------------------------------------------------
# RTH filter
# ---------------------------------------------------------------------------
def filter_rth(df_day: pd.DataFrame) -> pd.DataFrame:
    if df_day.empty:
        return df_day
    ts = pd.to_datetime(df_day["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("US/Eastern")
    else:
        ts = ts.dt.tz_convert("US/Eastern")
    times = ts.dt.time
    mask = (times >= RTH_OPEN) & (times <= RTH_CLOSE)
    return df_day.loc[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-phase", type=int, required=True, choices=[1, 2])
    parser.add_argument("--source-root", default=None,
                        help="Override source root (default data/intraday_clean_5min/phase{N})")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbol list; default = all in source")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD")
    parser.add_argument("--rth-only", action="store_true",
                        help="Filter to 09:30-16:00 US/Eastern only")
    parser.add_argument("--samples-per-symbol", type=int, default=50)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--image-size", type=int, default=IMAGE_SIZE)
    args = parser.parse_args()

    source_root = Path(args.source_root) if args.source_root else \
        Path(f"data/intraday_clean_5min/phase{args.source_phase}")
    output_dir = Path(args.output_dir)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    logger.info(f"[YOLO] source={source_root} symbols={symbols or 'ALL'} "
                f"start={args.start} end={args.end} rth_only={args.rth_only}")

    partitions = discover_partitions(source_root, symbols)
    if not partitions:
        logger.error(f"No partitions found under {source_root}")
        sys.exit(1)

    # 20d median volume per symbol for chop label
    sym_vol_20d_median: Dict[str, float] = {}
    for sym, files in partitions.items():
        try:
            # Sample last 30 days only for speed
            recent = sorted(files)[-30:]
            if not recent:
                continue
            df_all = pd.concat([pq.ParquetFile(p).read().to_pandas() for p in recent],
                               ignore_index=True)
            vol_total_by_day = df_all.groupby(df_all["timestamp"].dt.date)["volume"].sum()
            if len(vol_total_by_day) >= 5:
                sym_vol_20d_median[sym] = float(vol_total_by_day.median())
        except Exception as e:
            logger.warning(f"Could not compute vol median for {sym}: {e}")

    # Discover dates
    start_dt = pd.to_datetime(args.start).date() if args.start else None
    end_dt = pd.to_datetime(args.end).date() if args.end else None

    labels: List[Dict] = []
    images_written = 0
    symbols_processed = 0
    rng = np.random.default_rng(42)

    for sym, files in sorted(partitions.items()):
        # Group files by date
        by_date: Dict[str, List[Path]] = {}
        for f in files:
            date_str = f.parent.parent.name.split("date=")[-1]
            if start_dt and pd.to_datetime(date_str).date() < start_dt:
                continue
            if end_dt and pd.to_datetime(date_str).date() > end_dt:
                continue
            by_date.setdefault(date_str, []).append(f)

        if not by_date:
            continue

        # Sample dates
        date_list = sorted(by_date.keys())
        if args.samples_per_symbol and len(date_list) > args.samples_per_symbol:
            sampled_dates = list(rng.choice(date_list, size=args.samples_per_symbol, replace=False))
            sampled_dates = sorted(sampled_dates)
        else:
            sampled_dates = date_list

        for date_str in sampled_dates:
            try:
                df_day = pd.concat(
                    [pq.ParquetFile(f).read().to_pandas() for f in by_date[date_str]],
                    ignore_index=True
                ).sort_values("timestamp").reset_index(drop=True)
            except Exception as e:
                logger.warning(f"Failed to read {sym} {date_str}: {e}")
                continue

            if args.rth_only:
                df_day = filter_rth(df_day)

            # Prior-day high (for breakout label)
            prior_high = None
            try:
                idx = date_list.index(date_str)
                if idx > 0:
                    prior_date = date_list[idx - 1]
                    df_prior = pd.concat(
                        [pq.ParquetFile(f).read().to_pandas() for f in by_date[prior_date]],
                        ignore_index=True
                    )
                    prior_high = float(df_prior["high"].max())
            except Exception:
                prior_high = None

            # Render
            fname = render_chart(df_day, sym, date_str, images_dir)
            if not fname:
                continue

            # Label
            ctx = compute_features_for_day(df_day, prior_high)
            ctx["vol_ratio"] = (ctx.get("vol_total", 0) /
                                max(sym_vol_20d_median.get(sym, ctx.get("vol_total", 1)), 1))
            label, rule_id, conf = assign_label(ctx)

            labels.append({
                "image": fname,
                "symbol": sym,
                "date": date_str,
                "time_et": fname.split("_")[-1].replace(".jpg", ""),
                "close": float(df_day["close"].iloc[-1]),
                "volume_total": float(df_day["volume"].sum()),
                "ret_1h": round(ctx.get("ret_1h", 0.0), 6),
                "pattern_label": label,
                "rule_id": rule_id,
                "confidence": round(conf, 4),
            })
            images_written += 1

        symbols_processed += 1
        logger.info(f"[YOLO] {sym}: rendered {sum(1 for l in labels if l['symbol'] == sym)} images")

    # Write labels.csv
    labels_path = output_dir / "labels.csv"
    if labels:
        labels_df = pd.DataFrame(labels)
        labels_df.to_csv(labels_path, index=False)

    # Manifest
    manifest = {
        "source_phase": args.source_phase,
        "source_root": str(source_root),
        "output_dir": str(output_dir),
        "image_size": args.image_size,
        "rth_only": args.rth_only,
        "samples_per_symbol": args.samples_per_symbol,
        "generated_at": datetime.now().isoformat(),
        "symbols_requested": symbols or "ALL",
        "symbols_processed": symbols_processed,
        "images_written": images_written,
        "labels_csv": str(labels_path),
        "images_dir": str(images_dir),
        "label_distribution": (
            pd.DataFrame(labels)["pattern_label"].value_counts().to_dict() if labels else {}
        ),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Validation
    from PIL import Image
    bad = []
    for img_path in images_dir.glob("*.jpg"):
        try:
            with Image.open(img_path) as im:
                if im.size != (args.image_size, args.image_size):
                    bad.append((img_path.name, im.size))
        except Exception as e:
            bad.append((img_path.name, str(e)))

    print(f"\n[YOLO] rendered {images_written} images from {symbols_processed} symbols into {output_dir}")
    print(f"[YOLO] labels.csv: {len(labels)} rows")
    print(f"[YOLO] label_distribution: {manifest['label_distribution']}")
    if bad:
        print(f"[YOLO] WARNING: {len(bad)} images had size != {args.image_size}: {bad[:5]}")
    else:
        print(f"[YOLO] all images verified at {args.image_size}x{args.image_size}")


if __name__ == "__main__":
    main()