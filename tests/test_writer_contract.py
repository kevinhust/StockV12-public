#!/usr/bin/env python3
"""
test_writer_contract.py — TDD regression for incremental writer contract.

Why this test exists (ADR-009 addendum, 2026-07-01):
  Bug #3 was a silent-overwrite bug in write_partitioned_parquet:
  every cron run replaced the entire year partition with just the new
  days, dropping the prior 6 months of history. The function's
  docstring CLAIMED it was "incremental-safe" — but the implementation
  was full-overwrite.

  This test pins the contract:
    1. Two consecutive writes must MERGE (not overwrite).
    2. The result must contain the union of timestamps from both writes.
    3. Duplicate timestamps must be deduplicated (keep="last").
    4. Empty second-write must not destroy the first.

If any of these fail, the writer has regressed to Bug #3's class.
The test name reflects the contract: "incremental writer" must be
idempotent across multiple invocations.

Tests are written first per TDD methodology. The first time this test
was added, write_partitioned_parquet was overwriting partitions and
this test caught Bug #3 retroactively. It now guards against the same
class of regression for any future writer added to fetch_polygon_training_data.
"""
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

# Make fetch_polygon_training_data importable from archive (post V11→V12 migration)
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # tests/ → parents[1] = repo root
_ARCHIVE_SCRIPT = PROJECT_ROOT / "archive" / "v11-legacy-2026-07-09" / "scripts" / "fetch_polygon_training_data.py"
_LEGACY_SCRIPT = PROJECT_ROOT / "scripts" / "fetch_polygon_training_data.py"

# Prefer current scripts/ if present, fall back to archive.
_SCRIPT_PATH = _LEGACY_SCRIPT if _LEGACY_SCRIPT.exists() else _ARCHIVE_SCRIPT

if not _SCRIPT_PATH.exists():
    pytest.skip(
        f"fetch_polygon_training_data.py not found at {_SCRIPT_PATH}",
        allow_module_level=True,
    )

sys.path.insert(0, str(_SCRIPT_PATH.parent))

# Use importlib so we don't depend on the file naming conventions or
# the scripts/ directory being a real Python package.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "fetch_polygon_training_data",
    str(_SCRIPT_PATH),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["fetch_polygon_training_data"] = _mod
_spec.loader.exec_module(_mod)
write_partitioned_parquet = _mod.write_partitioned_parquet


def _make_fixture_df(
    start: str, end_inclusive: str, sym: str = "TEST"
) -> pd.DataFrame:
    """Build a minimal-but-valid Polygonesque DataFrame spanning [start..end]."""
    dates = pd.date_range(start=start, end=end_inclusive, freq="D")
    n = len(dates)
    return pd.DataFrame({
        "timestamp": pd.to_datetime(dates),
        "open":   [100.0 + i * 0.1 for i in range(n)],
        "high":   [101.0 + i * 0.1 for i in range(n)],
        "low":    [ 99.0 + i * 0.1 for i in range(n)],
        "close":  [100.5 + i * 0.1 for i in range(n)],
        "volume": [1_000_000 + i * 1000 for i in range(n)],
        "adj_close": [100.5 + i * 0.1 for i in range(n)],
        "split_factor": 1.0,
        "dividend": 0.0,
        "vwap":   [100.3 + i * 0.1 for i in range(n)],
        "symbol": sym,
    })


