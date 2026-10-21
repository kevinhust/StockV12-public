"""YoloFilterDecision — pure DTO for Wave 9 no-trade filter verdicts.

Phase 2 decoupling: lives in ``schemas`` so ``pipeline`` can depend on
types without importing ``services.yolo_filter`` (adapters/engine).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Dict, Optional


@dataclass(frozen=True)
class YoloFilterDecision:
    """Single-symbol YOLO filter verdict.

    Attributes:
        symbol: Ticker symbol (uppercased).
        as_of: Decision date (the entry date, not the image date).
        image_date: Date of the chart image used (as_of - image_lag_days).
        image_path: Absolute path to the chart image, or None if missing.
        yolo_probs: Mapping class_name -> softmax probability.
        predicted_class: Argmax class name (or "" on missing image).
        predicted_conf: Probability of predicted_class (0.0 on missing image).
        no_trade: True if the filter says suppress this entry.
        reason: Short reason tag (e.g. "CHOP_HIGH_CONF", "OK", "IMAGE_MISSING").
        blocked_class: The class whose threshold was exceeded (only when no_trade=True).
        threshold_used: Threshold value that was crossed (only when no_trade=True).
        model_id: Identifier of the loaded model (defaults to model dir name).
        degraded: True when the filter could not produce a verdict (model not loaded,
                  image missing, runtime error). Always False when no_trade=True.
    """

    symbol: str
    as_of: date
    image_date: Optional[date]
    image_path: Optional[str]
    yolo_probs: Dict[str, float]
    predicted_class: str
    predicted_conf: float
    no_trade: bool
    reason: str
    blocked_class: Optional[str] = None
    threshold_used: Optional[float] = None
    model_id: str = "yolo_phase2_v1_gpu"
    degraded: bool = False

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        d["image_date"] = self.image_date.isoformat() if self.image_date else None
        return d


__all__ = ["YoloFilterDecision"]
