"""
Fusion Engine V2.2
Multi-signal fusion for trading decisions.
Enhanced by Gemini Deep Review Round 2.

Core Logic:
- RL provides continuous position signal
- LLM provides directional validation
- YOLO provides pattern confirmation
- All signals fused with regime-aware multipliers
- V2.2: Dynamic exponential weighting via signal-source win-rate (Redis)
"""

import time
import json
import os
import threading
import logging
from loguru import logger
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass, field
from enum import Enum

# Import our V2.1 components
from services.latency_hedge import latency_hedge
from services.llm_reasoning_scorer import llm_reasoning_scorer
from services.gap_risk_manager import gap_risk_manager, GapType
from services.signal_expiry import signal_expiry_checker, ExpiryAction

# Phase 0.1 hard switch:
# disable RL contribution until Phase 3 retrain validates positive edge.
RL_WEIGHT = 0.0


class FusionStrategy(Enum):
    """融合策略"""
    CONSERVATIVE = "conservative"   # 保守策略
    AGGRESSIVE = "aggressive"     # 激进策略
    BALANCED = "balanced"         # 平衡策略


class ConflictResolution(Enum):
    """冲突解决方式"""
    HOLD = "hold"               # 保守持有
    FOLLOW_RL = "follow_rl"         # 跟随 RL
    FOLLOW_LLM = "follow_llm"       # 跟随 LLM
    REDUCED = "reduced"           # 降级执行


@dataclass
class FusionDecision:
    """融合决策结果"""
    symbol: str
    side: str                    # BUY / SELL / HOLD
    size: float                  # 0.0 ~ 1.0
    confidence: float              # 0.0 ~ 1.0
    sources: List[str]             # ["RL", "LLM", "YOLO"]
    signal_age_ms: float          # 信号年龄
    regime_multiplier: float      # Regime 乘数
    latency_multiplier: float     # 延迟乘数
    slippage_factor: float        # 滑点因子
    reasoning_boost: float        # LLM 推理加成
    gap_adjustment: float         # 跳空调整
    conflict_resolved: bool        # 是否经过冲突解决
    reason: str                   # 决策原因
    dominant_source: str = "LLM"  # V2.2: 主要信号源 ("RL" | "LLM" | "YOLO")

    metadata: Dict = field(default_factory=dict)


@dataclass
class FusionInput:
    """融合输入数据"""
    symbol: str
    rl_target: float             # RL 目标仓位 [-1, 1]
    llm_side: str               # LLM 方向 BUY/SELL/HOLD
    llm_confidence: float         # LLM 置信度
    llm_reasoning: str           # LLM 推理文本
    regime: str                 # 市场情绪
    yolo_confidence: float       # YOLO 置信度
    yolo_pattern: str           # YOLO 模式类型
    bid: float                 # 当前买价
    ask: float                 # 当前卖价
    prev_close: Optional[float] = None  # 前收盘价 (用于 Gap 检测)
    open_price: Optional[float] = None  # 开盘价 (用于 Gap 检测)
    event_timestamp: Optional[float] = None  # 事件时间戳


