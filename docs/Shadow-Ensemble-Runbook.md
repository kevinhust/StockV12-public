# Shadow Ensemble Runbook

**Audience:** Operator running StockApp-V12 Tier 2 paper-trading shadow.
**Cadence:** Daily 16:10 NYC cron (5 min after single-model shadow).
**Purpose:** Accumulate live evidence for the A+B ensemble backend before
promoting it to Tier 2 paper trading.

---

## TL;DR

- **Where data lives:** `models/chronos/_shadow/ensemble/`
- **What to monitor:** hit_rate, spread_p50/p95, fallback_rate
- **Decision:** After 1-2 weeks of cron data, compare accumulated
  hit_rate to single-model+gate baseline; promote if hit_rate ≥ baseline
  AND fallback_rate < 25% (3% spread threshold is too noisy above that).
- **This runbook covers:** Where data lives, how to read JSONs, what the
  numbers mean, when to promote, when to abort.

---

## 1. The loop

### Daily cron (host-agnostic, NYC time)

```
# $STOCKAPP_ROOT/v12/scripts/run_shadow_ensemble.sh
10 16 * * 1-5  →  run_shadow_ensemble.sh
  ├─ Loads N predictors (env: CHRONOS_ENSEMBLE_MODEL_PATHS)
  ├─ For each (symbol in 10-symbol basket):
  │   └─ Per-model predict → ensemble aggregate
  │       ├─ if per-model spread ≤ 3.0%  → arithmetic mean
  │       └─ if per-model spread > 3.0%  → fallback to first model
  ├─ Writes $STOCKAPP_ROOT/models/chronos/_shadow/ensemble/YYYY-MM-DD.json
  ├─ Writes $STOCKAPP_ROOT/models/chronos/_shadow/ensemble/per_model/{label}/YYYY-MM-DD.json
  └─ Refreshes $STOCKAPP_ROOT/models/chronos/_shadow/ensemble/SUMMARY.json
```

**Where the cron runs:** originally on the GPU box (`/home/trainer/Stockapp`)
because AutoGluon Chronos on CPU was assumed slow. 2026-07-01 move to NUC
(`/home/app/Stockapp`) for reliability — the GPU box is frequently off
and not user-controlled. CPU inference for `chronos-bolt-small` is fast
enough (~5-10s per symbol, daily total ~2-3 min).

**Host detection:** `STOCKAPP_ROOT` defaults to `/home/$(whoami)/Stockapp`
in both wrapper scripts, so the same scripts work on either host with no
env override. Override with `STOCKAPP_ROOT=/path/to/repo` if the repo
lives elsewhere.

### Skip conditions

| Condition | Behavior |
|-----------|----------|
| Weekend (Sat/Sun) | Skip (markets closed) |
| Before 16:10 NYC | Skip (incomplete daily close) |
| Model dir missing | Fail (cron logs to `/tmp/shadow_ensemble_cron.log`) |
| One model fails on a symbol | Other models still contribute; spread decision is made on valid models only |

### Manual run (for ad-hoc backfill)

```bash
# Run for a specific past date (useful for filling gaps after a missed cron)
TZ=America/New_York TODAY_OVERRIDE=2026-07-01 /home/$(whoami)/Stockapp/v12/scripts/run_shadow_ensemble.sh

# Or call the script directly with a different cutoff window
cd $STOCKAPP_ROOT
PYTHONPATH=v12/src:src:. python3 v12/scripts/shadow_ensemble.py \
    --cutoff-days-ago 7 \
    --today 2026-07-01
```

---

## 2. File layout

```
models/chronos/_shadow/
├── phase2_v2/                         # ← single-model shadow (existing)
│   ├── YYYY-MM-DD.json
│   └── SUMMARY.json
├── E05_p3_coldstart_seed42/           # ← single-model shadow (existing)
│   ├── YYYY-MM-DD.json
│   └── SUMMARY.json
├── SUMMARY.json                       # ← cross-model summary (existing)
└── ensemble/                          # ← NEW for shadow mode
    ├── YYYY-MM-DD.json                #    ensemble aggregate per date
    ├── SUMMARY.json                   #    accumulated stats (hit_rate, spread, fallback)
    └── per_model/
        ├── phase2_v2/
        │   └── YYYY-MM-DD.json        #    raw per-model prediction
        └── E05_p3_coldstart_seed42/
            └── YYYY-MM-DD.json
```

