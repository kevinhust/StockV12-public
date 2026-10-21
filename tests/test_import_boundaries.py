"""Tests for AST-based import boundary checker (Task 3)."""
from __future__ import annotations

from pathlib import Path

from scripts.check_import_boundaries import Rule, find_violations, main

SAMPLE = """
from stockapp_v12.adapters.legacy_v11.chronos import LegacyChronosAdapter
"""


def test_finds_forbidden_adapters_import_in_pipeline(tmp_path: Path) -> None:
    rules = [
        Rule(
            name="pipeline_no_adapters",
            importer_glob="**/pipeline/**/*.py",
            forbidden_module_prefixes=("stockapp_v12.adapters",),
        )
    ]
    # File lives under pipeline/ so importer_glob matches
    pipeline_f = tmp_path / "pipeline" / "stages.py"
    pipeline_f.parent.mkdir()
    pipeline_f.write_text(SAMPLE)
    viols = find_violations(root=tmp_path, rules=rules)
    assert any("stockapp_v12.adapters" in v.message for v in viols)


def test_no_violation_when_import_allowed(tmp_path: Path) -> None:
    rules = [
        Rule(
            name="pipeline_no_adapters",
            importer_glob="**/pipeline/**/*.py",
            forbidden_module_prefixes=("stockapp_v12.adapters",),
        )
    ]
    pipeline_f = tmp_path / "pipeline" / "stages.py"
    pipeline_f.parent.mkdir()
    pipeline_f.write_text("from stockapp_v12.core.models import Signal\n")
    viols = find_violations(root=tmp_path, rules=rules)
    assert viols == []


def test_main_warn_mode_exits_zero_with_violations(tmp_path: Path) -> None:
    pipeline_f = tmp_path / "src" / "stockapp_v12" / "pipeline" / "stages.py"
    pipeline_f.parent.mkdir(parents=True)
    pipeline_f.write_text(SAMPLE)
    # DEFAULT_RULES use src/stockapp_v12/pipeline/**/*.py
    code = main(["--root", str(tmp_path)])
    assert code == 0


def test_main_strict_mode_exits_one_with_violations(tmp_path: Path) -> None:
    pipeline_f = tmp_path / "src" / "stockapp_v12" / "pipeline" / "stages.py"
    pipeline_f.parent.mkdir(parents=True)
    pipeline_f.write_text(SAMPLE)
    code = main(["--root", str(tmp_path), "--strict"])
    assert code == 1
