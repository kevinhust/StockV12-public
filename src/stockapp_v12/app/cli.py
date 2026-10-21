"""Thin CLI entrypoints (Phase 5).

Install with ``pip install -e .`` then:
  stockapp-demo-wiring
  stockapp-check-boundaries
"""

from __future__ import annotations

import sys


def demo_wiring() -> None:
    """Smoke: composition root with fakes (no network)."""
    from stockapp_v12.app.wiring import Settings, build_runtime

    rt = build_runtime(Settings(use_fake_market_data=True), use_memory_stores=True)
    print("clock", rt.clock.today_et())
    print("market_data", type(rt.market_data).__name__)
    print("notifier", type(rt.notifier).__name__)
    print("position_store", type(rt.position_store).__name__ if rt.position_store else None)
    raise SystemExit(0)


def check_boundaries() -> None:
    """Run import boundary checker (strict)."""
    from pathlib import Path

    # scripts/ may not be a package — load by path
    import importlib.util

    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "check_import_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_import_boundaries", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    raise SystemExit(mod.main(["--root", str(root), "--strict"]))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: stockapp-cli <demo-wiring|check-boundaries>")
        raise SystemExit(0)
    cmd = argv[0]
    if cmd == "demo-wiring":
        demo_wiring()
    elif cmd == "check-boundaries":
        check_boundaries()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