The `ensemble/per_model/{label}/` JSONs are identical in shape to the
single-model shadow JSONs (`{"model", "eval_date", "rows": [...]}`). They
serve as a cross-check: if a single-model shadow JSON disagrees with the
ensemble's per-model row, the operator has a discrepancy to investigate.

---

## 3. Reading the per-date JSON

`models/chronos/_shadow/ensemble/2026-07-01.json`:

```json
{
  "ensemble_name": "A_B_ensemble",
  "ensemble_models": ["phase2_v2", "E05_p3_coldstart_seed42"],
  "eval_date": "2026-07-01",
  "today": "2026-07-02",
  "fallback_spread_pct": 3.0,
  "n_symbols": 10,
  "n_used_fallback": 1,
  "rows": [
    {
      "symbol": "AAPL",
      "current_close": 299.24,
      "forecast_mean": 300.43,
      "forecast_ret_pct": 0.398,
      "actual_mean": 297.15,
      "actual_ret_pct": -0.699,
      "mase": 1.97,
      "per_model_forecast_ret_pct": {
        "phase2_v2": 0.92,
        "E05_p3_coldstart_seed42": -0.13
      },
      "per_model_spread_pct": 1.05,
      "used_fallback": false,
      "fallback_reason": null,
      "n_models_valid": 2
    }
  ]
}
```

### Key fields

- **`forecast_ret_pct` vs `actual_ret_pct`** — the sign-match question.
  Same sign → "hit".  Pair `(forecast_ret_pct, actual_ret_pct)` is
  what `hit_rate` aggregates over.
- **`per_model_spread_pct`** — `max(forecast_ret_pct) - min(forecast_ret_pct)`
  across the N models. If > 3.0%, fallback fires.
- **`used_fallback`** — `true` means the row's forecast was the FIRST
  model's forecast, not the arithmetic mean. The `fallback_reason`
  field names which model was used and why.
- **`n_models_valid`** — sometimes < N. If a model fails to predict
  (e.g. parquet missing), the ensemble runs on the survivors.

---

## 4. Reading the SUMMARY

`models/chronos/_shadow/ensemble/SUMMARY.json`:

```json
{
  "ensemble_name": "A_B_ensemble",
  "ensemble_models": ["phase2_v2", "E05_p3_coldstart_seed42"],
  "today": "2026-07-02",
  "generated_at": "2026-07-02T16:10:42-04:00",
  "fallback_spread_pct": 3.0,
  "current_run_aggregate": {
    "n_rows": 10,
    "n_pairs": 10,
    "hit_rate": {"hit_rate": 0.7, "n_pairs": 10, "n_hits": 7},
    "signal_hit_rate": {"hit_rate": 0.75, "n_pairs": 8, "n_hits": 6, "threshold": 0.5},
    "mase_mean": 1.85,
    "n_used_fallback": 1,
    "fallback_rate": 0.1
  },
  "accumulated": {
    "n_dates": 7,
    "date_range": ["2026-06-24", "2026-07-02"],
    "aggregate": {
      "n_rows": 70,
      "n_pairs": 70,
      "hit_rate": {"hit_rate": 0.6, "n_pairs": 70, "n_hits": 42},
      "signal_hit_rate": {"hit_rate": 0.62, "n_pairs": 60, "n_hits": 37, "threshold": 0.5},
      "mase_mean": 2.05,
      "simulated_pnl": {
        "total_pnl_pct": 142.5,
        "avg_pnl_per_trade_pct": 1.42,
        "n_trades": 60,
        "n_wins": 37,
        "win_rate": 0.62,
        "threshold": 0.5
      },
      "spread_pcts": [0.5, 1.0, 0.3, ...],
      "spread_stats": {
        "n": 70,
        "min": 0.1, "p50": 1.4, "p95": 4.8, "max": 6.2
      },
      "fallback_count": 9,
      "fallback_rate": 0.1286
    },
    "n_dates_by_source": {"cron": 7, "backfill": 0},
    "n_rows_by_source": {"cron": 70, "backfill": 0}
  }
}
```

