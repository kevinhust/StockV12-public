# tests/e2e_champion_selection.py
"""End-to-end: synthetic shadow data → champion_evaluation → assert selection.

30 dates × 5 symbols per model = 150 trades per model (above n_trades gate).
actual_ret_pct varies per row to give nonzero daily_pnl pstdev → Sharpe > 1.0.

Model A → champion (highest Sharpe, positive PnL, high win_rate)
Model B → challenger (lower Sharpe, different hyperparam_family)
Model C → fails pnl_30d gate (consistent negative actuals)
"""
import json
import random
import subprocess
import sys
from pathlib import Path


# Deterministic for stable Sharpe values across runs.
random.seed(42)


def _write_shadow_json(path: Path, rows: list[dict], source: str = "backfill") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "model": path.parent.name,
        "eval_date": "2026-06-23",
        "rows": rows,
        "source": source,
    }))


def _gen_rows(actual_pool: list[float], mase: float = 1.0) -> list[dict]:
    """Generate 30 dates × 5 symbols = 150 rows cycling through actual_pool."""
    rows = []
    for d in range(1, 31):
        for s_i in range(5):
            actual = actual_pool[(d * 5 + s_i) % len(actual_pool)]
            rows.append({
                "symbol": f"SYM{s_i}",
                "forecast_ret_pct": 2.0,  # always above 0.5 threshold
                "actual_ret_pct": actual,
                "mase": mase,
                "eval_date": f"2026-06-{d:02d}",
                "eval_source": "backfill",
                "model": "X",
                "cutoff_days_ago": 1,
            })
    return rows


def test_e2e_evaluation_picks_top_model(tmp_path: Path):
    shadow_root = tmp_path / "shadow"

    # Model A: strong positive — expect champion
    _write_shadow_json(
        shadow_root / "A" / "2026-06-23.json",
        _gen_rows(actual_pool=[1.5, 1.0, 2.0, 1.5, 0.5, 0.8, 1.5, 2.5, 1.2, 0.9]),
    )
    # Model B: weaker positive — expect challenger (different family)
    _write_shadow_json(
        shadow_root / "B" / "2026-06-23.json",
        _gen_rows(actual_pool=[0.8, 0.6, 1.0, 0.4, 0.7, 0.5, 0.9, 0.3, 0.6, 0.4]),
    )
    # Model C: negative — fails pnl_30d gate
    _write_shadow_json(
        shadow_root / "C" / "2026-06-23.json",
        _gen_rows(actual_pool=[-1.0] * 10),  # consistent negative
    )

    result = subprocess.run(
        [sys.executable, "scripts/champion_evaluation.py",
         "--report", "--shadow-dir", str(shadow_root),
         "--models", "A", "B", "C"],
        capture_output=True, text=True,
        env={"PYTHONPATH": "src:scripts:.", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "Recommended Champion:   A" in result.stdout
    assert "Recommended Challenger: B" in result.stdout
