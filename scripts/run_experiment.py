#!/usr/bin/env python3
"""
run_experiment.py — multi-config experiment runner for Stage 2.x.

Each YAML config declares ONE training configuration plus its smoke + full H2H
eval gates and a promotion gate. This runner:

  1. Loads YAML, validates required fields, applies defaults.
  2. Expands optional seed sweep → N runs (one per seed).
  3. For each run: fine-tune (or zero-shot) → smoke H2H → full H2H.
  4. Aggregates per-seed metrics into mean/std.
  5. Checks promotion gate (hit_rate floor + MASE-vs-v2 + reproducibility).
  6. Writes per-run report + aggregated.json + updates index.json /
     experiment_dashboard.md so results can be compared at a glance.

Design notes:
  - Pure functions (load_config, resolve_run_id, expand_seed_sweep,
    aggregate_seed_metrics, check_promotion_gate, update_index,
    render_index_md, apply_config_defaults) are fully unit-tested in
    tests/test_run_experiment.py. The orchestration that calls
    subprocess is exercised manually on the GPU box.
  - Designed for the GPU box's WSL2/CUDA env (CUDA_VISIBLE_DEVICES=0,
    nice for politeness). batch_size default 16; AutoGluon Chronos picks
    device via torch.cuda.is_available().
  - Per-run GPU budget: <8 min smoke + <20 min full ≈ 30 min max per run,
    so 8 experiments × 5 seeds can fit in ~20 GPU-hours across a day.

Usage:
  PYTHONPATH=src:scripts:. python3 scripts/run_experiment.py \\
      --config configs/experiments/exp_E01_v2_baseline_5seed.yaml \\
      --phase all                      # all = train + smoke + full + aggregate
  # --phase train|smoke|full|aggregate|all
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Repo paths (works on both local + GPU box; eval script uses same idiom).
REPO = Path(__file__).resolve().parents[1]
# Post-migration: v12/ promoted to root, scripts live at repo root.
SCRIPTS_DIR = REPO / "scripts"
MODELS_CHRONOS = REPO / "models" / "chronos"
EXPERIMENTS_ROOT = MODELS_CHRONOS / "_experiments"
INDEX_PATH = EXPERIMENTS_ROOT / "index.json"
INDEX_MD_PATH = EXPERIMENTS_ROOT / "experiment_dashboard.md"

# Promotion gate default floor — Stage-1 gate from v12/docs/V3_POSTMORTEM_AND_TRAINING_PLAN.md.
# Crosses (forecast, actual) need sign agreement ≥ 0.53 to claim any directional
# edge (0.50 = coin flip). Empirical: phase2_v2 already sits at 0.30 → ANY
# candidate that wants to displace v2 must visibly beat it on this metric, not
# just on MASE.
DEFAULT_MIN_HIT_RATE = 0.55   # v2 itself fails this (0.30), so floor is strict
DEFAULT_V2_BASELINE_MASE = 2.5152  # measured in v12/docs/H2H_V3_REPORT.md
DEFAULT_MAX_MASE_DELTA_PCT = 5.0   # allow up to +5% worse MASE if sign edge large
DEFAULT_MAX_HIT_RATE_STD = 0.05    # reproducibility: std across seeds ≤ 0.05
DEFAULT_TRAIN_SEED = 42
DEFAULT_SIGNAL_HIT_RATE_FLOOR = 0.55

# Train/H2H call envelopes (kept here so tests don't need to shell out).
PYTHON_ENV = "PYTHONPATH=src:scripts:."


# ---------------------------------------------------------------------------
# Pure helpers — unit-tested in v12/tests/test_run_experiment.py
# ---------------------------------------------------------------------------

class ConfigValidationError(ValueError):
    """Raised when a YAML config is missing required fields or unreadable."""


_REQUIRED_KEYS = ("exp_id", "name", "description", "hypothesis",
                  "train", "eval", "output")


def load_config(path: Path) -> dict:
    """Read a YAML config, validate required fields, return dict.

    Raises:
      ConfigValidationError: if file is missing, unreadable, or missing any
        of the required top-level keys. The exception message enumerates the
        missing keys so the operator can fix the config quickly.
    """
    import yaml

    if not path.exists():
        raise ConfigValidationError(f"config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except Exception as e:
        raise ConfigValidationError(f"failed to parse YAML {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigValidationError(f"config {path} is not a YAML mapping")

    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"config {path.name} missing required keys: {missing}"
        )
    return raw


def resolve_run_id(cfg: dict, seed: Optional[int]) -> str:
    """Compose a run id from exp_id (+ optional seed).

    seed=None → just exp_id. seed=int (including 0) → "<exp_id>_seed<N>".
    """
    base = cfg["exp_id"]
    if seed is None:
        return base
    return f"{base}_seed{int(seed)}"


def expand_seed_sweep(cfg: dict) -> list[dict]:
    """Return one run-spec dict per (exp, seed). Each includes run_id + seed.

    Sweep is enabled by `seed_sweep.enabled: true` AND `seeds` non-empty.
    When disabled (or absent), returns a single run using `train.seed`.
    """
    train_seed = (cfg.get("train") or {}).get("seed", DEFAULT_TRAIN_SEED)
    sweep = cfg.get("seed_sweep") or {}
    if sweep.get("enabled") and sweep.get("seeds"):
        seeds = list(sweep["seeds"])
    else:
        seeds = [train_seed]
    runs = []
    for s in seeds:
        # Shallow-copy cfg so per-run mutations (model_dir, log paths) are
        # safe — but keep the original cfg immutable for the next caller.
        from copy import deepcopy
        run_cfg = deepcopy(cfg)
        run_cfg["train"]["seed"] = int(s)
        runs.append({
            "seed": int(s),
            "run_id": resolve_run_id(cfg, s),
            "cfg": run_cfg,
        })
    return runs


def aggregate_seed_metrics(reports: list[dict]) -> dict:
    """Aggregate per-seed eval reports into mean/std + n_seeds.

    Each report must look like:
        {"seed": int, "metrics": {"hit_rate": float, "mase": float,
                                  "signal_hit_rate": float|None}}

    Returns:
        {
            "n_seeds": int,
            "hit_rate_mean": float, "hit_rate_std": float,
            "mase_mean": float, "mase_std": float,
            "signal_hit_rate_mean": float|None, "signal_hit_rate_std": float|None,
        }

    Raises:
        ValueError: if `reports` is empty.
        KeyError:    if any report is missing 'metrics' or a required metric.
    """
    if not reports:
        raise ValueError("aggregate_seed_metrics: empty reports list")
    metrics_keys = ("hit_rate", "mase", "signal_hit_rate")
    for r in reports:
        if "metrics" not in r:
            raise KeyError(f"report missing 'metrics': {r.get('seed')}")
        for k in metrics_keys:
            if k not in r["metrics"]:
                raise KeyError(f"seed {r.get('seed')} missing metric {k!r}")

    def _gather(key: str) -> list[float]:
        return [float(r["metrics"][key]) for r in reports if r["metrics"].get(key) is not None]

    import numpy as np
    hr = _gather("hit_rate")
    mase = _gather("mase")
    shr = _gather("signal_hit_rate")

    def _mean_std(xs: list[float]) -> tuple[Optional[float], Optional[float]]:
        if not xs:
            return None, None
        arr = np.asarray(xs, dtype=float)
        return (
            round(float(arr.mean()), 4),
            round(float(arr.std(ddof=1)) if len(arr) > 1 else 0.0, 4),
        )

    hr_mean, hr_std = _mean_std(hr)
    mase_mean, mase_std = _mean_std(mase)
    shr_mean, shr_std = _mean_std(shr)

    return {
        "n_seeds": len(reports),
        "hit_rate_mean": hr_mean,
        "hit_rate_std": hr_std,
        "mase_mean": mase_mean,
        "mase_std": mase_std,
        "signal_hit_rate_mean": shr_mean,
        "signal_hit_rate_std": shr_std,
    }


def check_promotion_gate(aggregated: dict, gate: dict) -> dict:
    """Decide whether `aggregated` clears the promotion gate.

    Gate defaults (all optional):
      min_hit_rate: float            (hit_rate_mean must be ≥ this)
      min_signal_hit_rate: float     (signal_hit_rate_mean must be ≥ this,
                                      if signal_hit_rate was computed)
      max_mase_delta_pct_vs_v2: float (mase must not exceed
                                      v2_baseline_mase × (1 + pct/100))
      v2_baseline_mase: float        (defaults to 2.5152 measured on OOS)
      max_hit_rate_std: float        (hit_rate_std across seeds must be ≤ this)

    Returns:
        {"cleared": bool, "reasons": list[str]}

    Each failure adds a human-readable reason like
        "hit_rate 0.30 < 0.55"
        "mase 2.80 > 2.6409 (v2 × 1.050)"
        "hit_rate_std 0.10 > 0.05 (reproducibility)"
    """
    reasons: list[str] = []
    v2_mase = float(gate.get("v2_baseline_mase", DEFAULT_V2_BASELINE_MASE))

    # 1. hit_rate floor
    min_hr = gate.get("min_hit_rate")
    if min_hr is not None:
        hr_mean = aggregated.get("hit_rate_mean")
        if hr_mean is None or hr_mean < min_hr:
            reasons.append(
                f"hit_rate {hr_mean} < {min_hr}"
            )

    # 2. signal_hit_rate floor (only if measured)
    min_shr = gate.get("min_signal_hit_rate")
    if min_shr is not None:
        shr_mean = aggregated.get("signal_hit_rate_mean")
        if shr_mean is not None and shr_mean < min_shr:
            reasons.append(
                f"signal_hit_rate {shr_mean} < {min_shr}"
            )

    # 3. mase regression vs v2 baseline
    max_delta = gate.get("max_mase_delta_pct_vs_v2")
    if max_delta is not None:
        mase_mean = aggregated.get("mase_mean")
        if mase_mean is not None:
            threshold = v2_mase * (1.0 + max_delta / 100.0)
            if mase_mean > threshold:
                reasons.append(
                    f"mase {mase_mean:.4f} > {threshold:.4f} (v2 × {1 + max_delta/100:.3f})"
                )

    # 4. reproducibility — hit_rate std across seeds
    max_std = gate.get("max_hit_rate_std")
    if max_std is not None:
        hr_std = aggregated.get("hit_rate_std")
        if hr_std is not None and hr_std > max_std:
            reasons.append(
                f"hit_rate_std {hr_std:.4f} > {max_std} (reproducibility)"
            )

    return {"cleared": len(reasons) == 0, "reasons": reasons}


def update_index(idx_path: Path, entry: dict) -> dict:
    """Upsert `entry` into the experiment index JSON.

    Match key is `entry["exp_id"]`. Existing entries with the same exp_id
    are replaced (rather than duplicated). Returns the full in-memory idx.

    Side effect: writes idx_path atomically (write-to-tmp then rename) so a
    crash mid-write doesn't corrupt the dashboard.
    """
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text())
        except Exception:
            idx = {"experiments": []}
    else:
        idx = {"experiments": []}
    if "experiments" not in idx:
        idx = {"experiments": []}
    # Replace existing entry with same exp_id
    exp_id = entry.get("exp_id")
    idx["experiments"] = [
        e for e in idx["experiments"] if e.get("exp_id") != exp_id
    ]
    idx["experiments"].append(entry)
    idx["last_updated"] = datetime.now().isoformat(timespec="seconds")
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(idx_path.suffix + ".tmp")
    tmp.write_text(json.dumps(idx, indent=2, default=str))
    tmp.replace(idx_path)
    return idx


def render_index_md(idx: dict) -> str:
    """Render the index as a sortable markdown table.

    Rows are sorted by hit_rate_mean DESC so the top of the dashboard shows
    the most promising experiments first.

    Columns:
      exp_id | name | status | date | hit_rate (±std) | mase (±std) | n_seeds | gate
    """
    rows = list(idx.get("experiments", []))
    def _hr(e: dict) -> float:
        return (e.get("full") or {}).get("hit_rate_mean") or 0.0
    rows.sort(key=_hr, reverse=True)

    lines = [
        "# Chronos V12 — Experiment Dashboard",
        "",
        f"_Last updated: {idx.get('last_updated', 'unknown')}_  ",
        f"_Total experiments: {len(rows)}_",
        "",
        "Sorted by `hit_rate_mean` (descending). 0.50 = coin flip; Stage-1 gate "
        "is `hit_rate_mean >= 0.55` and `mase <= v2_baseline × 1.05` with "
        "`hit_rate_std <= 0.05`.",
        "",
        "| exp_id | name | status | date | hit_rate (±std) | mase (±std) | n_seeds | gate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in rows:
        full = e.get("full") or {}
        hr_m = full.get("hit_rate_mean")
        hr_s = full.get("hit_rate_std")
        mase_m = full.get("mase_mean")
        mase_s = full.get("mase_std")
        n_seeds = full.get("n_seeds", "—")
        hr_cell = f"{hr_m:.3f} ± {hr_s:.3f}" if hr_m is not None and hr_s is not None else (
            f"{hr_m:.3f}" if hr_m is not None else "—"
        )
        mase_cell = f"{mase_m:.3f} ± {mase_s:.3f}" if mase_m is not None and mase_s is not None else (
            f"{mase_m:.3f}" if mase_m is not None else "—"
        )
        gate_cell = "PASS" if (e.get("promotion_gate") or {}).get("cleared") else "FAIL"
        lines.append(
            f"| {e.get('exp_id', '?')} "
            f"| {e.get('name', '')} "
            f"| {e.get('status', '')} "
            f"| {e.get('date', '')} "
            f"| {hr_cell} "
            f"| {mase_cell} "
            f"| {n_seeds} "
            f"| {gate_cell} |"
        )
    # Footer: pin the v2 baseline and the gate thresholds so readers don't
    # have to dig through docs to know what the numbers mean.
    lines += [
        "",
        "## Baselines and gates",
        "",
        f"- v2 baseline MASE (OOS mean): **{DEFAULT_V2_BASELINE_MASE}**",
        f"- Stage-1 gate floor: `hit_rate_mean >= {DEFAULT_MIN_HIT_RATE}`",
        f"- MASE budget: `mase_mean <= v2 × 1.{int(DEFAULT_MAX_MASE_DELTA_PCT):02d}`",
        f"- Reproducibility: `hit_rate_std <= {DEFAULT_MAX_HIT_RATE_STD}`",
        "",
    ]
    return "\n".join(lines)


def apply_config_defaults(cfg: dict) -> dict:
    """Fill in optional-but-needed defaults so downstream code can rely on keys.

    Defaults applied:
      train.warm_init_from      = None
      train.learning_rate       = None  (train script picks its own 1e-5)
      train.context_length      = None
      train.seed                = 42
      seed_sweep.enabled        = False
      seed_sweep.seeds          = [42]
      promotion_gate.min_hit_rate           = 0.55
      promotion_gate.v2_baseline_mase       = 2.5152
      promotion_gate.max_mase_delta_pct_vs_v2 = 5.0
      promotion_gate.max_hit_rate_std       = 0.05
      eval.smoke.sign_threshold = 0.5
      eval.full.sign_threshold  = 0.5
    """
    from copy import deepcopy
    out = deepcopy(cfg)
    train = out.setdefault("train", {})
    train.setdefault("warm_init_from", None)
    train.setdefault("learning_rate", None)
    train.setdefault("context_length", None)
    train.setdefault("seed", DEFAULT_TRAIN_SEED)
    train.setdefault("regime_filter", None)

    seed_sweep = out.setdefault("seed_sweep", {"enabled": False, "seeds": [DEFAULT_TRAIN_SEED]})
    if not isinstance(seed_sweep.get("seeds"), list) or not seed_sweep["seeds"]:
        seed_sweep["seeds"] = [DEFAULT_TRAIN_SEED]

    gate = out.setdefault("promotion_gate", {})
    gate.setdefault("min_hit_rate", DEFAULT_MIN_HIT_RATE)
    gate.setdefault("v2_baseline_mase", DEFAULT_V2_BASELINE_MASE)
    gate.setdefault("max_mase_delta_pct_vs_v2", DEFAULT_MAX_MASE_DELTA_PCT)
    gate.setdefault("max_hit_rate_std", DEFAULT_MAX_HIT_RATE_STD)
    gate.setdefault("min_signal_hit_rate", None)

    for phase in ("smoke", "full"):
        ph = out.setdefault("eval", {}).setdefault(phase, {})
        ph.setdefault("enabled", False)
        ph.setdefault("symbols", None)  # None → use evaluator default
        ph.setdefault("cutoffs", [10])
        ph.setdefault("sign_threshold", 0.5)

    out.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    out.setdefault("status", "draft")
    return out


# ---------------------------------------------------------------------------
# Orchestration — calls subprocess; designed to run on the GPU box only.
# ---------------------------------------------------------------------------

def _run_subprocess(cmd: list[str], log_path: Path) -> int:
    """Run `cmd`, tee stdout+stderr to `log_path`, return exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as logf:
        proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    return proc.returncode


