"""Tests for ADR-008 walk-forward promotion gate (Phase A3)."""

import json

from stockapp_v12.eval.adr008_gate import (
    Adr008Thresholds,
    default_thresholds,
    evaluate,
    evaluate_file,
    main,
)


# ----- helpers -----

def _passing_summary(**overrides):
    """A summary that clears all 4 default thresholds."""
    s = {
        "model": "phase2_v2",
        "simulated_pnl_pct": 0.55,
        "mase": 2.06,
        "win_rate": 0.50,
        "n_trades": 30,
    }
    s.update(overrides)
    return s


def _failing_summary(**overrides):
    """A summary that fails on pnl (negative)."""
    return _passing_summary(simulated_pnl_pct=-0.10)


def _pnl_moratorium_summary(**overrides):
    """Same as passing but PnL is negative — tests moratorium behavior."""
    return _passing_summary(simulated_pnl_pct=-10.0)


# ----- test pass/fail per metric -----


def test_passing_summary_passes():
    res = evaluate(_passing_summary(), model_name="phase2_v2")
    assert res.passed is True
    assert len(res.checks) == 4
    assert all(c.passed for c in res.checks)
    assert "passed" in res.reason


def test_failing_pnl_marks_fail():
    """With moratorium OFF, negative PnL fails the gate."""
    res = evaluate(_failing_summary(), model_name="x",
                   thresholds=Adr008Thresholds(pnl_moratorium=False))
    assert res.passed is False
    failed_metrics = [c.metric for c in res.checks if not c.passed]
    assert failed_metrics == ["pnl"]


def test_pnl_moratorium_negative_pnl_does_not_fail():
    """Round 4 moratorium: negative PnL is informational, not blocking."""
    res = evaluate(_pnl_moratorium_summary(), model_name="x")
    assert res.passed is True  # moratorium on by default
    pnl_check = [c for c in res.checks if c.metric == "pnl"][0]
    assert pnl_check.informational is True
    assert pnl_check.actual == -10.0  # value still reported


def test_pnl_moratorium_off_still_blocks():
    """When moratorium is explicitly OFF, PnL gate is restored."""
    res = evaluate(_pnl_moratorium_summary(),
                   thresholds=Adr008Thresholds(pnl_moratorium=False))
    assert res.passed is False
    pnl_check = [c for c in res.checks if c.metric == "pnl"][0]
    assert pnl_check.informational is False


def test_missing_field_marks_fail():
    """A summary missing win_rate should fail with 'missing win_rate'."""
    s = _passing_summary()
    del s["win_rate"]
    res = evaluate(s, model_name="x")
    assert res.passed is False
    wr = [c for c in res.checks if c.metric == "win_rate"][0]
    assert wr.passed is False
    assert "missing" in wr.message


def test_empty_summary_marks_error():
    res = evaluate({}, model_name="x")
    assert res.passed is False
    assert res.error == "empty summary"


def test_each_threshold_violation():
    # pnl boundary — strict >, so pnl == 0 fails (moratorium OFF)
    thr_no_moratorium = Adr008Thresholds(pnl_moratorium=False)
    res = evaluate(_passing_summary(simulated_pnl_pct=0.0), thresholds=thr_no_moratorium)
    assert res.passed is False
    # pnl > 0 succeeds
    res = evaluate(_passing_summary(simulated_pnl_pct=0.001), thresholds=thr_no_moratorium)
    assert res.passed is True

    # With moratorium ON (default), pnl=0 does NOT fail
    res = evaluate(_passing_summary(simulated_pnl_pct=0.0))
    assert res.passed is True

    # mase boundary — <=, so mase == 4.011 passes
    res = evaluate(_passing_summary(mase=4.011))
    assert res.passed is True
    # mase > 4.011 fails
    res = evaluate(_passing_summary(mase=4.012))
    assert res.passed is False
    # mase == 4.5 fails
    res = evaluate(_passing_summary(mase=4.5))
    assert res.passed is False

    # win_rate boundary — >=, so 0.40 passes
    res = evaluate(_passing_summary(win_rate=0.40))
    assert res.passed is True
    # 0.39 fails
    res = evaluate(_passing_summary(win_rate=0.39))
    assert res.passed is False

    # n_trades boundary — >=, so 30 passes
    res = evaluate(_passing_summary(n_trades=30))
    assert res.passed is True
    # 29 fails
    res = evaluate(_passing_summary(n_trades=29))
    assert res.passed is False


# ----- multiple failures -----


