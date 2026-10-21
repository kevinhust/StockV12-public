"""
YoloNoTradeFilter — Wave 9 integration of Phase 2 GPU retrain as a no-trade filter.

Loads `models/yolo/phase2_v1_gpu/best.pt` (ResNet18 classifier, 6 classes) and
applies the tuned per-class thresholds from `threshold_tuned.json` to decide
whether the current 5min chart "predicts" a chop/normal (neutral) pattern at
high enough confidence to suppress an entry. See
`docs/YOLO_GPU_RETRAIN_REPORT.md` for tuning rationale.

Architecture (per Wave 9 task brief):
  EntryGuard → Forecast (Chronos) → YoloNoTradeFilter → SR-01~05 + SR-06 → Gatekeeper

The filter is **soft**: a missing image or a runtime error returns
`no_trade=False` with a tagged reason so it never blocks the pipeline.
Only high-confidence neutral-class predictions suppress entry.

Fail-soft semantics:
- IMAGE_MISSING  → no_trade=False (no image → no suppression signal)
- MODEL_ERROR    → no_trade=False (exception during inference → no suppression)
- DEGRADED       → no_trade=False (model not loaded → no suppression)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from stockapp_v12.schemas.yolo import YoloFilterDecision  # Phase 2: DTO in schemas

# Project layout helpers
# yolo_filter.py lives at src/stockapp_v12/services/yolo_filter.py (post-migration)
# parents[0]=services, parents[1]=stockapp_v12, parents[2]=src, parents[3]=<repo root>
PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_DIR = PROJECT_ROOT_DEFAULT / "models" / "yolo" / "phase2_v1_gpu"
DEFAULT_IMAGE_DIR = PROJECT_ROOT_DEFAULT / "data" / "intraday_clean_5min" / "phase2" / "ready_for_yolo_full" / "images"

# Tuned per-class thresholds — keep in sync with threshold_tuned.json.
# These are the "high-confidence neutral" gates. Conservative on breakout
# (allow more entries), conservative on chop (block more aggressively).
DEFAULT_NEUTRAL_CLASSES: List[str] = ["chop", "normal"]
DEFAULT_BULLISH_CLASSES: List[str] = ["breakout", "trend_up"]
DEFAULT_BEARISH_CLASSES: List[str] = ["trend_down", "reversal"]

# Default chart-render time (16:00 ET = end of RTH) matches prepare_yolo_dataset.py
DEFAULT_IMAGE_TIME_HHMMSS = "160000"


class YoloNoTradeFilter:
    """Loads phase2_v1_gpu ResNet18 + tuned thresholds and produces YoloFilterDecision.

    Construction is cheap (no model load); the model is loaded lazily on the first
    `predict()` call so that `--yolo-no-trade-filter` off (or unittests with no model)
    incurs zero overhead.
    """

    def __init__(
        self,
        *,
        model_dir: Path = DEFAULT_MODEL_DIR,
        image_dir: Path = DEFAULT_IMAGE_DIR,
        neutral_classes: Optional[List[str]] = None,
        bullish_classes: Optional[List[str]] = None,
        bearish_classes: Optional[List[str]] = None,
        image_lag_days: int = 1,
        image_time_hhmmss: str = DEFAULT_IMAGE_TIME_HHMMSS,
        device: str = "auto",
        model_id: Optional[str] = None,
        # Optional: bypass threshold; only suppress if predicted class is neutral AND conf > this.
        # This is a second gate to reduce false-positive suppressions.
        min_pred_conf: float = 0.30,
    ):
        self.model_dir = Path(model_dir)
        self.image_dir = Path(image_dir)
        self.neutral_classes = set(neutral_classes or DEFAULT_NEUTRAL_CLASSES)
        self.bullish_classes = set(bullish_classes or DEFAULT_BULLISH_CLASSES)
        self.bearish_classes = set(bearish_classes or DEFAULT_BEARISH_CLASSES)
        self.image_lag_days = int(image_lag_days)
        self.image_time_hhmmss = image_time_hhmmss
        self.device_pref = device
        self.model_id = model_id or self.model_dir.name
        self.min_pred_conf = float(min_pred_conf)

        # Lazy state
        self._model = None
        self._class_names: List[str] = []
        self._label_to_idx: Dict[str, int] = {}
        self._thresholds: Dict[str, float] = {}
        self._resolved_device: Optional[str] = None
        self._transform = None
        self._imagenet_mean = (0.485, 0.456, 0.406)
        self._imagenet_std = (0.229, 0.224, 0.225)
        self._image_size = 224

    # ------------------------------------------------------------------
    # Lazy model loading
    # ------------------------------------------------------------------
    def _load(self) -> bool:
        """Load model + metadata on first call. Returns True if loaded OK."""
        if self._model is not None:
            return True

        best_pt = self.model_dir / "best.pt"
        thr_json = self.model_dir / "threshold_tuned.json"
        cls_json = self.model_dir / "class_index.json"

        if not best_pt.exists():
            logger.warning(f"[YOLO-FILTER] best.pt not found at {best_pt}; filter DEGRADED")
            return False
        if not thr_json.exists():
            logger.warning(f"[YOLO-FILTER] threshold_tuned.json not found at {thr_json}; filter DEGRADED")
            return False

        try:
            import torch
            from torchvision import transforms
            import torchvision.models as tvm

            device = self._pick_device(torch)
            self._resolved_device = str(device)

            ckpt = torch.load(best_pt, map_location=device, weights_only=False)
            self._class_names = list(ckpt.get("class_names", []))
            self._label_to_idx = dict(ckpt.get("label_to_idx", {}))
            if not self._class_names:
                logger.warning("[YOLO-FILTER] best.pt has no class_names; filter DEGRADED")
                return False

            model = tvm.resnet18(weights=None)
            in_features = model.fc.in_features
            model.fc = torch.nn.Linear(in_features, len(self._class_names))
            model.load_state_dict(ckpt["model_state_dict"])
            model.eval()
            model.to(device)
            self._model = model

            with thr_json.open() as f:
                thr = json.load(f)
            self._thresholds = dict(thr.get("tuned", {}).get("per_class_thresholds", {}))

            # Optional: train_meta may override image_size
            meta_json = self.model_dir / "train_meta.json"
            if meta_json.exists():
                try:
                    meta = json.loads(meta_json.read_text())
                    self._image_size = int(meta.get("image_size", self._image_size))
                except Exception as e:
                    logger.warning(
                        "[YOLO-FILTER] train_meta.json load failed at %s: %s: %s — "
                        "keeping default image_size=%d (corrupted meta ignored)",
                        meta_json, type(e).__name__, e, self._image_size,
                    )

            self._transform = transforms.Compose([
                transforms.Resize((self._image_size, self._image_size)),
                transforms.ToTensor(),
                transforms.Normalize(self._imagenet_mean, self._imagenet_std),
            ])
            logger.info(
                f"[YOLO-FILTER] loaded model={self.model_dir.name} device={device} "
                f"classes={self._class_names} image_size={self._image_size} "
                f"thresholds={self._thresholds}"
            )
            return True
        except Exception as e:
            logger.error(f"[YOLO-FILTER] failed to load model: {e!r}; filter DEGRADED")
            self._model = None
            return False

    def _pick_device(self, torch_mod):
        if self.device_pref != "auto":
            return torch_mod.device(self.device_pref)
        if torch_mod.backends.mps.is_available():
            return torch_mod.device("mps")
        if torch_mod.cuda.is_available():
            return torch_mod.device("cuda")
        return torch_mod.device("cpu")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """True if model + thresholds can be loaded (call once at startup)."""
        return self._load()

    def predict(self, symbol: str, as_of: date) -> YoloFilterDecision:
        """Run inference on the chart for (as_of - image_lag_days) and produce a decision.

        Soft-fails (returns degraded decision with no_trade=False) on:
        - model load failure
        - missing chart image
        - any runtime exception
        """
        sym = symbol.upper().strip()
        image_date = as_of - timedelta(days=self.image_lag_days)
        image_path = self.image_dir / f"{sym}_{image_date.isoformat()}_{self.image_time_hhmmss}.jpg"

        # Degraded if model didn't load
        if not self._load():
            return YoloFilterDecision(
                symbol=sym, as_of=as_of, image_date=image_date, image_path=None,
                yolo_probs={}, predicted_class="", predicted_conf=0.0,
                no_trade=False, reason="DEGRADED_NO_MODEL",
                model_id=self.model_id, degraded=True,
            )

        if not image_path.exists():
            logger.debug(f"[YOLO-FILTER] {sym} {as_of}: image missing at {image_path.name}")
            return YoloFilterDecision(
                symbol=sym, as_of=as_of, image_date=image_date,
                image_path=str(image_path),
                yolo_probs={}, predicted_class="", predicted_conf=0.0,
                no_trade=False, reason="IMAGE_MISSING",
                model_id=self.model_id, degraded=True,
            )

        # Inference
        try:
            import torch
            from PIL import Image

            img = Image.open(image_path).convert("RGB")
            x = self._transform(img).unsqueeze(0).to(self._resolved_device)
            with torch.no_grad():
                logits = self._model(x)
                probs = torch.softmax(logits, dim=1).cpu().numpy()[0]
            yolo_probs = {name: float(p) for name, p in zip(self._class_names, probs)}
            pred_idx = int(probs.argmax())
            pred_class = self._class_names[pred_idx]
            pred_conf = float(probs[pred_idx])
        except Exception as e:
            logger.error(f"[YOLO-FILTER] {sym} {as_of}: inference failed: {e!r}")
            return YoloFilterDecision(
                symbol=sym, as_of=as_of, image_date=image_date,
                image_path=str(image_path),
                yolo_probs={}, predicted_class="", predicted_conf=0.0,
                no_trade=False, reason=f"INFERENCE_ERROR:{type(e).__name__}",
                model_id=self.model_id, degraded=True,
            )

        # No-trade logic: high-confidence neutral class exceeds its tuned threshold
        # AND argmax prediction is a neutral class with reasonable confidence.
        blocked_class: Optional[str] = None
        blocked_threshold: Optional[float] = None

        for cls in self.neutral_classes:
            thr = self._thresholds.get(cls)
            prob = yolo_probs.get(cls, 0.0)
            if thr is not None and prob >= thr and pred_class == cls and pred_conf >= self.min_pred_conf:
                blocked_class = cls
                blocked_threshold = thr
                break

        if blocked_class is not None:
            return YoloFilterDecision(
                symbol=sym, as_of=as_of, image_date=image_date,
                image_path=str(image_path),
                yolo_probs=yolo_probs,
                predicted_class=pred_class, predicted_conf=pred_conf,
                no_trade=True,
                reason=f"NEUTRAL_HIGH_CONF_{blocked_class.upper()}",
                blocked_class=blocked_class, threshold_used=blocked_threshold,
                model_id=self.model_id, degraded=False,
            )

        # Otherwise OK — but tag direction for downstream visibility
        if pred_class in self.bullish_classes:
            tag = "BULLISH_PATTERN"
        elif pred_class in self.bearish_classes:
            tag = "BEARISH_PATTERN"
        else:
            tag = "PATTERN_OK"

        return YoloFilterDecision(
            symbol=sym, as_of=as_of, image_date=image_date,
            image_path=str(image_path),
            yolo_probs=yolo_probs,
            predicted_class=pred_class, predicted_conf=pred_conf,
            no_trade=False, reason=tag,
            model_id=self.model_id, degraded=False,
        )

    def summarize_decision(self, dec: YoloFilterDecision) -> str:
        """Compact one-line summary for backtest logs."""
        if dec.no_trade:
            return f"YOLO_BLOCK[{dec.blocked_class}@{dec.threshold_used:.2f}→{dec.predicted_class}={dec.predicted_conf:.2f}]"
        if dec.degraded:
            return f"YOLO_DEGRADED[{dec.reason}]"
        return f"YOLO_OK[{dec.predicted_class}={dec.predicted_conf:.2f}]"


__all__ = [
    "YoloFilterDecision",
    "YoloNoTradeFilter",
    "DEFAULT_NEUTRAL_CLASSES",
    "DEFAULT_BULLISH_CLASSES",
    "DEFAULT_BEARISH_CLASSES",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_IMAGE_DIR",
]