"""
Semantic Distiller — Causal Cognition Layer
从实时 OHLCV 数据流中提取"因果补丁"(Causal Patches)

核心算法:
    当价格位移 > 2.5 × ATR 时，结合 Regime + Volume 生成结构化因果补丁，
    写入 Redis (market:causal_context) 供 HeadTraderNode 实时对齐。

生命周期位置: 09:30 - 15:45 (盘中实时运行)
"""

import os
import sys
import json
import time
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("SemanticDistiller")


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────

@dataclass
class CausalPatch:
    """因果补丁：描述一次被检测到的因果性价格位移"""
    symbol: str
    timestamp: str
    pattern: str                    # e.g. "Volatility_Spike", "Liquidity_Trap"
    causal_chain: str               # 人类可读的因果描述
    patch_type: str = "Causal_Patch"
    severity: float = 0.0           # 0-10 严重程度
    regime_at_detection: str = ""
    volume_ratio: float = 0.0       # 当前成交量 / 20日均量
    price_change_pct: float = 0.0
    atr_multiple: float = 0.0       # 价格位移 / ATR 的倍数
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ─────────────────────────────────────────────
# Semantic Distiller Engine
# ─────────────────────────────────────────────

class SemanticDistiller:
    """
    认知层核心：从原始市场数据中提取因果关系

    Pipeline:
        1. 接收 OHLCV 数据 (从 Kafka 或 Redis)
        2. 计算 14-day ATR
        3. 检测 > 2.5x ATR 的异常位移
        4. 结合 Regime + Volume 生成语义标签
        5. 写入 Redis: market:causal_context
    """

    # ── 检测阈值 ──
    ATR_SPIKE_THRESHOLD = 2.5       # 价格位移超过 ATR 的倍数触发
    ATR_EXTREME_THRESHOLD = 4.0     # 极端波动阈值
    VOLUME_SPIKE_RATIO = 2.0        # 成交量突增倍数
    CONTEXT_WINDOW = 30             # 仅关注最近 30 天数据
    ATR_PERIOD = 14                 # ATR 计算周期

    # ── 语义映射表 ──
    PATTERN_TAXONOMY = {
        # (atr_level, volume_level, regime_class) -> pattern_name
        ("spike", "high", "bearish"):   "Panic_Selloff",
        ("spike", "high", "bullish"):   "Momentum_Surge",
        ("spike", "high", "neutral"):   "Liquidity_Trap",
        ("spike", "low", "bearish"):    "Desiccated_Macro_Drift",
        ("spike", "low", "bullish"):    "Stealth_Accumulation",
        ("spike", "low", "neutral"):    "Low_Conviction_Spike",
        ("extreme", "high", "bearish"): "Flash_Crash",
        ("extreme", "high", "bullish"): "Euphoric_Breakout",
        ("extreme", "high", "neutral"): "Regime_Transition",
        ("extreme", "low", "bearish"):  "Illiquidity_Crisis",
        ("extreme", "low", "bullish"):  "Silent_Breakout",
        ("extreme", "low", "neutral"):  "Ghost_Move",
    }

    BEARISH_REGIMES = {"PANIC", "BEAR_LONG", "BEAR_SHORT", "RANGE_BEARISH", "EXTREME_VOL"}
    BULLISH_REGIMES = {"BULL_LONG", "BULL_SHORT", "RANGE_BULLISH"}

    def __init__(self, redis_client=None, kafka_consumer=None):
        """
        Args:
            redis_client: Redis 连接 (用于读取 regime 和写入补丁)
            kafka_consumer: Kafka consumer (可选，用于实时订阅 OHLCV)
        """
        self.redis = redis_client
        self.kafka = kafka_consumer
        self._patch_buffer: List[CausalPatch] = []  # 今日补丁缓存

    # ─────────────────────────────────────────
    # Core: ATR Calculation
    # ─────────────────────────────────────────

    def _calculate_atr(self, df: pd.DataFrame, period: int = None) -> float:
        """计算 Average True Range (ATR)"""
        period = period or self.ATR_PERIOD

        if len(df) < period + 1:
            logger.warning(f"Insufficient data for ATR (need {period + 1}, got {len(df)})")
            return 0.0

        high = df['high'].values
        low = df['low'].values
        close = df['close'].values

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1])
            )
        )

        atr = np.mean(tr[-period:])
        return float(atr) if not np.isnan(atr) else 0.0

    # ─────────────────────────────────────────
    # Core: Causal Patch Detection
    # ─────────────────────────────────────────

    def distill(self, symbol: str, df: pd.DataFrame) -> Optional[CausalPatch]:
        """
        对单个标的进行因果蒸馏

        Args:
            symbol: 股票代码
            df: OHLCV DataFrame (需包含 open, high, low, close, volume)

        Returns:
            CausalPatch if detected, else None
        """
        if df is None or len(df) < self.CONTEXT_WINDOW:
            return None

        # 仅取最近 N 天
        df = df.tail(self.CONTEXT_WINDOW).copy()

        # 1. 计算 ATR
        atr = self._calculate_atr(df)
        if atr <= 0:
            return None

        # 2. 计算最新一根 K 线的价格位移
        last_row = df.iloc[-1]
        price_change = abs(last_row['close'] - last_row['open'])
        atr_multiple = price_change / atr

        # 3. 阈值检查
        if atr_multiple < self.ATR_SPIKE_THRESHOLD:
            return None  # 正常波动，不生成补丁

        # 4. 成交量分析
        avg_volume = df['volume'].rolling(window=20).mean().iloc[-1]
        volume_ratio = last_row['volume'] / avg_volume if avg_volume > 0 else 0

        # 5. 获取当前 Regime
        regime = self._get_current_regime()

        # 6. 语义映射
        atr_level = "extreme" if atr_multiple >= self.ATR_EXTREME_THRESHOLD else "spike"
        volume_level = "high" if volume_ratio >= self.VOLUME_SPIKE_RATIO else "low"
        regime_class = self._classify_regime(regime)

        pattern = self.PATTERN_TAXONOMY.get(
            (atr_level, volume_level, regime_class),
            "Unclassified_Anomaly"
        )

        # 7. 计算严重程度 (0-10)
        severity = min(10.0, atr_multiple * 2.0 + (volume_ratio - 1.0) * 0.5)

        # 8. 构建因果链描述
        direction = "↑" if last_row['close'] > last_row['open'] else "↓"
        causal_chain = (
            f"{symbol} {direction} ${price_change:.2f} "
            f"({atr_multiple:.1f}x ATR) | "
            f"Vol {volume_ratio:.1f}x avg | "
            f"Regime: {regime}"
        )

        patch = CausalPatch(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pattern=pattern,
            causal_chain=causal_chain,
            severity=round(severity, 2),
            regime_at_detection=regime,
            volume_ratio=round(volume_ratio, 2),
            price_change_pct=round(price_change / last_row['open'] * 100, 3),
            atr_multiple=round(atr_multiple, 2),
            metadata={
                "atr": round(atr, 4),
                "close": round(last_row['close'], 2),
                "open": round(last_row['open'], 2),
                "volume": int(last_row['volume']),
            }
        )

        logger.info(
            f"🧠 Causal Patch Detected: {pattern} | "
            f"{symbol} {atr_multiple:.1f}x ATR | severity={severity:.1f}"
        )

        return patch

    def distill_batch(self, data: Dict[str, pd.DataFrame]) -> List[CausalPatch]:
        """
        批量蒸馏多个标的

        Args:
            data: {symbol: DataFrame} 映射

        Returns:
            检测到的补丁列表
        """
        patches = []
        for symbol, df in data.items():
            try:
                patch = self.distill(symbol, df)
                if patch:
                    patches.append(patch)
            except Exception as e:
                logger.error(f"Error distilling {symbol}: {e}")

        if patches:
            logger.info(f"🧠 Batch distillation complete: {len(patches)} patches from {len(data)} symbols")

        return patches

    # ─────────────────────────────────────────
    # Redis Integration
    # ─────────────────────────────────────────

    def publish_to_redis(self, patches: List[CausalPatch]):
        """将因果补丁写入 Redis 供 HeadTraderNode 消费"""
        if not self.redis or not patches:
            return

        try:
            # 1. 写入当日补丁列表
            patch_dicts = [p.to_dict() for p in patches]
            self.redis.set(
                "causal_patches:today",
                json.dumps(patch_dicts, default=str),
                ex=86400  # 24h TTL
            )

            # 2. 写入实时因果上下文 (供 HeadTrader 毫秒级对齐)
            context = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "patch_count": len(patches),
                "severity_max": max(p.severity for p in patches),
                "dominant_pattern": max(patches, key=lambda p: p.severity).pattern,
                "symbols_affected": list(set(p.symbol for p in patches)),
                "patches": patch_dicts[-5:],  # 最近 5 个补丁
            }
            self.redis.set(
                "market:causal_context",
                json.dumps(context, default=str),
                ex=3600  # 1h TTL
            )

            # 3. 按 symbol 写入个股上下文
            for patch in patches:
                self.redis.set(
                    f"causal:{patch.symbol}",
                    patch.to_json(),
                    ex=3600
                )

            logger.info(f"📤 Published {len(patches)} causal patches to Redis")

        except Exception as e:
            logger.error(f"Failed to publish patches to Redis: {e}")

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def _get_current_regime(self) -> str:
        """从 Redis 获取当前市场状态"""
        if not self.redis:
            return "UNKNOWN"
        try:
            data = self.redis.get("market_regime:current")
            if data:
                return json.loads(data).get("regime", "UNKNOWN")
        except Exception:
            pass
        return "UNKNOWN"

    def _classify_regime(self, regime: str) -> str:
        """将 regime 分类为 bullish/bearish/neutral"""
        regime_upper = regime.upper()
        if regime_upper in self.BEARISH_REGIMES:
            return "bearish"
        elif regime_upper in self.BULLISH_REGIMES:
            return "bullish"
        return "neutral"


