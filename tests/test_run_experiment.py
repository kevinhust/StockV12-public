"""TDD tests for v12/scripts/run_experiment.py — experiment runner pure functions.

Stage 2.x of v12/docs/V3_LESSONS_AND_EXP_PLAN.md. Tests cover only the pure
functions: config loading, seed-sweep expansion, run-id resolution, metrics
aggregation across seeds, promotion-gate evaluation, index dashboard updates.

Round 2 P0 added 3 tests for ``run_training`` cmd construction (seed + base_model
plumbing). We mock ``_run_subprocess`` and ``_resolve_model_dir_for_run`` so the
subprocess-bound orchestration can be unit-tested without a GPU.

The end-to-end orchestration is exercised manually on the GPU box.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


# ── load_config ──────────────────────────────────────────────────────────
class TestLoadConfig:
    def _write(self, tmp: Path, body: dict) -> Path:
        p = tmp / "exp.yaml"
        p.write_text(yaml.safe_dump(body))
        return p

    def test_minimal_valid_config_loads(self, tmp_path):
        from scripts.run_experiment import load_config
        body = {
            "exp_id": "E01",
            "name": "test",
            "description": "x",
            "hypothesis": "y",
            "train": {"base_model": "phase2_v2", "fine_tune_steps": 1000},
            "eval": {
                "smoke": {"enabled": True, "symbols": ["AAPL"], "cutoffs": [10]},
                "full": {"enabled": True, "symbols": None, "cutoffs": [10, 20]},
            },
            "output": {"base_dir": str(tmp_path / "exp")},
        }
        cfg = load_config(self._write(tmp_path, body))
        assert cfg["exp_id"] == "E01"
        assert cfg["train"]["fine_tune_steps"] == 1000

    def test_missing_required_field_raises(self, tmp_path):
        from scripts.run_experiment import load_config, ConfigValidationError
        body = {"exp_id": "E01"}  # missing name, train, eval, output
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(self._write(tmp_path, body))
        # All 4 missing required top-level keys should appear.
        msg = str(exc_info.value).lower()
        assert "name" in msg
        assert "train" in msg
        assert "eval" in msg
        assert "output" in msg

    def test_missing_file_raises(self, tmp_path):
        from scripts.run_experiment import load_config, ConfigValidationError
        with pytest.raises(ConfigValidationError, match="not found"):
            load_config(tmp_path / "does_not_exist.yaml")


# ── resolve_run_id ───────────────────────────────────────────────────────
class TestResolveRunId:
    def test_no_seed_returns_exp_id(self):
        from scripts.run_experiment import resolve_run_id
        cfg = {"exp_id": "E01"}
        assert resolve_run_id(cfg, seed=None) == "E01"

    def test_with_seed_includes_seed(self):
        from scripts.run_experiment import resolve_run_id
        cfg = {"exp_id": "E04"}
        assert resolve_run_id(cfg, seed=42) == "E04_seed42"

    def test_seed_zero_is_included(self):
        # edge case: seed=0 must still appear (not treated as None)
        from scripts.run_experiment import resolve_run_id
        cfg = {"exp_id": "E01"}
        assert resolve_run_id(cfg, seed=0) == "E01_seed0"


# ── expand_seed_sweep ───────────────────────────────────────────────────
class TestExpandSeedSweep:
    def test_no_sweep_returns_single_config(self):
        from scripts.run_experiment import expand_seed_sweep
        cfg = {"exp_id": "E02", "train": {"seed": 42}}
        runs = expand_seed_sweep(cfg)
        assert len(runs) == 1
        assert runs[0]["seed"] == 42

    def test_sweep_disabled_returns_single(self):
        from scripts.run_experiment import expand_seed_sweep
        cfg = {
            "exp_id": "E02",
            "train": {"seed": 42},
            "seed_sweep": {"enabled": False, "seeds": [1, 2, 3]},
        }
        runs = expand_seed_sweep(cfg)
        assert len(runs) == 1
        assert runs[0]["seed"] == 42

    def test_sweep_enabled_returns_one_per_seed(self):
        from scripts.run_experiment import expand_seed_sweep
        cfg = {
            "exp_id": "E01",
            "train": {"seed": 42},
            "seed_sweep": {"enabled": True, "seeds": [42, 43, 44, 45, 46]},
        }
        runs = expand_seed_sweep(cfg)
        assert len(runs) == 5
        assert [r["seed"] for r in runs] == [42, 43, 44, 45, 46]
        # Each run has a unique run_id (resolve_run_id)
        assert [r["run_id"] for r in runs] == [
            "E01_seed42", "E01_seed43", "E01_seed44", "E01_seed45", "E01_seed46",
        ]

    def test_sweep_overrides_train_seed(self):
        from scripts.run_experiment import expand_seed_sweep
        cfg = {
            "exp_id": "E01",
            "train": {"seed": 999},  # ignored when sweep enabled
            "seed_sweep": {"enabled": True, "seeds": [1, 2]},
        }
        runs = expand_seed_sweep(cfg)
        assert [r["seed"] for r in runs] == [1, 2]


# ── aggregate_seed_metrics ──────────────────────────────────────────────
class TestAggregateSeedMetrics:
    def test_empty_list_raises(self):
        from scripts.run_experiment import aggregate_seed_metrics
        with pytest.raises(ValueError, match="empty"):
            aggregate_seed_metrics([])

    def test_single_seed_returns_zero_std(self):
        from scripts.run_experiment import aggregate_seed_metrics
        reports = [{
            "seed": 42,
            "metrics": {"hit_rate": 0.30, "mase": 2.51, "signal_hit_rate": 0.31},
        }]
        agg = aggregate_seed_metrics(reports)
        assert agg["n_seeds"] == 1
        assert agg["hit_rate_mean"] == 0.30
        assert agg["hit_rate_std"] == 0.0
        assert agg["mase_mean"] == 2.51
        assert agg["mase_std"] == 0.0

    def test_multiple_seeds_computes_mean_and_std(self):
        from scripts.run_experiment import aggregate_seed_metrics
        reports = [
            {"seed": 42, "metrics": {"hit_rate": 0.30, "mase": 2.51, "signal_hit_rate": 0.31}},
            {"seed": 43, "metrics": {"hit_rate": 0.34, "mase": 2.48, "signal_hit_rate": 0.30}},
            {"seed": 44, "metrics": {"hit_rate": 0.28, "mase": 2.55, "signal_hit_rate": 0.33}},
            {"seed": 45, "metrics": {"hit_rate": 0.32, "mase": 2.50, "signal_hit_rate": 0.32}},
            {"seed": 46, "metrics": {"hit_rate": 0.36, "mase": 2.47, "signal_hit_rate": 0.29}},
        ]
        agg = aggregate_seed_metrics(reports)
        assert agg["n_seeds"] == 5
        # hit_rate mean = (0.30+0.34+0.28+0.32+0.36)/5 = 0.32
        assert agg["hit_rate_mean"] == pytest.approx(0.32, abs=1e-4)
        # std should be positive (sample std, ddof=1)
        assert agg["hit_rate_std"] > 0
        # std should be small (these are noise-floor values)
        assert agg["hit_rate_std"] < 0.05

    def test_missing_metric_in_one_report_raises(self):
        from scripts.run_experiment import aggregate_seed_metrics
        reports = [
            {"seed": 42, "metrics": {"hit_rate": 0.30, "mase": 2.51}},
            {"seed": 43, "metrics": {"hit_rate": 0.34}},  # missing mase
        ]
        with pytest.raises(KeyError):
            aggregate_seed_metrics(reports)


# ── check_promotion_gate ────────────────────────────────────────────────
class TestCheckPromotionGate:
    BASE = {
        "min_hit_rate": 0.55,
        "max_mase_delta_pct_vs_v2": 5.0,
        "v2_baseline_mase": 2.5152,
        "max_hit_rate_std": 0.05,
    }

    def test_clears_when_hit_rate_above_floor(self):
        from scripts.run_experiment import check_promotion_gate
        result = check_promotion_gate(
            aggregated={"hit_rate_mean": 0.60, "mase_mean": 2.50, "hit_rate_std": 0.03},
            gate=self.BASE,
        )
        assert result["cleared"] is True
        # When cleared, reasons must be empty — no failure tokens leak through.
        assert result["reasons"] == []
        assert not any("hit_rate" in r.lower() for r in result["reasons"])

    def test_fails_when_hit_rate_below_floor(self):
        from scripts.run_experiment import check_promotion_gate
        result = check_promotion_gate(
            aggregated={"hit_rate_mean": 0.40, "mase_mean": 2.50, "hit_rate_std": 0.03},
            gate=self.BASE,
        )
        assert result["cleared"] is False
        assert any("hit_rate" in r.lower() for r in result["reasons"])

    def test_fails_when_mase_too_much_worse_than_v2(self):
        from scripts.run_experiment import check_promotion_gate
        result = check_promotion_gate(
            aggregated={"hit_rate_mean": 0.60, "mase_mean": 2.80, "hit_rate_std": 0.03},
            gate=self.BASE,
        )
        # v2 baseline 2.5152, threshold +5% = 2.6409. mase=2.80 fails.
        assert result["cleared"] is False

    def test_fails_when_reproducibility_too_noisy(self):
        from scripts.run_experiment import check_promotion_gate
        result = check_promotion_gate(
            aggregated={"hit_rate_mean": 0.60, "mase_mean": 2.50, "hit_rate_std": 0.10},
            gate=self.BASE,
        )
        assert result["cleared"] is False
        assert any("std" in r.lower() or "reproducibility" in r.lower() for r in result["reasons"])

    def test_gate_optional_fields_default_to_permissive(self):
        from scripts.run_experiment import check_promotion_gate
        # No max_mase_delta, no max_hit_rate_std → only hit_rate floor checked
        result = check_promotion_gate(
            aggregated={"hit_rate_mean": 0.60, "mase_mean": 99.0, "hit_rate_std": 0.50},
            gate={"min_hit_rate": 0.55},
        )
        assert result["cleared"] is True


# ── update_index ─────────────────────────────────────────────────────────
class TestUpdateIndex:
    def test_create_new_index_with_first_entry(self, tmp_path):
        from scripts.run_experiment import update_index
        idx_path = tmp_path / "index.json"
        entry = {
            "exp_id": "E01",
            "name": "5-seed v2 baseline",
            "status": "complete",
            "date": "2026-06-29",
            "training_gpu_minutes": 25,
            "full": {"hit_rate_mean": 0.30, "hit_rate_std": 0.04,
                     "mase_mean": 2.51, "mase_std": 0.03},
            "promotion_gate": {"cleared": False, "reasons": ["hit_rate 0.30 < 0.55"]},
        }
        idx = update_index(idx_path, entry)
        assert len(idx["experiments"]) == 1
        assert idx["experiments"][0]["exp_id"] == "E01"
        # Persisted to disk
        loaded = json.loads(idx_path.read_text())
        assert loaded["experiments"][0]["exp_id"] == "E01"

    def test_update_existing_entry_replaces(self, tmp_path):
        from scripts.run_experiment import update_index
        idx_path = tmp_path / "index.json"
        idx_path.write_text(json.dumps({"experiments": [
            {"exp_id": "E01", "status": "in_progress", "name": "old"},
        ]}))
        new_entry = {"exp_id": "E01", "status": "complete", "name": "new"}
        idx = update_index(idx_path, new_entry)
        assert len(idx["experiments"]) == 1
        assert idx["experiments"][0]["status"] == "complete"
        assert idx["experiments"][0]["name"] == "new"

    def test_render_index_md_contains_key_columns(self, tmp_path):
        from scripts.run_experiment import update_index, render_index_md
        idx_path = tmp_path / "index.json"
        idx = update_index(idx_path, {
            "exp_id": "E01",
            "name": "5-seed v2 baseline",
            "status": "complete",
            "date": "2026-06-29",
            "training_gpu_minutes": 25,
            "full": {"hit_rate_mean": 0.30, "hit_rate_std": 0.04, "mase_mean": 2.51},
            "promotion_gate": {"cleared": False, "reasons": ["hit_rate 0.30 < 0.55"]},
        })
        md = render_index_md(idx)
        assert "| E01 |" in md
        assert "5-seed v2 baseline" in md
        assert "0.30" in md
        assert "2.51" in md
        assert "FAIL" in md or "fail" in md.lower()

    def test_render_index_md_sorts_by_hit_rate_descending(self, tmp_path):
        from scripts.run_experiment import update_index, render_index_md
        idx_path = tmp_path / "index.json"
        # Add E01 (low hit_rate), then E02 (high hit_rate)
        idx = update_index(idx_path, {
            "exp_id": "E01", "name": "low", "status": "complete", "date": "2026-06-29",
            "training_gpu_minutes": 10,
            "full": {"hit_rate_mean": 0.20, "mase_mean": 2.50},
            "promotion_gate": {"cleared": False, "reasons": []},
        })
        idx = update_index(idx_path, {
            "exp_id": "E07", "name": "high", "status": "complete", "date": "2026-06-29",
            "training_gpu_minutes": 30,
            "full": {"hit_rate_mean": 0.55, "mase_mean": 2.40},
            "promotion_gate": {"cleared": True, "reasons": []},
        })
        md = render_index_md(idx)
        # E07 (0.55) should appear before E01 (0.20)
        e07_pos = md.find("| E07 |")
        e01_pos = md.find("| E01 |")
        assert e07_pos < e01_pos


# ── config schema defaults ───────────────────────────────────────────────
class TestConfigDefaults:
    def test_apply_defaults_fills_missing_optional_keys(self):
        from scripts.run_experiment import apply_config_defaults
        cfg = {
            "exp_id": "E01",
            "name": "x",
            "train": {"fine_tune_steps": 1000},
            "eval": {"smoke": {"enabled": True}, "full": {"enabled": True}},
            "output": {"base_dir": "/tmp"},
        }
        out = apply_config_defaults(cfg)
        # Defaults filled in
        assert out["train"]["learning_rate"] is None
        assert out["train"]["warm_init_from"] is None
        assert out["train"]["context_length"] is None
        assert out["train"]["seed"] == 42  # default
        assert out["seed_sweep"]["enabled"] is False
        assert out["seed_sweep"]["seeds"] == [42]
        assert out["promotion_gate"]["min_hit_rate"] == 0.55
        assert out["promotion_gate"]["v2_baseline_mase"] == 2.5152

    def test_apply_defaults_preserves_user_values(self):
        from scripts.run_experiment import apply_config_defaults
        cfg = {
            "exp_id": "E01",
            "name": "x",
            "train": {"fine_tune_steps": 5000, "learning_rate": 1e-5, "seed": 99},
            "eval": {"smoke": {}, "full": {}},
            "output": {"base_dir": "/tmp"},
            "seed_sweep": {"enabled": True, "seeds": [1, 2, 3]},
        }
        out = apply_config_defaults(cfg)
        assert out["train"]["learning_rate"] == 1e-5
        assert out["train"]["fine_tune_steps"] == 5000
        assert out["seed_sweep"]["seeds"] == [1, 2, 3]

# ── run_training cmd construction (Round 2 P0) ───────────────────────────
# These tests verify that the runner propagates YAML `seed` and `base_model`
# to the trainer subprocess via --seed and --base-model flags. Previously
# the trainer ignored seed (collapsed 5-seed sweeps to n=1) and hardcoded
# base_model to autogluon/chronos-bolt-small.
class TestRunTrainingCmd:
    """Verify run_training() builds the right subprocess command."""

    def _patch_run_subprocess(self, monkeypatch, captured):
        """Replace _run_subprocess with a recorder that always returns 0."""
        from scripts import run_experiment as rxe
        def fake(cmd, log_path):
            captured["cmd"] = list(cmd)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("(mocked)")
            return 0
        monkeypatch.setattr(rxe, "_run_subprocess", fake)

    def _patch_resolve(self, monkeypatch, tmp_path):
        """Make _resolve_model_dir_for_run return a tmp dir so file IO works."""
        from scripts import run_experiment as rxe
        def fake_resolve(run_cfg, run_id):
            d = tmp_path / "models" / run_id
            d.mkdir(parents=True, exist_ok=True)
            return d
        monkeypatch.setattr(rxe, "_resolve_model_dir_for_run", fake_resolve)

    def test_seed_passed_to_trainer_cmd(self, tmp_path, monkeypatch):
        from scripts import run_experiment as rxe
        captured = {}
        self._patch_run_subprocess(monkeypatch, captured)
        self._patch_resolve(monkeypatch, tmp_path)
        run_cfg = {
            "exp_id": "E01",
            "train": {
                "fine_tune_steps": 1000,
                "learning_rate": 1e-5,
                "batch_size": 16,
                "context_length": 512,
                "seed": 42,
                "base_model": "autogluon/chronos-bolt-small",
            },
        }
        model_dir, rc, elapsed = rxe.run_training(run_cfg, "E01_seed42")
        assert rc == 0
        assert "--seed" in captured["cmd"]
        # Find the value right after --seed
        idx = captured["cmd"].index("--seed")
        assert captured["cmd"][idx + 1] == "42"

    def test_base_model_passed_to_trainer_cmd(self, tmp_path, monkeypatch):
        from scripts import run_experiment as rxe
        captured = {}
        self._patch_run_subprocess(monkeypatch, captured)
        self._patch_resolve(monkeypatch, tmp_path)
        run_cfg = {
            "exp_id": "E04",
            "train": {
                "fine_tune_steps": 1000,
                "learning_rate": 1e-5,
                "batch_size": 16,
                "context_length": 512,
                "seed": 42,
                "base_model": "phase2_v2",
            },
        }
        model_dir, rc, elapsed = rxe.run_training(run_cfg, "E04_seed42")
        assert rc == 0
        assert "--base-model" in captured["cmd"]
        idx = captured["cmd"].index("--base-model")
        assert captured["cmd"][idx + 1] == "phase2_v2"

    def test_zero_step_short_circuit_skips_subprocess(self, tmp_path, monkeypatch):
        """steps=0 must NOT spawn a trainer subprocess — it short-circuits
        to the base model on disk. This is the E04 warm-init path."""
        from scripts import run_experiment as rxe
        captured = {}
        def fail_if_called(cmd, log_path):
            raise AssertionError(
                f"_run_subprocess should not be called when steps=0, got {cmd}"
            )
        monkeypatch.setattr(rxe, "_run_subprocess", fail_if_called)

        # Create a fake phase2_v2 model dir so the short-circuit finds it.
        models_dir = tmp_path / "models" / "chronos" / "phase2_v2"
        models_dir.mkdir(parents=True)
        # Point MODELS_CHRONOS at our tmp dir for this test.
        monkeypatch.setattr(rxe, "MODELS_CHRONOS", tmp_path / "models" / "chronos")

        run_cfg = {
            "exp_id": "E04",
            "train": {
                "fine_tune_steps": 0,
                "seed": 42,
                "base_model": "phase2_v2",
            },
        }
        model_dir, rc, elapsed = rxe.run_training(run_cfg, "E04_seed42")
        assert rc == 0
        assert model_dir == models_dir  # short-circuited to base model directly