class FusionEngine:
    """
    融合引擎 V2.1

    将 RL、LLM、YOLO、Regime 等多个信号融合为最终交易决策。
    整合了 Gemini Deep Review Round 2 的所有建议。
    """

    # 6-level Regime 乘数 (V2.0)
    REGIME_MULTIPLIERS = {
        "BULL_LONG":   {"buy": 1.3, "sell": 0.3},
        "BULL_SHORT":  {"buy": 1.1, "sell": 0.6},
        "NEUTRAL":     {"buy": 1.0, "sell": 1.0},
        "BEAR_SHORT":  {"buy": 0.6, "sell": 1.1},
        "BEAR_LONG":  {"buy": 0.3, "sell": 1.3},
        "PANIC":       {"buy": 0.1, "sell": 0.1, "hard_lock": True},
    }

    # 冲突解决阈值
    STRONG_RL_THRESHOLD = 0.5      # 强 RL 信号阈值
    WEAK_RL_THRESHOLD = 0.1       # 弱 RL 信号阈值
    NEUTRAL_RL_THRESHOLD = 0.1   # 中性 RL 阈值

    # 仓位限制
    MAX_POSITION_SIZE = 0.95       # 最大仓位
    MIN_POSITION_SIZE = 0.1        # 最小有效仓位

    def __init__(self, strategy: FusionStrategy = FusionStrategy.CONSERVATIVE):
        self.strategy = strategy
        self.logger = logger
        # V2.2: Dynamic weights — three-tier fallback (Redis → regime default → local → 1.0)
        self._weights: Dict[str, float] = {}
        # Unix timestamp (seconds) when _weights was last loaded from Redis; None = never loaded
        self._weights_timestamp: Optional[float] = None
        self._LOCAL_WEIGHT_FRESHNESS_SEC = 1800  # 30 minutes
        # Phase 0.4: RL DISABLED — Sharpe -33, feature pipeline mismatch (v15 → v18).
        # w_rl=0.0 makes rl_signal = abs(target)^0.0 = 1.0 (neutral multiplier).
        # Re-enable only after RL retraining achieves Sharpe > 0.0 (Phase 2.2).
        self._regime_fallback = {
            "BULL_LONG":   {"w_rl": 0.0, "w_llm": 1.0, "w_yolo": 0.9},
            "BULL_SHORT":  {"w_rl": 0.0, "w_llm": 1.0, "w_yolo": 1.0},
            "NEUTRAL":     {"w_rl": 0.0, "w_llm": 1.0, "w_yolo": 1.0},
            "BEAR_SHORT":  {"w_rl": 0.0, "w_llm": 1.1, "w_yolo": 1.2},
            "BEAR_LONG":   {"w_rl": 0.0, "w_llm": 1.2, "w_yolo": 1.3},
            "PANIC":       {"w_rl": 0.0, "w_llm": 1.3, "w_yolo": 1.5},
        }
        # Hardcoded safe defaults — used when Redis weights are stale (> 30 min) or unavailable.
        self._local_default_weights: Dict[str, float] = {
            "w_rl": 0.0, "w_llm": 1.0, "w_yolo": 1.0,
        }
        self._redis_client = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._pubsub_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._load_weights()
        if os.getenv("FUSION_WEIGHTS_ENABLED", "true").lower() == "true":
            self._refresh_weights_periodically(interval_sec=300)
            self._start_weight_listener()

    def fuse(self, input: FusionInput) -> FusionDecision:
        """
        执行多信号融合

        Args:
            input: FusionInput 包含所有输入信号

        Returns:
            FusionDecision: 最终融合决策
        """
        start_time = time.time()
        rl_disabled = RL_WEIGHT == 0.0
        effective_rl_target = 0.0 if rl_disabled else input.rl_target
        decision_direction = input.llm_side if rl_disabled else self._get_rl_direction(effective_rl_target)

        if rl_disabled:
            self.logger.warning(
                f"RL signal skipped for {input.symbol} (Phase 0.1, RL_WEIGHT=0.0)"
            )

        # 1. Signal Expiry Check
        expiry_result = signal_expiry_checker.validate(
            symbol=input.symbol,
            current_yolo_conf=input.yolo_confidence,
            current_rl_target=effective_rl_target,
            current_volatility=self._get_current_volatility(input.symbol)
        )

        if expiry_result.action == ExpiryAction.ABORT:
            return self._create_abort_decision(input, expiry_result)

        # 2. Gap Risk Check
        if input.prev_close and input.open_price:
            gap_result = gap_risk_manager.detect_gap(
                symbol=input.symbol,
                prev_close=input.prev_close,
                open_price=input.open_price,
                current_atr=self._get_current_volatility(input.symbol)
            )

            # 更新 Gap 类型基于 RL 方向
            if gap_result:
                gap_risk_manager.classify_gap_direction(
                    symbol=input.symbol,
                    rl_direction=decision_direction
                )

            # 检查是否应该延迟交易
            if gap_risk_manager.should_delay_trading(input.symbol):
                return self._create_gap_delay_decision(input, gap_result)

        # 3. Latency Hedge Check
        latency_mult = latency_hedge.get_latency_multiplier()

        if latency_mult == 0:
            return self._create_latency_halt_decision(input)

        # 4. Slippage Factor
        slippage_factor = latency_hedge.get_slippage_factor(input.bid, input.ask)
        if slippage_factor < 0.3:
            self.logger.warning(f"⚠️ Extreme slippage for {input.symbol}: {slippage_factor:.2f}")
            return self._create_slippage_abort_decision(input, slippage_factor)
        # 5. Direction Consistency Check
        rl_direction = decision_direction
        if (not rl_disabled) and rl_direction != input.llm_side:
            # 方向冲突
            return self._resolve_conflict(input, rl_direction)
        # 6. LLM Reasoning Score
        reasoning_score, reasoning_boost = llm_reasoning_scorer.score(
            llm_reasoning=input.llm_reasoning,
            event_timestamp=input.event_timestamp,
        )
        # 7. Regime Multiplier
        regime_mult = self._get_regime_multiplier(input.regime, decision_direction)
        # 8. Gap Adjustment (如果 GapRiskManager 触发了 Gap)
        gap_adjustment = 1.0
        if input.prev_close and input.open_price:
            gap_event = gap_risk_manager.gapped_symbols.get(input.symbol)
            if gap_event:
                gap_adjustment = self._get_gap_adjustment(gap_event)

        # 9. Calculate Final Size (V2.2: exponential weighting)
        weights = self._get_weights_for_regime(input.regime)
        w_rl, w_llm, w_yolo = weights["w_rl"], weights["w_llm"], weights["w_yolo"]

        rl_signal = 1.0 if rl_disabled else abs(effective_rl_target) ** w_rl
        llm_signal  = input.llm_confidence ** w_llm
        yolo_signal = input.yolo_confidence ** w_yolo

        final_size = (
            rl_signal
            * llm_signal
            * yolo_signal
            * regime_mult
            * latency_mult
            * slippage_factor
            * reasoning_boost
            * gap_adjustment
        )

        # Track dominant source for PostMortem feedback
        source_scores = {"RL": rl_signal, "LLM": llm_signal, "YOLO": yolo_signal}
        dominant_source = max(source_scores, key=source_scores.get)

        self.logger.debug(
            f"Weights: w_rl={w_rl:.2f} w_llm={w_llm:.2f} w_yolo={w_yolo:.2f} | "
            f"Scores: RL={rl_signal:.4f} LLM={llm_signal:.4f} YOLO={yolo_signal:.4f} | "
            f"Dominant: {dominant_source}"
        )

        if w_rl == 0.0:
            self.logger.debug("RL signal neutralised (w_rl=0.0, Phase 0.4 — RL disabled)")

        # 10. Apply Position Limits
        final_size = max(self.MIN_POSITION_SIZE, min(final_size, self.MAX_POSITION_SIZE))

        if final_size < self.MIN_POSITION_SIZE:
            return self._create_below_minimum_decision(input, final_size)
        # 11. Calculate Final Confidence
        final_confidence = (input.yolo_confidence + input.llm_confidence) / 2
        # 12. Create Final Decision
        signal_age_ms = (time.time() - start_time) * 1000
        return FusionDecision(
            symbol=input.symbol,
            side=decision_direction,
            size=final_size,
            confidence=final_confidence,
            sources=["RL", "LLM", "YOLO"],
            signal_age_ms=signal_age_ms,
            regime_multiplier=regime_mult,
            latency_multiplier=latency_mult,
            slippage_factor=slippage_factor,
            reasoning_boost=reasoning_boost,
            gap_adjustment=gap_adjustment,
            conflict_resolved=False,
            reason="Fusion successful",
            dominant_source=dominant_source,
            metadata={
                "rl_target": input.rl_target,
                "effective_rl_target": effective_rl_target,
                "rl_disabled": rl_disabled,
                "llm_side": input.llm_side,
                "yolo_pattern": input.yolo_pattern,
                "regime": input.regime,
                "weights": {"w_rl": w_rl, "w_llm": w_llm, "w_yolo": w_yolo},
                "source_scores": source_scores,
            },
        )

    def _get_rl_direction(self, rl_target: float) -> str:
        """将 RL target 转换为方向"""
        if rl_target > 0.05:
            return "BUY"
        elif rl_target < -0.05:
            return "SELL"
        return "HOLD"

    def _get_regime_multiplier(self, regime: str, direction: str) -> float:
        """获取 Regime 乘数"""
        regime_config = self.REGIME_MULTIPLIERS.get(regime, self.REGIME_MULTIPLIERS["NEUTRAL"])
        direction_key = "buy" if direction == "BUY" else "sell"
        return regime_config.get(direction_key, 1.0)
    def _get_gap_adjustment(self, gap_event) -> float:
        """根据 Gap 事件调整仓位"""
        if gap_event.gap_type == GapType.REVERSAL:
            return 0.3  # 逆势跳空大幅降仓
        elif gap_event.gap_type == GapType.EXHAUSTION:
            return 0.2  # 流动性枯竭
        elif gap_event.gap_type == GapType.TREND_FOLLOWING:
            return 0.7  # 顺势跳空小幅降仓
        return 1.0
    def _resolve_conflict(
        self,
        input: FusionInput,
        rl_direction: str
    ) -> FusionDecision:
        """解决 RL 和 LLM 方向冲突"""
        # 强 RL + 反向 LLM = HOLD
        if abs(input.rl_target) >= self.STRONG_RL_THRESHOLD and rl_direction != input.llm_side:
            if input.llm_side != "HOLD":
                self.logger.warning(
                    f"⚠️ Strong RL ({input.rl_target:.2f}) conflicts with LLM ({input.llm_side})"
                )
                return self._create_conflict_decision(
                    input, rl_direction, "Strong RL vs LLM conflict"
                )
        # 弱 RL + HOLD = 跟随 RL， 减半
        if abs(input.rl_target) < self.WEAK_RL_THRESHOLD and input.llm_side == "HOLD":
            self.logger.info(f"📊 Weak RL with LLM HOLD, following RL at 0.5x")
            return self._create_follow_rl_decision(input, rl_direction, 0.5)
        # 中性 RL + LLM Signal = 限制仓位
        if abs(input.rl_target) < self.NEUTRAL_RL_THRESHOLD and input.llm_side != "HOLD":
            # 使用 LLM Reasoning Score 调整
            reasoning_score, reasoning_boost = llm_reasoning_scorer.score(
                llm_reasoning=input.llm_reasoning,
                event_timestamp=input.event_timestamp,
            )
            size_limit = 0.25
            if reasoning_boost > 1.2:
                size_limit = 0.4  # 宏观事件加成
            self.logger.info(f"📊 Neutral RL + LLM ({input.llm_side}) with reasoning boost {reasoning_boost:.2f}")
            return self._create_follow_llm_decision(input, size_limit, reasoning_boost)
        # 默认: HOLD
        return self._create_conflict_decision(input, "HOLD", "Default conflict resolution")
    def _create_abort_decision(
        self,
        input: FusionInput,
        expiry_result
    ) -> FusionDecision:
        """创建信号过期决策"""
        return FusionDecision(
            symbol=input.symbol,
            side="HOLD",
            size=0.0,
            confidence=0.0,
            sources=["EXPIRED"],
            signal_age_ms=expiry_result.signal_age_ms,
            regime_multiplier=0.0,
            latency_multiplier=0.0,
            slippage_factor=0.0,
            reasoning_boost=1.0,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason=expiry_result.reason,
        )
    def _create_gap_delay_decision(
        self,
        input: FusionInput,
        gap_result
    ) -> FusionDecision:
        """创建 Gap 延迟决策"""
        return FusionDecision(
            symbol=input.symbol,
            side="HOLD",
            size=0.0,
            confidence=0.0,
            sources=["GAP_DELAY"],
            signal_age_ms=0,
            regime_multiplier=0.0,
            latency_multiplier=0.0,
            slippage_factor=0.0,
            reasoning_boost=1.0,
            gap_adjustment=0.0,
            conflict_resolved=True,
            reason=f"Gap observation mode: {gap_result.gap_type.value}",
        )
    def _create_latency_halt_decision(self, input: FusionInput) -> FusionDecision:
        """创建延迟熔断决策"""
        return FusionDecision(
            symbol=input.symbol,
            side="HOLD",
            size=0.0,
            confidence=0.0,
            sources=["LATENCY_HALT"],
            signal_age_ms=0,
            regime_multiplier=0.0,
            latency_multiplier=0.0,
            slippage_factor=0.0,
            reasoning_boost=1.0,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason="Network latency exceeds critical threshold",
        )
    def _create_slippage_abort_decision(
        self,
        input: FusionInput,
        slippage_factor: float
    ) -> FusionDecision:
        """创建滑点过高决策"""
        return FusionDecision(
            symbol=input.symbol,
            side="HOLD",
            size=0.0,
            confidence=0.0,
            sources=["SLIPPAGE"],
            signal_age_ms=0,
            regime_multiplier=0.0,
            latency_multiplier=0.0,
            slippage_factor=slippage_factor,
            reasoning_boost=1.0,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason=f"Bid-Ask spread too high: {slippage_factor:.2f}",
        )
    def _create_below_minimum_decision(
        self,
        input: FusionInput,
        calculated_size: float
    ) -> FusionDecision:
        """创建低于最小仓位决策"""
        return FusionDecision(
            symbol=input.symbol,
            side="HOLD",
            size=0.0,
            confidence=0.0,
            sources=["BELOW_MIN"],
            signal_age_ms=0,
            regime_multiplier=0.0,
            latency_multiplier=0.0,
            slippage_factor=0.0,
            reasoning_boost=1.0,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason=f"Calculated size {calculated_size:.4f} below minimum {self.MIN_POSITION_SIZE}",
        )
    def _create_conflict_decision(
        self,
        input: FusionInput,
        resolved_direction: str,
        reason: str
    ) -> FusionDecision:
        """创建冲突解决决策"""
        return FusionDecision(
            symbol=input.symbol,
            side=resolved_direction,
            size=0.0,
            confidence=0.0,
            sources=["CONFLICT"],
            signal_age_ms=0,
            regime_multiplier=0.0,
            latency_multiplier=0.0,
            slippage_factor=0.0,
            reasoning_boost=1.0,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason=reason,
        )
    def _create_follow_rl_decision(
        self,
        input: FusionInput,
        direction: str,
        size_multiplier: float
    ) -> FusionDecision:
        """创建跟随 RL 决策"""
        regime_mult = self._get_regime_multiplier(input.regime, direction)
        base_size = abs(input.rl_target) * size_multiplier
        final_size = base_size * regime_mult * input.yolo_confidence * input.llm_confidence
        final_size = min(final_size, self.MAX_POSITION_SIZE)
        return FusionDecision(
            symbol=input.symbol,
            side=direction,
            size=final_size,
            confidence=input.llm_confidence * 0.5,  # LLM HOLD 时置信度减半
            sources=["RL", "LLM_CAUTIOUS"],
            signal_age_ms=0,
            regime_multiplier=regime_mult,
            latency_multiplier=latency_hedge.get_latency_multiplier(),
            slippage_factor=latency_hedge.get_slippage_factor(input.bid, input.ask),
            reasoning_boost=1.0,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason=f"Following RL despite LLM HOLD at {size_multiplier}x",
        )
    def _create_follow_llm_decision(
        self,
        input: FusionInput,
        size_limit: float,
        reasoning_boost: float
    ) -> FusionDecision:
        """创建跟随 LLM 决策"""
        regime_mult = self._get_regime_multiplier(input.regime, input.llm_side)
        latency_mult = latency_hedge.get_latency_multiplier()
        slippage_factor = latency_hedge.get_slippage_factor(input.bid, input.ask)
        final_size = size_limit * regime_mult * latency_mult * slippage_factor * reasoning_boost
        final_size = min(final_size, self.MAX_POSITION_SIZE)
        return FusionDecision(
            symbol=input.symbol,
            side=input.llm_side,
            size=final_size,
            confidence=input.llm_confidence,
            sources=["LLM", "RL_NEUTRAL"],
            signal_age_ms=0,
            regime_multiplier=regime_mult,
            latency_multiplier=latency_mult,
            slippage_factor=slippage_factor,
            reasoning_boost=reasoning_boost,
            gap_adjustment=1.0,
            conflict_resolved=True,
            reason=f"Following LLM with neutral RL, size limit: {size_limit}, reasoning boost: {reasoning_boost:.2f}",
        )
    def _get_current_volatility(self, symbol: str) -> Optional[float]:
        """获取当前波动率 (ATR%).

        P3: Reads from Redis market_data:atr_pct:{symbol}:* keys or
        watchlist tier1/tier2 details. Returns None if unavailable.
        """
        r = self._get_redis()
        if not r:
            return None

        # Try ATR cache directly
        import json
        from datetime import datetime
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            raw = r.get(f"market_data:atr_pct:{symbol}:{date_str}")
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        val = data.get("value")
                        return float(val) if val and val > 0 else None
                    return float(data)
                except (json.JSONDecodeError, ValueError):
                    return float(raw)
        except Exception:
            pass

        # Fallback: check watchlist detail
        for detail_key in ("watchlist:tier1:details", "watchlist:tier2:details"):
            try:
                raw = r.hget(detail_key, symbol)
                if raw:
                    data = json.loads(raw)
                    atr_pct = data.get("atr_percent") or data.get("atr_pct")
                    if atr_pct:
                        return float(atr_pct)
            except Exception:
                pass

        return None

    # ─────────────────────────────────────────────
    #  V2.2: Dynamic Weight Management
    # ─────────────────────────────────────────────

    def _get_redis(self):
        """Lazy Redis connection."""
        if self._redis_client is None:
            try:
                import redis
                self._redis_client = redis.Redis(
                    host=os.getenv("REDIS_HOST", "localhost"),
                    port=int(os.getenv("REDIS_PORT", 6379)),
                    decode_responses=True,
                )
                # Test connection
                self._redis_client.ping()
            except Exception as e:
                self.logger.warning(f"FusionEngine Redis unavailable: {e}")
                self._redis_client = None
        return self._redis_client

    def _load_weights(self):
        """Load weights from Redis. Falls back to local defaults then regime fallback.

        Dual-protection: checks ts (written by PostMortem) for freshness.
        If ts is absent or older than _LOCAL_WEIGHT_FRESHNESS_SEC, uses local defaults.
        """
        import time as _time

        r = self._get_redis()
        if r:
            try:
                raw = r.get("fusion:weights:current")
                if raw:
                    data = json.loads(raw)
                    ts = data.get("ts")
                    if ts and (isinstance(ts, (int, float))
                               and (_time.time() - ts) <= self._LOCAL_WEIGHT_FRESHNESS_SEC):
                        # Fresh — strip ts before storing
                        self._weights = {k: v for k, v in data.items() if k != "ts"}
                        self._weights_timestamp = float(ts)
                        self.logger.info(
                            f"Weights loaded from Redis (ts={ts}, fresh): {self._weights}"
                        )
                        return
                    elif ts:
                        age = _time.time() - float(ts)
                        self.logger.warning(
                            f"Redis weights stale (age={age:.0f}s > {self._LOCAL_WEIGHT_FRESHNESS_SEC}s) "
                            f"— using local defaults"
                        )
                    else:
                        self.logger.warning(
                            "Redis weights missing ts field — using local defaults"
                        )
            except Exception as e:
                self.logger.warning(f"Failed to load weights from Redis: {e}")

        # Local safe defaults — used when Redis is unavailable, stale, or unparseable
        self._weights = self._local_default_weights.copy()
        self._weights_timestamp = None
        self.logger.info(
            f"Using local default weights (no fresh Redis data): {self._weights}"
        )

    def _get_weights_for_regime(self, regime: str) -> Dict[str, float]:
        """Return w_rl, w_llm, w_yolo for the given regime.

        Priority (4 tiers):
            1. Redis weights (fresh, ts < 30 min) — set by PostMortem nightly
            2. Local default weights (safe unit defaults) — used when Redis is stale/unavailable
            3. Regime-specific multipliers from _regime_fallback
            4. Unit multipliers (1.0) for any weight not in any of the above
        """
        # Regime defaults
        regime_defaults = self._regime_fallback.get(
            regime,
            {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}
        )

        # Layer on Redis weights (they apply across all regimes)
        # When Redis weights exist they represent per-source multipliers
        # relative to the regime defaults, so we combine multiplicatively.
        w_rl = regime_defaults["w_rl"] * self._weights.get("w_rl", 1.0)
        w_llm = regime_defaults["w_llm"] * self._weights.get("w_llm", 1.0)
        w_yolo = regime_defaults["w_yolo"] * self._weights.get("w_yolo", 1.0)

        return {"w_rl": w_rl, "w_llm": w_llm, "w_yolo": w_yolo}

    def _refresh_weights_periodically(self, interval_sec: int = 300):
        """Background thread: reloads weights from Redis every `interval_sec` seconds."""
        def _run():
            while not self._stop_event.wait(interval_sec):
                self._load_weights()

        self._refresh_thread = threading.Thread(target=_run, daemon=True)
        self._refresh_thread.start()
        self.logger.info(f"Started weight refresh thread (interval={interval_sec}s)")

    def _start_weight_listener(self):
        """Background thread: subscribes to Redis Pub/Sub for real-time weight updates."""
        def _listen():
            try:
                r = self._get_redis()
                if not r:
                    return
                pubsub = r.pubsub()
                pubsub.subscribe("fusion:weights:updated")
                self.logger.info("FusionEngine subscribed to fusion:weights:updated")
                for message in pubsub.listen():
                    if self._stop_event.is_set():
                        break
                    if message["type"] == "message":
                        channel = message.get("channel", "")
                        if isinstance(channel, bytes):
                            channel = channel.decode("utf-8")
                        if channel == "fusion:weights:updated":
                            try:
                                import time as _time
                                data = json.loads(message["data"])
                                ts = data.get("ts")
                                if ts and (isinstance(ts, (int, float))
                                           and (_time.time() - float(ts))
                                           <= self._LOCAL_WEIGHT_FRESHNESS_SEC):
                                    self._weights = {k: v for k, v in data.items() if k != "ts"}
                                    self._weights_timestamp = float(ts)
                                    self.logger.info(
                                        f"Real-time weight update (ts={ts}, fresh): {self._weights}"
                                    )
                                elif ts:
                                    age = _time.time() - float(ts)
                                    self.logger.warning(
                                        f"Pub/Sub weight update rejected — stale (age={age:.0f}s): {data}"
                                    )
                                else:
                                    self._weights = data
                                    self._weights_timestamp = None
                                    self.logger.warning(
                                        f"Pub/Sub weight update accepted (no ts): {self._weights}"
                                    )
                            except Exception as e:
                                self.logger.warning(
                                    f"Failed to parse weight update: {e}"
                                )
            except Exception as e:
                self.logger.warning(f"Weight listener thread exited: {e}")

        self._pubsub_thread = threading.Thread(target=_listen, daemon=True)
        self._pubsub_thread.start()

    def stop(self):
        """Stop background threads. Call on shutdown."""
        self._stop_event.set()
        if self._redis_client:
            try:
                self._redis_client.close()
            except Exception:
                pass


# Singleton instance
fusion_engine = FusionEngine()
