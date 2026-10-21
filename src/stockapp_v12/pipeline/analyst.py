"""
LLMAnalyst structured output — analysis only, no sizing.
Spec: v12/docs/TRADING_PIPELINE.md#llmanalyst-output-contract
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

ANALYST_VERSION = "v12-analyst-1"
VALID_SIDES = frozenset({"BUY", "PASS"})


class AnalystParseError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass
class AnalystOutput:
    symbol: str
    side: str
    confidence: float
    thesis_tags: List[str]
    invalidators: List[str]
    expected_holding_days: int
    entry_rationale: str
    risk_flags: List[str] = field(default_factory=list)
    analyst_version: str = ANALYST_VERSION
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def passes_confidence_gate(self, min_confidence: float) -> bool:
        return self.side == "BUY" and self.confidence >= min_confidence


def _as_str_list(value: Any, field_name: str) -> List[str]:
    if value is None:
        if field_name == "invalidators":
            return []
        raise AnalystParseError("MISSING_FIELD", field_name)
    if not isinstance(value, list):
        raise AnalystParseError("INVALID_TYPE", field_name)
    return [str(x) for x in value]


def parse_analyst_output(
    raw: Dict[str, Any],
    *,
    symbol: Optional[str] = None,
    min_confidence: float = 0.70,
) -> Tuple[Optional[AnalystOutput], Optional[str]]:
    try:
        sym = str(raw.get("symbol") or symbol or "").upper().strip()
        if not sym:
            raise AnalystParseError("MISSING_FIELD", "symbol")

        side = str(raw.get("side", "")).upper().strip()
        if side not in VALID_SIDES:
            raise AnalystParseError("INVALID_ENUM", "side")

        confidence = float(raw.get("confidence"))
        if not (0.0 <= confidence <= 1.0):
            raise AnalystParseError("OUT_OF_RANGE", "confidence")

        thesis_tags = _as_str_list(raw.get("thesis_tags"), "thesis_tags")
        invalidators = _as_str_list(raw.get("invalidators"), "invalidators")

        expected_days = int(raw.get("expected_holding_days"))
        if expected_days < 1 or expected_days > 20:
            expected_days = max(2, min(15, expected_days if expected_days > 0 else 5))

        rationale = str(raw.get("entry_rationale", "")).strip()
        if not rationale:
            raise AnalystParseError("MISSING_FIELD", "entry_rationale")

        risk_flags = raw.get("risk_flags") or []
        if not isinstance(risk_flags, list):
            raise AnalystParseError("INVALID_TYPE", "risk_flags")

        out = AnalystOutput(
            symbol=sym,
            side=side,
            confidence=confidence,
            thesis_tags=thesis_tags,
            invalidators=invalidators,
            expected_holding_days=expected_days,
            entry_rationale=rationale,
            risk_flags=[str(x) for x in risk_flags],
            analyst_version=str(raw.get("analyst_version") or ANALYST_VERSION),
            generated_at=str(raw.get("generated_at") or datetime.now(timezone.utc).isoformat()),
        )

        if out.side == "BUY" and out.confidence < min_confidence:
            out.side = "PASS"

        return out, None

    except (AnalystParseError, TypeError, ValueError, KeyError):
        return None, "ANALYST_INVALID"


def analyst_output_from_llm_text(
    text: str,
    *,
    symbol: str,
    min_confidence: float = 0.70,
) -> Tuple[Optional[AnalystOutput], Optional[str]]:
    import json
    import re

    text = text.strip()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None, "ANALYST_INVALID"
        try:
            raw = json.loads(match.group())
        except json.JSONDecodeError:
            return None, "ANALYST_INVALID"

    if not isinstance(raw, dict):
        return None, "ANALYST_INVALID"
    return parse_analyst_output(raw, symbol=symbol, min_confidence=min_confidence)