def _resolve_model_dir_for_run(run_cfg: dict, run_id: str) -> Path:
    """Where the AutoGluon predictor for this run lands on disk."""
    exp_id = run_cfg["exp_id"]
    name = run_cfg.get("name", exp_id).replace(" ", "_").replace("/", "_")
    return EXPERIMENTS_ROOT / f"{exp_id}_{name}" / run_id / "model"


def _extract_metrics_from_h2h(report_path: Path) -> dict:
    """Pull hit_rate / mase / signal_hit_rate from an H2H JSON report.

    The eval script writes one JSON containing multiple models / cutoffs;
    for an experiment we only eval ONE model, so we take the per-model
    entry. Across cutoffs, we average (this matches the terminal output
    of the H2H script — see eval_chronos_h2h_v3.py L555-575).
    """
    report = json.loads(report_path.read_text())
    # cross_cutoff_hit_rate gives us the mean across cutoffs for ONE model
    cross = report.get("cross_cutoff_hit_rate") or {}
    cross_mase = report.get("cross_cutoff_summary") or {}
    models = list(cross.keys())
    if len(models) != 1:
        # Defensive: experiment should run only one model
        raise ValueError(
            f"expected 1 model in H2H report, got {models}"
        )
    m = models[0]
    hr = cross[m].get("mean_hit_rate_across_cutoffs")
    shr = cross[m].get("mean_signal_hit_rate_across_cutoffs")
    mase = cross_mase[m].get("mean_mase_across_cutoffs")
    return {
        "hit_rate": hr,
        "mase": mase,
        "signal_hit_rate": shr,
    }