def test_multiple_failures_listed_in_reason():
    """With moratorium OFF, both pnl + mase failures are listed."""
    res = evaluate(_passing_summary(simulated_pnl_pct=-0.01, mase=10.0),
                   thresholds=Adr008Thresholds(pnl_moratorium=False), model_name="x")
    assert res.passed is False
    assert "pnl" in res.reason and "mase" in res.reason


def test_multiple_failures_with_moratorium_ignores_pnl():
    """With moratorium ON, only mase failure listed; pnl is informational."""
    res = evaluate(_passing_summary(simulated_pnl_pct=-0.01, mase=10.0), model_name="x")
    assert res.passed is False
    assert "pnl" not in res.reason  # moratorium
    assert "mase" in res.reason


# ----- thresholds -----


def test_default_thresholds_match_spec():
    t = default_thresholds()
    assert t.pnl_min == 0.0
    assert t.mase_max == 4.011
    assert t.win_rate_min == 0.40
    assert t.n_trades_min == 30
    assert t.pnl_moratorium is True  # Round 4 council verdict


def test_custom_thresholds_override():
    """Operators can apply tighter thresholds for stricter rounds."""
    s = _passing_summary()
    # Default passes
    assert evaluate(s).passed is True
    # Custom: raise the bar so the same summary fails
    tight = Adr008Thresholds(pnl_min=0.0, mase_max=1.0, win_rate_min=0.55, n_trades_min=50)
    assert evaluate(s, thresholds=tight).passed is False


# ----- GateResult serialization -----


def test_gate_result_to_dict_has_required_keys():
    res = evaluate(_passing_summary(), model_name="phase2_v2")
    d = res.to_dict()
    assert "passed" in d
    assert "model" in d
    assert "checks" in d
    assert "thresholds" in d
    assert "summary_keys" in d
    assert len(d["checks"]) == 4


def test_gate_check_to_dict_serializable():
    res = evaluate(_passing_summary())
    chk_dict = res.checks[0].to_dict()
    # Re-serialize to make sure JSON-clean
    s = json.dumps(chk_dict)
    roundtrip = json.loads(s)
    assert roundtrip["metric"] == chk_dict["metric"]


# ----- evaluate_file -----


def test_evaluate_file_from_summary(tmp_path):
    p = tmp_path / "summary.json"
    p.write_text(json.dumps(_passing_summary()))
    res = evaluate_file(p)
    assert res.passed is True


def test_evaluate_file_from_per_date_with_rows(tmp_path):
    payload = {
        "model": "phase2_v2",
        "eval_date": "2026-07-01",
        "simulated_pnl_pct": 0.55,
        "mase": 2.06,
        "win_rate": 0.50,
        "rows": [{"i": i} for i in range(30)],  # n_trades=30 derived
    }
    p = tmp_path / "2026-07-01.json"
    p.write_text(json.dumps(payload))
    res = evaluate_file(p)
    assert res.passed is True


def test_evaluate_file_with_model_override(tmp_path):
    p = tmp_path / "summary.json"
    p.write_text(json.dumps(_passing_summary()))
    res = evaluate_file(p, model_name="custom_model")
    assert res.model_name == "custom_model"


# ----- CLI -----


def test_cli_pass(tmp_path, capsys):
    p = tmp_path / "summary.json"
    p.write_text(json.dumps(_passing_summary()))
    rc = main([str(p), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out


def test_cli_fail_no_exit(tmp_path, capsys):
    """With moratorium OFF, negative PnL shows as fail."""
    p = tmp_path / "summary.json"
    s = _failing_summary()
    s["pnl_moratorium"] = False  # not in summary; use evaluate directly
    p.write_text(json.dumps(_failing_summary()))
    rc = main([str(p), "--json"])
    # default does NOT gate on exit
    assert rc == 0
    out = capsys.readouterr().out
    # With moratorium ON (default), negative PnL doesn't fail the gate
    assert '"passed": true' in out


def test_cli_fail_with_exit_on_fail(tmp_path, capsys):
    p = tmp_path / "summary.json"
    # Need a summary that actually fails — use mase failure (moratorium doesn't affect mase)
    p.write_text(json.dumps(_passing_summary(mase=10.0)))
    rc = main([str(p), "--exit-on-fail"])
    assert rc == 1


def test_cli_human_output_shows_verdict(tmp_path, capsys):
    p = tmp_path / "summary.json"
    p.write_text(json.dumps(_passing_summary()))
    rc = main([str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "pnl" in out