### What to look at first (in order)

1. **`accumulated.aggregate.spread_stats.p95`** — if > 5.0%, the 3.0%
   fallback threshold is being approached/breached too often. Operator
   should consider raising the threshold (e.g. 4.0%) to reduce
   fallbacks, OR investigate why the models disagree so much.
2. **`accumulated.aggregate.fallback_rate`** — fraction of
   (symbol, day) tuples that used fallback instead of averaging. Sweet
   spot is 10-20%. Above 25% means the ensemble is mostly degenerate
   (averaging rarely happens). Below 5% means models always agree
   (averaging is happening but isn't doing much work).
3. **`accumulated.aggregate.hit_rate.hit_rate`** — directional accuracy
   across all forecast/actual pairs. Compare to the single-model+gate
   baseline (see §6 below).
4. **`accumulated.aggregate.mase_mean`** — average magnitude error.
   Lower is better, but absolute number depends on data window.
5. **`accumulated.aggregate.simulated_pnl`** — long/short/flat
   simulated P&L using 0.5% threshold. If hit_rate is good but PnL
   is bad, the bias is miscalibrated (model takes low-magnitude trades
   that are barely profitable).

---

## 5. The fallback contract (recap)

| Per-model spread | Behavior | Audit field |
|------------------|----------|-------------|
| ≤ 3.0% | Arithmetic mean of all model forecasts | `used_fallback: false` |
| > 3.0% | Use FIRST model's forecast (Variant A — the more-conservative seed) | `used_fallback: true`, `fallback_reason: "..."` |

The first model in the list is the safer one (validated against
`v12/scripts/validate_ensemble_gate.py`, 2026-07-01). When models
disagree by >3%, the ensemble defaults to it rather than averaging
contradictory signals.

---

## 6. Decision matrix: shadow → paper

After 1-2 weeks of cron data (≥7 trading days, ideally ≥14), decide:

| Signal | Green (promote) | Yellow (hold) | Red (abort) |
|--------|-----------------|---------------|-------------|
| `hit_rate` vs single-model+gate | ≥ baseline | within 5pp below | > 5pp below |
| `fallback_rate` | < 20% | 20-30% | > 30% |
| `spread_p95` | < 5.0% | 5.0-7.0% | > 7.0% |
| `mase_mean` | within 10% of single-model | within 25% | > 25% worse |
| `n_dates` (cron) | ≥ 7 | 4-6 | < 4 |

**Why this matrix:** the ensemble is positioned as a *variance reducer*,
not a regime hedge (Round 2 council decision, 2026-07-01). We expect
~equal hit_rate with materially lower single-day variance in confident
calls. If hit_rate regresses, the averaging is hurting more than helping
for this symbol set.

**Promotion action:** remove the `if ensemble_backend is None` branch
fallback in `src/services/lifecycle_scheduler.py` and force the
ensemble path. Update `.env` to ensure `CHRONOS_ENSEMBLE_MODEL_PATHS`
is set permanently (not commented out).

**Abort action:** delete the cron entry, archive `_shadow/ensemble/`
to `_archive/2026-07/`, and revert Tier 2 to single-model+gate.

---

## 7. Quick commands

### Check today's run

```bash
# Last run's one-liner
tail -5 /tmp/shadow_ensemble_cron.log

# Today's per-date JSON
cat models/chronos/_shadow/ensemble/$(date +%Y-%m-%d).json | head -30
```

### Check accumulated status

```bash
# Compact one-liner of accumulated stats
python3 -c "
import json
d = json.load(open('models/chronos/_shadow/ensemble/SUMMARY.json'))
agg = d['accumulated']['aggregate']
ss = agg['spread_stats']
print(f'n_dates={d[\"accumulated\"][\"n_dates\"]}  '
      f'hit_rate={agg[\"hit_rate\"][\"hit_rate\"]}  '
      f'spread_p50={ss[\"p50\"]}  spread_p95={ss[\"p95\"]}  '
      f'fallback_rate={agg[\"fallback_rate\"]}  '
      f'pnl={agg[\"simulated_pnl\"][\"total_pnl_pct\"]}%')
"
```

### Compare ensemble vs single-model

```bash
# Side-by-side
python3 -c "
import json
ens = json.load(open('models/chronos/_shadow/ensemble/SUMMARY.json'))['accumulated']['aggregate']
ph2 = json.load(open('models/chronos/_shadow/phase2_v2/SUMMARY.json'))['accumulated']['aggregate']
e05 = json.load(open('models/chronos/_shadow/E05_p3_coldstart_seed42/SUMMARY.json'))['accumulated']['aggregate']
print(f'                ensemble  phase2_v2  E05_p3')
print(f'hit_rate        {ens[\"hit_rate\"][\"hit_rate\"]:.3f}      {ph2[\"hit_rate\"][\"hit_rate\"]:.3f}      {e05[\"hit_rate\"][\"hit_rate\"]:.3f}')
print(f'pnl_pct         {ens[\"simulated_pnl\"][\"total_pnl_pct\"]:.1f}      {ph2[\"simulated_pnl\"][\"total_pnl_pct\"]:.1f}      {e05[\"simulated_pnl\"][\"total_pnl_pct\"]:.1f}')
print(f'mase            {ens[\"mase_mean\"]:.3f}      {ph2[\"mase_mean\"]:.3f}      {e05[\"mase_mean\"]:.3f}')
"
```

### Re-run for a missed day

```bash
# If cron failed on a weekday, fill the gap manually
TZ=America/New_York TODAY_OVERRIDE=2026-06-30 /home/$(whoami)/Stockapp/v12/scripts/run_shadow_ensemble.sh
```

### View the fallback rate trend over time

```bash
# Count fallbacks per day (catches a model that started disagreeing)
for d in $(ls models/chronos/_shadow/ensemble/[0-9]*.json | sort); do
    n=$(python3 -c "import json; print(sum(1 for r in json.load(open('$d'))['rows'] if r['used_fallback']))")
    echo "$(basename $d .json)  fallbacks=$n"
done
```

---

## 8. Failure modes & troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Today's JSON missing but log shows exit=0 | DST transition (spring-forward) or cron daemon not running | `systemctl status cron`; check log timestamp |
| `n_used_fallback == 10` for several days in a row | Model corruption (one regressed); spread is consistently >3% | Re-validate the regressed model; check if the seed drift is the cause |
| `hit_rate` drops suddenly on a single day | Market regime shift (not model problem) | Cross-check with single-model shadow — if both regress, regime; if only ensemble regresses, ensemble problem |
| `mase_mean` 5x larger than usual | Forecast horizon mismatch (data window too short) | Check `cutoff_days_ago` is still 1; verify parquet freshness |
| Cron log says `FileNotFoundError: model dir missing` | Model dir renamed or moved | Update `CHRONOS_ENSEMBLE_MODEL_PATHS` in the cron's env block |
| Ensemble hits `n_pairs` of 0 | All actuals are null (data feed broken) | Cross-check single-model shadow — same root cause |

---

## 9. Reference: where each number comes from

| Metric | Source | Formula |
|--------|--------|---------|
| `hit_rate` | `eval_chronos_h2h_v3.hit_rate()` | `n_pairs where sign(forecast) == sign(actual) / n_pairs` |
| `signal_hit_rate` | `eval_chronos_h2h_v3.signal_hit_rate()` | Same as `hit_rate` but restricted to `|forecast| > threshold` |
| `mase_mean` | `eval_chronos_h2h_v3.compute_mase()` | Mean Absolute Scaled Error per (model, symbol) |
| `simulated_pnl` | `shadow_trade.simulate_pnl()` | `sum(sign(forecast) × actual_ret_pct) for |forecast| > threshold` |
| `spread_pcts` | `shadow_ensemble.aggregate_ensemble_row` | `max(per_model_forecast_ret_pct) - min(...)` per (symbol, day) |
| `fallback_count` | `shadow_ensemble.aggregate_ensemble_row` | Number of rows where `used_fallback == true` |

---

## 10. Related docs

- `v12/docs/Stage-2x-Round3-Chronos-MultiSeed-Results.md` — Round 3
  closure: ensemble design, validation, Tier 2 wiring decision
- `v12/scripts/validate_ensemble_gate.py` — Backtest validation
  (frozen baseline; do not re-run on live data)
- `v12/src/stockapp_v12/services/ensemble_chronos_backend.py` — The
  production code path; this runbook only validates the data flow
- `v12/src/services/lifecycle_scheduler.py` (~line 648) — Where the
  ensemble backend is wired into Tier 2

---

## 11. NUC deployment (2026-07-01 move from GPU box)

**Why move:** GPU box (`user@<gpu-host>`) is frequently off and not
user-controlled. Silent cron failure is worse than slow CPU inference.

**What the move does:** replaces all hard-coded `/home/trainer/Stockapp/`
paths with `STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"`,
which auto-detects the runtime user's home (works on both `trainer` and
`app`).

