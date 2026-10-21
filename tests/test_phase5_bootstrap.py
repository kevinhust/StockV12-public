"""Phase 5: bootstrap never adds archive/; CLI demo exits 0."""

from __future__ import annotations

import sys

from stockapp_v12.app.bootstrap import ensure_src_on_path, repo_root


def test_ensure_src_on_path_has_src_not_archive():
    root = ensure_src_on_path()
    assert root == repo_root()
    assert str(root / "src") in sys.path
    assert not any("archive/v11" in p for p in sys.path)


def test_demo_wiring_cli(capsys):
    from stockapp_v12.app.cli import demo_wiring

    try:
        demo_wiring()
    except SystemExit as e:
        assert e.code == 0
    out = capsys.readouterr().out
    assert "FakeMarketData" in out or "market_data" in out