def run_training(run_cfg: dict, run_id: str) -> tuple[Path, int, float]:
    """Invoke train_chronos_baseline.py with this run's params.

    Returns (model_dir, exit_code, elapsed_sec).

    Special case: ``fine_tune_steps == 0`` means "no fresh training, use the
    base model as-is". We resolve ``base_model`` from the YAML (default
    ``phase2_v2``) against ``models/chronos/`` and return without invoking
    the trainer subprocess. The caller will still symlink it for H2H eval.
    """
    train = run_cfg["train"]
    steps = int(train["fine_tune_steps"])
    # Default matches trainer's original hardcoded value so YAMLs without
    # explicit `base_model` behave identically pre/post Round 2 P0 patch.
    base_model = train.get("base_model") or "autogluon/chronos-bolt-small"
    if steps == 0:
        # No-op training: point model_dir at the base model directly.
        model_dir = MODELS_CHRONOS / base_model
        if not model_dir.exists():
            print(
                f"[FAIL] {run_id} fine_tune_steps=0 but base model "
                f"{model_dir} missing", file=sys.stderr
            )
            return model_dir, 2, 0.0
        print(
            f"\n[train] run_id={run_id} steps=0 → reusing base model "
            f"{base_model} (no fresh fine-tune)",
            file=sys.stderr,
        )
        return model_dir, 0, 0.0

    out_dir = _resolve_model_dir_for_run(run_cfg, run_id)
    # Round 2 P0 plumbing: --seed (was previously absent) + --base-model
    # (was hardcoded to autogluon/chronos-bolt-small in trainer). Now both
    # are propagated so seed_sweep YAMLs actually sweep and warm-init
    # experiments honor YAML `base_model`.
    seed = int(train.get("seed", 123))
    base_model = train.get("base_model") or "autogluon/chronos-bolt-small"
    # Resolve short names (e.g. "phase2_v2") against models/chronos/ so AG
    # Chronos can load the local checkpoint. HF model IDs (containing "/")
    # and absolute paths are passed through unchanged.
    if "/" not in base_model and not base_model.startswith("/"):
        candidate = MODELS_CHRONOS / base_model
        if candidate.exists():
            # AG Chronos needs the inner fine-tuned-ckpt directory, not the
            # predictor root. Scan for it: models/Chronos[<base>]/W0/fine-tuned-ckpt/
            ft_ckpt = candidate / "models" / f"Chronos[{base_model.replace('/', '__')}]" / "W0" / "fine-tuned-ckpt"
            if not ft_ckpt.exists():
                # Fallback: glob for any Chronos fine-tuned-ckpt inside the predictor
                candidates = sorted((candidate / "models").glob("Chronos*/W0/fine-tuned-ckpt"))
                if candidates:
                    ft_ckpt = candidates[0]
            if ft_ckpt.exists():
                base_model = str(ft_ckpt.resolve())
            else:
                base_model = str(candidate.resolve())
        else:
            print(
                f"[WARN] {run_id} base_model={base_model} not in {MODELS_CHRONOS}, "
                f"passing short name to trainer (may fail if not a HF model ID)",
                file=sys.stderr,
            )
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "train_chronos_baseline.py"),
        "--mode", "finetune",
        "--steps", str(steps),
        "--batch-size", str(int(train.get("batch_size", 16))),
        "--context-length", str(int(train.get("context_length") or 512)),
        "--out", str(out_dir),
        "--seed", str(seed),
        "--base-model", base_model,
    ]
    if train.get("learning_rate"):
        cmd += ["--lr", str(float(train["learning_rate"]))]
    cmd += ["--device", "cuda"]
    log_path = out_dir.parent / f"{run_id}_train.log"
    print(f"\n[train] run_id={run_id} steps={steps} seed={seed} base_model={base_model}", file=sys.stderr)
    print("  $", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    t0 = time.time()
    rc = _run_subprocess(cmd, log_path)
    return out_dir, rc, time.time() - t0


def run_h2h(
    run_cfg: dict, run_id: str, model_dir: Path, phase: str,
) -> tuple[Path, int, float]:
    """Invoke eval_chronos_h2h_v3.py for this run's model.

    `phase` is "smoke" or "full". Returns (report_path, exit_code, elapsed_sec).

    Bridges the path-layout mismatch: our `_experiments/<exp>_<run>/<seed>/model/`
    layout cannot be passed to ``--models`` (which expects a directory directly
    under ``models/chronos/``), so we create a per-run symlink with a unique
    name (``<run_id>``) into the flat layout. The symlink stays in place after
    H2H completes so the model archive under ``_experiments/`` is the SSOT.
    """
    phase_cfg = (run_cfg.get("eval") or {}).get(phase) or {}
    out_dir = model_dir.parent
    symlink_path = MODELS_CHRONOS / run_id
    symlink_created = False
    if not symlink_path.exists():
        symlink_path.symlink_to(model_dir)
        symlink_created = True
    try:
        if phase == "smoke":
            report_path = out_dir / f"{run_id}_smoke_report.json"
            cmd = [
                sys.executable, str(SCRIPTS_DIR / "eval_chronos_h2h_v3.py"),
                "--models", run_id,
                "--out", str(report_path),
                "--smoke",
            ]
        else:
            report_path = out_dir / f"{run_id}_full_report.json"
            symbols = phase_cfg.get("symbols")
            cutoffs = phase_cfg.get("cutoffs") or [10, 20]
            cmd = [
                sys.executable, str(SCRIPTS_DIR / "eval_chronos_h2h_v3.py"),
                "--models", run_id,
                "--cutoffs", *[str(int(c)) for c in cutoffs],
                "--out", str(report_path),
            ]
            if symbols:
                cmd += ["--symbols", *symbols]

        log_path = out_dir / f"{run_id}_{phase}_h2h.log"
        print(f"\n[{phase}] run_id={run_id}", file=sys.stderr)
        print("  $", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
        t0 = time.time()
        rc = _run_subprocess(cmd, log_path)
        return report_path, rc, time.time() - t0
    finally:
        # Leave the symlink in place — _experiments/ is the SSOT archive and
        # removing the symlink would break any retry / dashboard re-render.
        # Future sweeps will overwrite this same path safely (it's unique per
        # <exp_id>_<seed>).
        if symlink_created and not symlink_path.is_symlink():
            pass  # someone else created it concurrently — fine


def orchestrate_experiment(cfg_path: Path, phases: list[str]) -> int:
    """End-to-end pipeline: load cfg → train per seed → smoke → full → aggregate → gate → index.

    phases: subset of {"train", "smoke", "full", "aggregate"}; "all" = all four.
    Returns the process exit code (0 = success, non-zero = at least one step failed).
    """
    raw_cfg = load_config(cfg_path)
    cfg = apply_config_defaults(raw_cfg)
    exp_id = cfg["exp_id"]
    print(f"\n=== exp {exp_id} — {cfg.get('name', '')} ===", file=sys.stderr)
    print(f"hypothesis: {cfg.get('hypothesis', '')}", file=sys.stderr)

    # Honor deferred_to_round: if the config is marked for a future round,
    # log + skip rather than silently running it with the wrong script.
    deferred = cfg.get("deferred_to_round")
    if deferred:
        reason = cfg.get("reason_for_deferral", "no reason given")
        print(
            f"[skip] {exp_id} deferred to round {deferred}: {reason}",
            file=sys.stderr,
        )
        # Still record a placeholder in the index so it shows up as "skipped".
        update_index(INDEX_PATH, {
            "exp_id": exp_id,
            "name": cfg.get("name"),
            "status": f"deferred_round_{deferred}",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "training_gpu_minutes": 0,
            "full": {},
            "promotion_gate": {"cleared": False, "reasons": [f"deferred to round {deferred}"]},
        })
        idx = json.loads(INDEX_PATH.read_text())
        INDEX_MD_PATH.write_text(render_index_md(idx))
        return 0  # not an error — operator chose to defer explicitly

    runs = expand_seed_sweep(cfg)
    print(f"  runs: {len(runs)}  (seed_sweep.enabled={cfg.get('seed_sweep', {}).get('enabled')})", file=sys.stderr)

    per_run_reports: list[dict] = []
    overall_rc = 0
    train_minutes_total = 0.0
    for spec in runs:
        run_cfg = spec["cfg"]
        run_id = spec["run_id"]
        seed = spec["seed"]

        # Phase: train
        if "train" in phases:
            model_dir, rc, elapsed = run_training(run_cfg, run_id)
            train_minutes_total += elapsed / 60.0
            if rc != 0:
                print(f"[FAIL] {run_id} train exited {rc}", file=sys.stderr)
                overall_rc = rc
                continue
        else:
            model_dir = _resolve_model_dir_for_run(run_cfg, run_id)

        # Phase: smoke
        smoke_report = None
        if "smoke" in phases and (cfg["eval"].get("smoke") or {}).get("enabled", True):
            smoke_path, rc, _ = run_h2h(run_cfg, run_id, model_dir, "smoke")
            if rc != 0:
                print(f"[FAIL] {run_id} smoke exited {rc}", file=sys.stderr)
                overall_rc = rc
                continue
            smoke_report = _extract_metrics_from_h2h(smoke_path)

        # Phase: full
        full_report = None
        if "full" in phases and (cfg["eval"].get("full") or {}).get("enabled", True):
            full_path, rc, _ = run_h2h(run_cfg, run_id, model_dir, "full")
            if rc != 0:
                print(f"[FAIL] {run_id} full exited {rc}", file=sys.stderr)
                overall_rc = rc
                continue
            full_report = _extract_metrics_from_h2h(full_path)

        per_run_reports.append({
            "seed": seed,
            "run_id": run_id,
            "model_dir": str(model_dir),
            "metrics": full_report or smoke_report or {},
        })

    # Phase: aggregate (across all seeds that completed)
    aggregated = None
    gate_result = None
    if "aggregate" in phases and per_run_reports:
        aggregated = aggregate_seed_metrics(per_run_reports)
        gate_result = check_promotion_gate(aggregated, cfg.get("promotion_gate") or {})
        print(f"\n[aggregate] n_seeds={aggregated['n_seeds']} "
              f"hit_rate={aggregated['hit_rate_mean']} ± {aggregated['hit_rate_std']} "
              f"mase={aggregated['mase_mean']} ± {aggregated['mase_std']}", file=sys.stderr)
        if gate_result["cleared"]:
            print(f"[gate] CLEARED — promotion gates passed", file=sys.stderr)
        else:
            print(f"[gate] FAILED — {'; '.join(gate_result['reasons'])}", file=sys.stderr)

    # Write summary + update index
    exp_dir = EXPERIMENTS_ROOT / f"{cfg['exp_id']}_{(cfg.get('name') or cfg['exp_id']).replace(' ', '_').replace('/', '_')}"
    exp_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "exp_id": exp_id,
        "name": cfg.get("name"),
        "description": cfg.get("description"),
        "hypothesis": cfg.get("hypothesis"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "training_gpu_minutes": round(train_minutes_total, 2),
        "config_path": str(cfg_path),
        "per_run_reports": per_run_reports,
        "full": aggregated,
        "promotion_gate": gate_result,
        "status": "complete" if overall_rc == 0 else "partial",
    }
    (exp_dir / "SUMMARY.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n[write] {exp_dir / 'SUMMARY.json'}", file=sys.stderr)

    if aggregated is not None:
        update_index(INDEX_PATH, {
            "exp_id": exp_id,
            "name": cfg.get("name"),
            "status": summary["status"],
            "date": summary["date"],
            "training_gpu_minutes": summary["training_gpu_minutes"],
            "full": aggregated,
            "promotion_gate": gate_result or {"cleared": False, "reasons": ["not run"]},
        })
        idx = json.loads(INDEX_PATH.read_text())
        INDEX_MD_PATH.write_text(render_index_md(idx))
        print(f"[write] {INDEX_PATH} + {INDEX_MD_PATH}", file=sys.stderr)

    return overall_rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, required=True,
                   help="Path to experiment YAML config")
    p.add_argument(
        "--phase", default="all",
        help=("One of {train, smoke, full, aggregate, all}. 'all' = train+smoke"
              "+full+aggregate. To skip train (e.g. resume after a crash) use "
              "'smoke,full,aggregate'.")
    )
    args = p.parse_args()

    phase_set = {"train", "smoke", "full", "aggregate"}
    if args.phase == "all":
        phases = ["train", "smoke", "full", "aggregate"]
    else:
        phases = [p.strip() for p in args.phase.split(",")]
        bad = [x for x in phases if x not in phase_set]
        if bad:
            print(f"ERROR: unknown phase(s): {bad}. Allowed: {sorted(phase_set)}",
                  file=sys.stderr)
            return 2

    return orchestrate_experiment(args.config, phases)


if __name__ == "__main__":
    raise SystemExit(main())
