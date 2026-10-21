"""Tests for the Python preflight module (Phase A2)."""

import json

from stockapp_v12.scripts import preflight
from stockapp_v12.scripts.preflight import (
    CheckResult,
    PreflightReport,
    check_audit_ledger_dir,
    check_killswitch,
    check_python_imports,
    check_redis_ping,
    check_trade_journal_schema,
    run_preflight,
    write_report,
)

from ._fakes import FakeRedis


# ----- individual checks -----


def test_python_imports_passes():
    res = check_python_imports()
    assert res.ok is True
    assert res.detail["failed"] == {}
    assert res.detail["checked"] >= 5


def test_redis_ping_ok_with_fake():
    r = FakeRedis()
    # FakeRedis has no .ping() — fake it for this check
    r.ping = lambda: True
    res = check_redis_ping(r)
    assert res.ok is True


def test_redis_ping_fails_when_ping_raises():
    r = FakeRedis()
    r.ping = lambda: (_ for _ in ()).throw(ConnectionError("nope"))
    res = check_redis_ping(r)
    assert res.ok is False
    assert "ConnectionError" in (res.error or "")


def test_killswitch_passes_when_clear():
    r = FakeRedis()
    res = check_killswitch(r)
    assert res.ok is True


def test_killswitch_fails_when_halted():
    r = FakeRedis()
    from stockapp_v12.bridge.killswitch import KillSwitch
    KillSwitch(r).halt("under_test", halted_by="pytest")
    res = check_killswitch(r)
    assert res.ok is False
    assert res.detail["is_halted"] is True


def test_audit_ledger_dir_creates(tmp_path):
    target = tmp_path / "audit_x"
    assert not target.exists()
    res = check_audit_ledger_dir(root=target)
    assert res.ok is True
    assert target.is_dir()


def test_trade_journal_schema_empty_day_is_ok():
    """No journal keys for today => ok (fresh day, nothing to validate)."""
    r = FakeRedis()
    # No keys set
    r.keys = lambda k: []
    res = check_trade_journal_schema(r)
    assert res.ok is True
    assert res.detail["note"] == "no journal keys today"


def test_trade_journal_schema_ok():
    r = FakeRedis()
    # Set up a journal key with valid schema_version=1 record
    import json as _json
    record = {
        "schema_version": 1,
        "symbol": "NVDA",
        "decision": "BUY",
        "ts": "2026-07-07T10:00:00-04:00",
    }
    today = "2026-07-07"
    r.set(f"journal:{today}:NVDA", _json.dumps([record]))
    r.keys = lambda k: [f"journal:{today}:NVDA"] if "journal" in k else []
    res = check_trade_journal_schema(r)
    assert res.ok is True
    assert res.detail["schema_version"] == 1


def test_trade_journal_schema_wrong_version():
    r = FakeRedis()
    import json as _json
    record = {"schema_version": 99, "symbol": "NVDA"}
    r.set("journal:2026-07-07:NVDA", _json.dumps([record]))
    r.keys = lambda k: ["journal:2026-07-07:NVDA"] if "journal" in k else []
    res = check_trade_journal_schema(r)
    assert res.ok is False
    assert res.detail["schema_version"] == 99


def test_trade_journal_corrupt_json():
    r = FakeRedis()
    r.set("journal:2026-07-07:NVDA", "not-json")
    r.keys = lambda k: ["journal:2026-07-07:NVDA"] if "journal" in k else []
    res = check_trade_journal_schema(r)
    assert res.ok is False


# ----- run_preflight -----


def test_run_preflight_all_ok(tmp_path):
    r = FakeRedis()
    r.ping = lambda: True
    r.keys = lambda k: []
    report = run_preflight(redis_client=r)
    # Python imports + audit dir succeed. Redis ping ok. KillSwitch ok. Schema empty (ok).
    names = [c.name for c in report.checks]
    assert "python_imports" in names
    assert "redis_ping" in names
    assert "killswitch" in names
    assert "trade_journal_schema" in names
    assert "audit_ledger_dir" in names
    assert report.overall == "ok"


def test_run_preflight_fails_when_halted(tmp_path):
    r = FakeRedis()
    r.ping = lambda: True
    r.keys = lambda k: []
    from stockapp_v12.bridge.killswitch import KillSwitch
    KillSwitch(r).halt("for_test", halted_by="pytest")
    report = run_preflight(redis_client=r)
    assert report.overall == "fail"


def test_run_preflight_fails_when_redis_unreachable(tmp_path):
    class _DeadRedis:
        def __getattr__(self, name):
            raise ConnectionError("redis dead")

    report = run_preflight(redis_client=_DeadRedis())
    # Even with redis dead, python_imports should still pass locally
    assert any(c.name == "python_imports" and c.ok for c in report.checks)
    assert any(c.name == "redis_ping" and not c.ok for c in report.checks)
    assert report.overall == "fail"


# ----- write_report -----


def test_write_report_creates_dated_and_latest(tmp_path):
    report = PreflightReport(
        ts="2026-07-07T12:00:00-04:00",
        overall="ok",
        checks=[CheckResult(name="x", status="ok", ok=True)],
    )
    out = write_report(report, health_dir=tmp_path)
    assert out is not None
    assert out.exists()
    # dated
    assert (tmp_path / "preflight_2026-07-07.json").exists()
    # latest NDJSON
    assert (tmp_path / "preflight_latest.jsonl").exists()
    # content is parseable JSON
    payload = json.loads(out.read_text())
    assert payload["overall"] == "ok"
    assert payload["checks"][0]["name"] == "x"


def test_write_report_appends_latest_ndjson(tmp_path):
    r1 = PreflightReport(ts="2026-07-07T10:00:00-04:00", overall="ok", checks=[])
    r2 = PreflightReport(ts="2026-07-07T11:00:00-04:00", overall="ok", checks=[])
    write_report(r1, health_dir=tmp_path)
    write_report(r2, health_dir=tmp_path)
    latest = (tmp_path / "preflight_latest.jsonl").read_text()
    lines = [l for l in latest.split("\n") if l.strip()]
    assert len(lines) == 2


# ----- CLI main -----


def test_main_json_mode_returns_0_when_all_ok(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "run_preflight", lambda redis_client=None: PreflightReport(
        ts="2026-07-07T12:00:00-04:00", overall="ok", checks=[],
    ))
    rc = preflight.main(["--json", "--no-write"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"overall": "ok"' in out


def test_main_exits_2_on_fail(monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", lambda redis_client=None: PreflightReport(
        ts="2026-07-07T12:00:00-04:00", overall="fail", checks=[],
    ))
    rc = preflight.main(["--json", "--no-write"])
    assert rc == 2


def test_main_exits_1_on_warn(monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", lambda redis_client=None: PreflightReport(
        ts="2026-07-07T12:00:00-04:00", overall="warn", checks=[],
    ))
    rc = preflight.main(["--json", "--no-write"])
    assert rc == 1
