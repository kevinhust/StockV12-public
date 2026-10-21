"""
Preflight (Python) — full V12 health check, suitable for ad-hoc operator
debugging or scheduled "is the system OK?" sanity probes.

Unlike the shell ``_preflight.sh`` (cheap, runs every cron invocation),
this one takes ~1s and surfaces structured findings as JSON, both to
stdout and to ``data/health/preflight_<date>.json``.

Spec: Phase A2 (council verdict 2026-07-07).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

logger = logging.getLogger("preflight")

EST = ZoneInfo("America/New_York")
# Post-migration (2026-07-09): v12/ promoted to root; health data now under data/
DEFAULT_HEALTH_DIR = Path("data/health")


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail"
    ok: bool
    detail: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class PreflightReport:
    ts: str
    overall: str  # "ok" | "warn" | "fail"
    checks: List[CheckResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "overall": self.overall,
            "checks": [asdict(c) for c in self.checks],
        }


# ----- individual checks -----

def check_redis_ping(redis_client) -> CheckResult:
    start = time.time()
    try:
        pong = redis_client.ping()
        ok = bool(pong)
        return CheckResult(
            name="redis_ping",
            status="ok" if ok else "fail",
            ok=ok,
            detail={"pong": pong},
            duration_ms=(time.time() - start) * 1000,
        )
    except Exception as e:
        return CheckResult(
            name="redis_ping",
            status="fail",
            ok=False,
            duration_ms=(time.time() - start) * 1000,
            error=f"{type(e).__name__}: {e}",
        )


def check_killswitch(redis_client) -> CheckResult:
    from stockapp_v12.bridge.killswitch import KillSwitch
    start = time.time()
    try:
        ks = KillSwitch(redis_client, fail_closed_on_error=True)
        state = ks.status()
        ok = not state.is_halted
        return CheckResult(
            name="killswitch",
            status="ok" if ok else "fail",
            ok=ok,
            detail=state.to_dict(),
            duration_ms=(time.time() - start) * 1000,
        )
    except Exception as e:
        return CheckResult(
            name="killswitch",
            status="fail",
            ok=False,
            duration_ms=(time.time() - start) * 1000,
            error=f"{type(e).__name__}: {e}",
        )


def check_python_imports() -> CheckResult:
    """Verify key V12 modules import cleanly."""
    start = time.time()
    targets = [
        "stockapp_v12",
        "stockapp_v12.bridge.live_guard",
        "stockapp_v12.bridge.killswitch",
        "stockapp_v12.bridge.gatekeeper",
        "stockapp_v12.core.limits",
        "stockapp_v12.core.sizing",
        "stockapp_v12.audit.audit_ledger",
        "stockapp_v12.services.entry_orchestrator",
        "stockapp_v12.services.position_manager",
    ]
    failed: Dict[str, str] = {}
    for mod in targets:
        try:
            importlib.import_module(mod)
        except Exception as e:
            failed[mod] = f"{type(e).__name__}: {e}"
    ok = not failed
    return CheckResult(
        name="python_imports",
        status="ok" if ok else "fail",
        ok=ok,
        detail={"checked": len(targets), "failed": failed},
        duration_ms=(time.time() - start) * 1000,
    )


def check_audit_ledger_dir(root: Optional[Path] = None) -> CheckResult:
    start = time.time()
    target = root or DEFAULT_HEALTH_DIR
    # Use AuditLedger's own path setup to exercise its mkdir logic
    try:
        from stockapp_v12.audit.audit_ledger import AuditLedger
        AuditLedger(root=target)
        ok = target.is_dir()
        return CheckResult(
            name="audit_ledger_dir",
            status="ok" if ok else "fail",
            ok=ok,
            detail={"path": str(target)},
            duration_ms=(time.time() - start) * 1000,
        )
    except Exception as e:
        return CheckResult(
            name="audit_ledger_dir",
            status="fail",
            ok=False,
            duration_ms=(time.time() - start) * 1000,
            error=f"{type(e).__name__}: {e}",
        )


def check_trade_journal_schema(redis_client) -> CheckResult:
    """Sample one journal:<today>:<sym> and verify schema_version=1."""
    start = time.time()
    try:
        from stockapp_v12.pipeline.journal import TradeJournal
        journal = TradeJournal(redis_client)
        # Use today's first available key, otherwise treat as OK (empty day)
        today = datetime.now(EST).strftime("%Y-%m-%d")
        # Scan by pattern via keys() — fine for diagnostics.
        keys = []
        try:
            keys = redis_client.keys(f"journal:{today}:*") or []
        except Exception as e:
            logger.warning(
                "[preflight] journal keys() scan failed: %s: %s — "
                "treat as no-journal-written-today (may mask real schema issue)",
                type(e).__name__, e,
            )
            keys = []
        if not keys:
            return CheckResult(
                name="trade_journal_schema",
                status="ok",
                ok=True,
                detail={"note": "no journal keys today", "date": today},
                duration_ms=(time.time() - start) * 1000,
            )
        sample = keys[0]
        raw = redis_client.get(sample)
        import json as _json
        try:
            entries = _json.loads(raw)
            if not isinstance(entries, list) or not entries:
                return CheckResult(
                    name="trade_journal_schema",
                    status="warn",
                    ok=False,
                    detail={"sample_key": sample, "issue": "empty_or_not_list"},
                    duration_ms=(time.time() - start) * 1000,
                )
            sv = entries[0].get("schema_version")
            ok = sv == 1
            return CheckResult(
                name="trade_journal_schema",
                status="ok" if ok else "fail",
                ok=ok,
                detail={"sample_key": sample, "schema_version": sv},
                duration_ms=(time.time() - start) * 1000,
            )
        except _json.JSONDecodeError:
            return CheckResult(
                name="trade_journal_schema",
                status="fail",
                ok=False,
                detail={"sample_key": sample, "issue": "json_decode_error"},
                duration_ms=(time.time() - start) * 1000,
            )
    except Exception as e:
        return CheckResult(
            name="trade_journal_schema",
            status="fail",
            ok=False,
            duration_ms=(time.time() - start) * 1000,
            error=f"{type(e).__name__}: {e}",
        )


# ----- orchestrator -----

def run_preflight(redis_client=None) -> PreflightReport:
    """Run every check.  ``redis_client`` is injected or built fresh."""
    if redis_client is None:
        try:
            import redis as redis_lib
            redis_client = redis_lib.Redis(
                host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                port=int(os.environ.get("REDIS_PORT", 6379)),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception:
            # Fall back to a stub whose methods raise — checks will report fail.
            class _Stub:
                def __getattr__(self, name):  # any call -> exception
                    raise ConnectionError("redis not available")
            redis_client = _Stub()

    checks: List[CheckResult] = []
    checks.append(check_python_imports())
    checks.append(check_audit_ledger_dir())
    checks.append(check_redis_ping(redis_client))
    checks.append(check_killswitch(redis_client))
    checks.append(check_trade_journal_schema(redis_client))

    if all(c.ok for c in checks):
        overall = "ok"
    elif any(c.status == "fail" for c in checks):
        overall = "fail"
    else:
        overall = "warn"

    return PreflightReport(
        ts=datetime.now(EST).isoformat(),
        overall=overall,
        checks=checks,
    )


def write_report(report: PreflightReport, health_dir: Optional[Path] = None) -> Optional[Path]:
    target_dir = health_dir or DEFAULT_HEALTH_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    today = report.ts.split("T")[0]
    out = target_dir / f"preflight_{today}.json"
    out.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    # Append-only line in latest.jsonl (for trend dashboards)
    latest = target_dir / "preflight_latest.jsonl"
    with latest.open("a") as fh:
        fh.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="V12 preflight health check")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument(
        "--health-dir",
        type=Path,
        default=None,
        help="override health output directory",
    )
    parser.add_argument(
        "--no-write", action="store_true", help="skip writing to disk"
    )
    args = parser.parse_args(argv)

    report = run_preflight()
    if not args.no_write:
        write_report(report, args.health_dir)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"== preflight @ {report.ts} ==  overall: {report.overall}")
        for c in report.checks:
            mark = "OK " if c.ok else "X  "
            err = f"  err={c.error}" if c.error else ""
            print(
                f"  {mark}{c.name:24s} {c.status:5s} "
                f"({c.duration_ms:.1f} ms){err}"
            )

    # Exit code: 0 = ok, 1 = warn, 2 = fail — so cron / monitors can react.
    return {"ok": 0, "warn": 1, "fail": 2}[report.overall]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