def test_two_writes_merge_not_overwrite():
    """First write 30 days, then write 10 more — final must have all 40."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "phase1"
        # First write: 2026-01-01..2026-01-30
        df1 = _make_fixture_df("2026-01-01", "2026-01-30", sym="AAPL")
        write_partitioned_parquet(df1, root, freq="daily")
        first_path = root / "year=2026" / "symbol=AAPL" / "data.parquet"
        assert first_path.exists(), "first write should produce partition"
        first_n = len(pd.read_parquet(first_path))
        assert first_n == 30, f"first write expected 30 rows, got {first_n}"

        # Second write: 2026-02-01..2026-02-10 (different days, NO overlap)
        df2 = _make_fixture_df("2026-02-01", "2026-02-10", sym="AAPL")
        write_partitioned_parquet(df2, root, freq="daily")

        # Assert: partition now has 40 rows (30 + 10), NOT 10.
        second_n = len(pd.read_parquet(first_path))
        assert second_n == 40, (
            f"BUG #3 REGRESSION: second write produced {second_n} rows. "
            f"Expected 40 (= 30 from first + 10 from second). If 10, the "
            f"writer is overwriting rather than merging."
        )

        # Verify both date ranges are present
        df = pd.read_parquet(first_path)
        dates = sorted(df["timestamp"].dt.date.tolist())
        assert dates[0] == pd.Timestamp("2026-01-01").date()
        assert dates[-1] == pd.Timestamp("2026-02-10").date()
        assert len(dates) == 40


def test_overlapping_dates_dedupe_keep_last():
    """If second write has overlapping dates with the first, keep="last" wins."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "phase1"
        df1 = _make_fixture_df("2026-01-01", "2026-01-10", sym="AAPL")
        write_partitioned_parquet(df1, root, freq="daily")

        # Second write: same 10 days but with bumped close prices
        df2 = _make_fixture_df("2026-01-01", "2026-01-10", sym="AAPL")
        df2["close"] = df2["close"] + 5.0  # all +5.0 to make the dupe obvious
        write_partitioned_parquet(df2, root, freq="daily")

        df = pd.read_parquet(root / "year=2026" / "symbol=AAPL" / "data.parquet")
        assert len(df) == 10, f"expected 10 deduped rows, got {len(df)}"
        # The last write's prices should win (keep="last")
        assert df.iloc[0]["close"] == pytest.approx(105.5, abs=0.01), (
            f"expected deduped close ~105.5 (100.5 + 5.0), got {df.iloc[0]['close']}"
        )


def test_empty_second_write_does_not_destroy_first():
    """If second write is empty, the original rows must survive."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "phase1"
        df1 = _make_fixture_df("2026-03-01", "2026-03-31", sym="NVDA")
        write_partitioned_parquet(df1, root, freq="daily")
        first_path = root / "year=2026" / "symbol=NVDA" / "data.parquet"
        first_n = len(pd.read_parquet(first_path))
        assert first_n == 31

        # Empty second write — function should no-op without destroying anything
        empty = pd.DataFrame(columns=df1.columns)
        write_partitioned_parquet(empty, root, freq="daily")

        n_after = len(pd.read_parquet(first_path))
        assert n_after == first_n, (
            f"empty second write destroyed data: {first_n} -> {n_after}"
        )


def test_multi_year_writes_create_year_partitions():
    """Writer must split by year; multi-year fixture → multiple year partitions."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "phase1"
        df = _make_fixture_df("2024-12-30", "2025-01-05", sym="SPY")  # 7 days spanning 2 years
        write_partitioned_parquet(df, root, freq="daily")

        year_dirs = [d for d in (root).iterdir() if d.is_dir() and d.name.startswith("year=")]
        years = sorted(d.name.replace("year=", "") for d in year_dirs)
        assert years == ["2024", "2025"], f"expected year=2024 + year=2025 partitions, got {years}"

        # Each year partition gets only the days in that year
        df_2024 = pd.read_parquet(root / "year=2024" / "symbol=SPY" / "data.parquet")
        df_2025 = pd.read_parquet(root / "year=2025" / "symbol=SPY" / "data.parquet")
        assert len(df_2024) == 2, f"2024 partition should have 2 days (Dec 30, 31), got {len(df_2024)}"
        assert len(df_2025) == 5, f"2025 partition should have 5 days (Jan 1..5), got {len(df_2025)}"


def test_write_twice_same_data_is_idempotent():
    """Writing identical data twice should not change the partition (no row explosion)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "phase1"
        df = _make_fixture_df("2026-04-01", "2026-04-15", sym="MSFT")
        write_partitioned_parquet(df, root, freq="daily")
        n1 = len(pd.read_parquet(root / "year=2026" / "symbol=MSFT" / "data.parquet"))

        # Write the same data again
        write_partitioned_parquet(df, root, freq="daily")
        n2 = len(pd.read_parquet(root / "year=2026" / "symbol=MSFT" / "data.parquet"))

        assert n1 == n2, (
            f"non-idempotent: writing same data twice changed row count {n1} -> {n2}. "
            f"Either the writer isn't deduping on timestamp, or it's exploding the partition."
        )
