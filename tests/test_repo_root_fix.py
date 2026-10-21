"""P0-1 regression test: swing_scanner._REPO_ROOT must resolve to project root.

Council review 2026-07-09 (Engineer voice, P0-1):
The previous arithmetic `_HERE.parent.parent.parent.parent` resolved to
``/Users/dev`` (i.e. $HOME), causing ``config/swing_scanner.yaml`` and
``config/universe.yaml`` to silently fall back to embedded defaults / a
hardcoded 10-name universe.

This test pins the fix at module-load time so any future refactor that breaks
the repo-root path arithmetic trips this regression test loudly.
"""

import os
from pathlib import Path



def test_swing_scanner_repo_root_resolves_to_project_root():
    """`_REPO_ROOT` must point to the project root, not $HOME or anywhere else."""
    # Lazy import so any module-level fix sits inside the assert
    from stockapp_v12.services.swing_scanner import _REPO_ROOT
    from stockapp_v12.core.paths import REPO_ROOT as EXPECTED_REPO_ROOT

    # 1. Must be an absolute path
    assert _REPO_ROOT.is_absolute(), f"_REPO_ROOT must be absolute: {_REPO_ROOT}"

    # 2. Must match the canonical REPO_ROOT from core/paths
    assert _REPO_ROOT == EXPECTED_REPO_ROOT, (
        f"swing_scanner._REPO_ROOT ({_REPO_ROOT}) != core.paths.REPO_ROOT "
        f"({EXPECTED_REPO_ROOT}). P0-1 regression: the local path arithmetic "
        "in swing_scanner.py is overriding core.paths again."
    )

    # 3. Must NOT resolve to $HOME (the historical bug)
    home = Path(os.path.expanduser("~"))
    assert _REPO_ROOT != home, (
        f"_REPO_ROOT resolved to $HOME ({home}). P0-1 regression: the local "
        "path arithmetic in swing_scanner.py is back to counting parents wrong."
    )

    # 4. Must NOT resolve to a system dir
    bad_roots = ("/", "/tmp", "/usr", "/var", "/etc")
    assert str(_REPO_ROOT) not in bad_roots, (
        f"_REPO_ROOT resolved to a system dir: {_REPO_ROOT}"
    )


def test_swing_scanner_repo_root_in_expected_layout():
    """Project root must contain the expected top-level structure."""
    from stockapp_v12.services.swing_scanner import _REPO_ROOT

    # Post-migration (2026-07-09): v12/ promoted to project root.
    assert _REPO_ROOT.name in {"Stockapp", "StockV12-public"}, (
        f"Project root should end in Stockapp or StockV12-public, got '{_REPO_ROOT}'"
    )

    # Sanity: src/, tests/, docs/ must exist as siblings
    assert (_REPO_ROOT / "src").is_dir(), f"missing src/ under {_REPO_ROOT}"
    assert (_REPO_ROOT / "tests").is_dir(), f"missing tests/ under {_REPO_ROOT}"
    assert (_REPO_ROOT / "docs").is_dir(), f"missing docs/ under {_REPO_ROOT}"
