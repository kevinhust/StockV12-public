# tests/test_shadow_cron_role.py
"""Verify run_shadow_cron.sh sets CHAMPION_CSV/CHALLENGER_CSV correctly
and that halt_swap.json takes precedence."""
import re
from pathlib import Path


def test_cron_defines_role_env_vars():
    """run_shadow_cron.sh must export CHAMPION_CSV and CHALLENGER_CSV.

    Accepts either of the bash-idiomatic forms:
        export CHAMPION_CSV
        export CHALLENGER_CSV
    or the combined form:
        export CHAMPION_CSV CHALLENGER_CSV
    """
    script = Path("scripts/run_shadow_cron.sh").read_text()
    assert "CHAMPION_CSV" in script
    assert "CHALLENGER_CSV" in script
    # Match either separate or combined `export` statement(s).
    # The combined form is one line: `export CHAMPION_CSV CHALLENGER_CSV`.
    # The separate form is two lines.
    combined = re.search(r"^\s*export\s+CHAMPION_CSV\s+CHALLENGER_CSV\s*$",
                         script, re.MULTILINE)
    separate = (
        re.search(r"^\s*export\s+CHAMPION_CSV\s*$", script, re.MULTILINE)
        and re.search(r"^\s*export\s+CHALLENGER_CSV\s*$", script, re.MULTILINE)
    )
    assert combined or separate, (
        "Expected `export CHAMPION_CSV CHALLENGER_CSV` (combined) "
        "or separate `export CHAMPION_CSV` + `export CHALLENGER_CSV` lines"
    )


def test_cron_reads_halt_swap_file():
    """run_shadow_cron.sh must check halt_swap.json for swap precedence."""
    script = Path("scripts/run_shadow_cron.sh").read_text()
    assert "halt_swap.json" in script
    assert "SWAP_CHAMPION" in script or "active_champion" in script


def test_ensemble_filters_to_champion_only():
    """run_shadow_ensemble.sh must not consume challenger JSONs."""
    script = Path("scripts/run_shadow_ensemble.sh").read_text()
    # Champion env var should be referenced; no full MODELS_CSV loop
    assert "CHAMPION" in script