### NUC setup (one-time, on the NUC)

```bash
# 1. SSH in
ssh app@nuc   # or whatever alias you have

# 2. Clone repo (NUC user is `app`, so /home/app/Stockapp)
cd ~
git clone git@github.com:<your-github-user>/StockV12-public.git   # or use HTTPS
cd Stockapp
git checkout main
git pull

# 3. Create in-repo venv (PEP 405) — avoids external ~/yolo_train_env dep
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install autogluon.timeseries pandas numpy pyarrow

# 4. Rsync the two model dirs from GPU box (one-time, ~500MB total)
#    GPU box is the source of truth for fine-tuned checkpoints.
rsync -avz --progress trainer@gpu:/home/trainer/Stockapp/models/chronos/phase2_v2 \
    ~/Stockapp/models/chronos/
rsync -avz --progress trainer@gpu:/home/trainer/Stockapp/models/chronos/E05_p3_coldstart_seed42 \
    ~/Stockapp/models/chronos/

# 5. Smoke test (manual run; no cron yet)
chmod +x v12/scripts/run_shadow_ensemble.sh v12/scripts/install_shadow_ensemble_cron.sh
TZ=America/New_York v12/scripts/run_shadow_ensemble.sh
# Expect: per-date JSON + SUMMARY.json under models/chronos/_shadow/ensemble/

# 6. Install the two crons (idempotent; safe to re-run)
v12/scripts/install_shadow_cron.sh           # 16:05 NYC single-model
v12/scripts/install_shadow_ensemble_cron.sh # 16:10 NYC ensemble

# 7. Verify crontab
crontab -l | grep -E "(TZ=|run_shadow)"
```

### GPU box decommission (optional, after 1 week of NUC shadow evidence)

Once NUC has 5+ trading days of clean shadow data, the GPU box crons can
be removed (still keep the GPU box for training the next variant — it's
just not the shadow host anymore).

```bash
# On GPU box
crontab -l | grep -v "run_shadow" | crontab -
# (Preserves other entries; only removes shadow-related lines.)
```

### Cost / capability comparison

| | GPU box (RTX 3060) | NUC (CPU only) |
|---|---|---|
| Per-symbol predict | ~2s | ~5-10s |
| Daily total (10 syms × 2 models) | ~40s | ~2-3 min |
| Reliability | Bad (frequently off) | Good (always on) |
| Setup | Done | One-time venv + rsync |

The 2-3 min daily cost is acceptable — cron fires at 16:10 NYC and
finishes well before any downstream consumer (paper trading at 16:30
if/when promoted).
