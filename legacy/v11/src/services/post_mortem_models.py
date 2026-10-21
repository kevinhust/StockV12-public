"""
Post-Mortem Data Models
Structured types for the Self-Correction Layer V2.
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Literal
from datetime import datetime, timezone


class LosingTrade(BaseModel):
    """Extracted metadata for a losing trade that warrants post-mortem analysis."""
    trace_id: str = Field(..., description="UUID linking to LangGraph checkpoint state")
    event_id: str
    symbol: str
    side: str                                   # BUY or SELL
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float                         # Negative value
    pnl_percent: float                          # e.g. -2.3 means -2.3%
    pattern_type: str = "UNKNOWN"
    mode: str = "PAPER"
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    risk_metadata: Dict[str, Any] = Field(default_factory=dict)
    context_snapshot: Dict[str, Any] = Field(default_factory=dict)


class NegativeLesson(BaseModel):
    """
    LLM-generated causal lesson derived from a losing trade.
    Stored in ChromaDB for future RAG retrieval.
    """
    trace_id: str = Field(..., description="Source trade's trace_id (dedup key)")
    symbol: str
    regime: str = Field("UNKNOWN", description="Market regime at time of failure")
    pattern: str = Field("UNKNOWN", description="Pattern that triggered the trade")
    failure_reason: str = Field(
        ..., description="Root cause: e.g. 'false breakout', 'regime mismatch', 'liquidity trap'"
    )
    lesson_text: str = Field(
        ...,
        description=(
            "Structured lesson: 'In [Regime X], when [Pattern Y] appears "
            "but [Factor Z] is present, the signal is likely a false positive.'"
        )
    )
    severity: int = Field(
        ..., ge=1, le=5,
        description="1=minor, 3=notable, 5=critical pattern to avoid"
    )
    market_context_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="Macro context at failure time: VIX, SPY trend, sector rotation, etc."
    )
    # Metadata for ChromaDB indexing
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_verified_at: Optional[datetime] = Field(None, description="Last time this lesson was validated by reinforcement engine")
    llm_model_used: str = "glm-5"
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="LLM's confidence in this lesson")
    success_count: int = Field(0, description="How many times this lesson successfully avoided a loss")


# ─── V2: Structured LLM Output Models ───

class PnlFactor(BaseModel):
    """Single factor contributing to a trade's PnL."""
    factor: str
    pct: float
    desc: str


class SystemSuggestion(BaseModel):
    """Actionable system/parameter improvement."""
    priority: Literal["P0", "P1", "P2"]
    suggestion: str


class TradeReviewModel(BaseModel):
    """Structured LLM output for a single trade review."""
    overview: str = Field(description="One sentence summary of the trade")
    execution_score: int = Field(ge=0, le=10, description="0-10 execution discipline")
    signal_score: int = Field(ge=0, le=10, description="0-10 signal quality/setup")
    pnl_attribution: List[PnlFactor] = Field(description="Factors contributing to PnL")
    example_category: Literal["A", "B", "C", "D", "E"] = Field(
        description="A=Excellence, B=Borderline Pos, C=Neutral, D=Borderline Neg, E=Fatal Error"
    )
    failure_reason: str = Field(
        description="Root cause: false_breakout, regime_mismatch, liquidity_trap, "
                    "stale_signal, overconfidence, news_shock, correlation_trap, other"
    )
    key_lesson: List[str] = Field(description="Key takeaways")
    system_suggestions: List[SystemSuggestion] = Field(description="Actionable improvements")
    severity: int = Field(ge=1, le=10, description="1=minor fluke, 10=critical systemic flaw")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this analysis")


class LessonEnvelope(BaseModel):
    """Canonical JSON structure for a post-mortem lesson, ready for Redis + ChromaDB."""
    doc_id: str                             # "lesson_{trace_id}"
    trace_id: str
    symbol: str
    regime: str
    pattern: str
    failure_reason: str
    lesson_text: str                        # Full text, no truncation
    severity: int                           # 1-10
    confidence: float
    pnl_pct: float
    time_of_day: str = "unknown"            # premarket/morning/midday/afternoon/eod
    market_context: Dict[str, Any] = Field(default_factory=dict)
    review: Optional[TradeReviewModel] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "post_mortem_v2"


class FilterResult(BaseModel):
    """Output of the 4-layer filtering pipeline."""
    passed: bool
    bucket: Literal["negative", "positive", "neutral"] = "neutral"
    layer_reasons: List[str] = Field(default_factory=list)
    priority_score: int = 0