# ─────────────────────────────────────────────
# Standalone Entry Point (for launchd / cron)
# ─────────────────────────────────────────────

def run_distillation():
    """
    独立运行入口：从 Redis watchlist 获取数据并执行蒸馏
    适合作为 launchd 任务定期执行 (盘中每 15-30 分钟)
    """
    import redis as redis_lib

    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    r = redis_lib.Redis(host=redis_host, port=redis_port, db=0, decode_responses=True)

    distiller = SemanticDistiller(redis_client=r)

    # 1. 从 Redis tier2 获取当前关注的 symbols (L2 CatalystScorer output)
    watchlist_raw = r.zrevrange("watchlist:tier2", 0, -1, withscores=True)
    if not watchlist_raw:
        logger.info("No active tier2 watchlist found. Skipping distillation.")
        return

    symbols = [s for s, _ in watchlist_raw]
    logger.info(f"Distilling {len(symbols)} symbols from watchlist:tier2...")

    # 2. 从 Redis 或 TimescaleDB 获取 OHLCV 数据 (P2: with freshness check)
    from core.data_freshness import check_freshness
    data = {}
    skipped_stale = 0
    for symbol in symbols[:20]:  # Cap at 20 for performance
        try:
            ohlcv_raw = r.get(f"ohlcv:daily:{symbol}")
            if ohlcv_raw:
                ohlcv_data = json.loads(ohlcv_raw)
                # P2: Check OHLCV data freshness (max 24h)
                freshness = check_freshness(r, f"ohlcv:daily:{symbol}", max_age_override=86400)
                if not freshness["ok"]:
                    logger.warning(
                        f"⚠️ OHLCV data stale for {symbol}: "
                        f"age={freshness['age_sec']:.0f}s, skipping"
                    )
                    skipped_stale += 1
                    continue
                df = pd.DataFrame(ohlcv_data)
                if len(df) >= 30:
                    data[symbol] = df
        except Exception as e:
            logger.debug(f"Could not load OHLCV for {symbol}: {e}")
    if skipped_stale:
        logger.info(f"P2: Skipped {skipped_stale} symbols with stale OHLCV data")

    if not data:
        logger.warning("No OHLCV data available for distillation.")
        return

    # 3. 执行蒸馏
    patches = distiller.distill_batch(data)

    # 4. 发布到 Redis
    if patches:
        distiller.publish_to_redis(patches)
        logger.info(f"✅ Distillation complete: {len(patches)} causal patches generated")
    else:
        logger.info("✅ Distillation complete: No anomalies detected (market normal)")


if __name__ == "__main__":
    run_distillation()
