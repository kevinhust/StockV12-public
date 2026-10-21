"""H1: AnalystOutput parse and validation."""

from stockapp_v12.pipeline.analyst import (
    parse_analyst_output,
    analyst_output_from_llm_text,
)


VALID = {
    "symbol": "NVDA",
    "side": "BUY",
    "confidence": 0.82,
    "thesis_tags": ["earnings_beat"],
    "invalidators": ["gap_down_5pct"],
    "expected_holding_days": 5,
    "entry_rationale": "Strong earnings beat with raised guidance.",
    "risk_flags": [],
}


def test_valid_parse():
    out, err = parse_analyst_output(VALID)
    assert err is None
    assert out is not None
    assert out.side == "BUY"
    assert out.confidence == 0.82
    assert out.thesis_tags == ["earnings_beat"]


def test_low_confidence_becomes_pass():
    raw = {**VALID, "confidence": 0.5}
    out, err = parse_analyst_output(raw, min_confidence=0.70)
    assert err is None
    assert out.side == "PASS"


def test_invalid_json_returns_none():
    out, code = analyst_output_from_llm_text("not json at all", symbol="NVDA")
    assert out is None
    assert code == "ANALYST_INVALID"


def test_missing_field():
    raw = {k: v for k, v in VALID.items() if k != "entry_rationale"}
    out, code = parse_analyst_output(raw)
    assert out is None
    assert code == "ANALYST_INVALID"
