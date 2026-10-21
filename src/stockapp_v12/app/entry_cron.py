"""NUC entry-window composition root (cron).

Keeps ``pipeline.entry_window`` free of ``app`` imports (boundary rule)
while wiring paper orchestrator + bars + equity for production cron.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import redis

from stockapp_v12.app.wiring import make_entry_window_providers
from stockapp_v12.pipeline.entry_window import run_entry_window

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V12 paper entry-window (wired)")
    parser.add_argument(
        "--mode",
        default=os.environ.get("V12_MODE", "paper_ibkr"),
        help="Entry mode (default paper_ibkr; live modes blocked by guard)",
    )
    parser.add_argument(
        "--regime-override",
        default=None,
        help="Test-only regime override",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List tier2 candidates only (no orchestrator)",
    )
    parser.add_argument(
        "--visibility-only",
        action="store_true",
        help="Do not inject orchestrator (log candidates only)",
    )
    args = parser.parse_args(argv)

    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_PORT", "6380"))
    try:
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
    except Exception as e:
        print(f"ERROR: cannot reach Redis at {host}:{port} — {e}")
        return 1

    if args.dry_run:
        from stockapp_v12.pipeline.entry_window import (
            read_market_regime,
            read_tier2_symbols,
        )

        tier2 = read_tier2_symbols(r)
        regime = args.regime_override or read_market_regime(r)
        print(f"DRY tier2_size={len(tier2)} regime={regime}")
        for sym in tier2[:20]:
            print(f"  candidate: {sym}")
        return 0

    mode = args.mode
    if (mode or "").lower().startswith("live"):
        print(f"ERROR: live mode {mode!r} not allowed via entry_cron (paper only)")
        return 2

    wire = (
        not args.visibility_only
        and os.environ.get("V12_ENTRY_WIRED", "1").strip() not in {"0", "false", "False"}
    )
    if wire:
        load_chronos = os.environ.get("V12_LOAD_CHRONOS", "1") not in {
            "0",
            "false",
            "False",
        }
        orch_factory, bars_provider, equity_getter = make_entry_window_providers(
            r, mode=mode, load_chronos=load_chronos
        )
        result = run_entry_window(
            r,
            mode=mode,
            regime_override=args.regime_override,
            orch_factory=orch_factory,
            bars_provider=bars_provider,
            equity_getter=equity_getter,
        )
    else:
        result = run_entry_window(
            r, mode=mode, regime_override=args.regime_override
        )

    print(
        f"regime={result.regime} tier2_size={result.tier2_size} "
        f"evaluated={result.evaluated} buys={result.buys} "
        f"denies={result.denies} passes={result.passes} "
        f"skipped_no_orch={result.skipped_no_orchestrator} "
        f"skipped_no_bars={result.skipped_no_bars} wired={int(wire)}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
