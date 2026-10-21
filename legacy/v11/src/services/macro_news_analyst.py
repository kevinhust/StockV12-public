"""
Macro News Analyst Service
市场级新闻分析（大方向市场基本面）

更新频率：每天 2 次（开盘前 8:00 EST + 午后 13:00 EST）
缓存 TTL：6 小时
"""

import os
import json
import logging
import requests
from typing import Dict, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import redis

logger = logging.getLogger("v8.macro_news")

EST = ZoneInfo("America/New_York")

# ── Regime polar groups (for severity classification) ────────────────────────
BULL_REGIMES = {"BULL_LONG", "BULL_SHORT"}
BEAR_REGIMES = {"BEAR_SHORT", "BEAR_LONG", "PANIC"}


def _now_est() -> datetime:
    """Return current time in US Eastern (EST/EDT)."""
    return datetime.now(EST)


class MacroNewsAnalyst:
    """市场级新闻分析服务"""

    def __init__(self, redis_client: redis.Redis):
        """
        初始化 Macro News Analyst

        Args:
            redis_client: Redis 客户端
        """
        self.redis = redis_client
        self.cache_key = "market_news:macro:latest"
        self.ttl_seconds = 21600  # 6 小时 TTL (macro news cache)
        self.regime_ttl_seconds = 18000  # 5h TTL — bridges max write gap (08:31→13:00 = 4h29m)

    # ── Alert helpers (fire-and-forget) ──────────────────────────────────────

    def _send_telegram_alert(self, message: str):
        """Fire-and-forget Telegram notification."""
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.warning(f"Telegram alert failed: {e}")

    def _is_duplicate_regime_alert(self, old_regime: str, new_regime: str) -> bool:
        """5-minute dedup to prevent alert spam on oscillating regime."""
        dedupe_key = f"alert:dedupe:regime_jump:{old_regime}_to_{new_regime}"
        try:
            was_set = self.redis.set(dedupe_key, "1", nx=True, ex=300)
            return not was_set   # True means duplicate (key already existed)
        except Exception:
            return False

    @staticmethod
    def _classify_jump_severity(old_regime: str, new_regime: str) -> tuple:
        """
        Classify regime jump severity.
        Returns (severity, emoji) where severity is LOW/MEDIUM/HIGH.
        """
        if "PANIC" in (old_regime, new_regime):
            return "HIGH", "\U0001f534"
        old_bull = old_regime in BULL_REGIMES
        new_bull = new_regime in BULL_REGIMES
        old_bear = old_regime in BEAR_REGIMES
        new_bear = new_regime in BEAR_REGIMES
        if old_bull != new_bull or old_bear != new_bear:
            return "MEDIUM", "\U0001f7e0"
        return "LOW", "\U0001f7e1"

    # ── Core methods ─────────────────────────────────────────────────────────

    def get_macro_news(self, force_refresh: bool = False) -> Dict:
        """
        获取市场级新闻分析

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            宏观新闻分析结果
        """
        # 1. 检查缓存（除非强制刷新）
        if not force_refresh:
            cached = self.redis.get(self.cache_key)
            if cached:
                data = json.loads(cached)
                logger.debug(f"✅ Using cached macro news (updated: {data.get('timestamp', 'unknown')})")
                return data

        # 2. 使用量化锚点生成宏观分析（不再是硬编码占位符）
        from services.macro_anchor import MacroAnchorService
        anchor = MacroAnchorService(redis_client=self.redis)
        result = anchor.compute_regime()

        # 3. 通过 dual-write 路径写入 Redis（market_regime:current + market_news:macro:latest）
        self.update_from_research(result)

        # 4. 归一化分数 (ADR-002)
        from core.utils.normalization import calibrate_signal
        result["normalized_sentiment"] = calibrate_signal(result["overall_sentiment_bias"], -0.5, 0.5)

        return result

    def update_from_research(self, analysis: Dict) -> bool:
        """
        从 @research Agent 更新宏观分析

        Args:
            analysis: 研究分析结果

        Returns:
            是否更新成功
        """
        try:
            # 验证必需字段
            required_fields = ["overall_sentiment_bias", "market_regime", "summary"]
            for field in required_fields:
                if field not in analysis:
                    logger.warning(f"⚠️ Missing required field: {field}")
                    return False

            # 添加时间戳
            now_est = _now_est()
            analysis["timestamp"] = now_est.isoformat()
            analysis["next_update"] = (now_est.timestamp() + self.ttl_seconds)

            # 1. 检查是否发生 Regime Jump (剧变报警)
            prev_macro = self.redis.get(self.cache_key)
            if prev_macro:
                prev = json.loads(prev_macro)
                old_regime = prev.get("market_regime")
                new_regime = analysis.get("market_regime")
                if old_regime and old_regime != new_regime:
                    logger.warning(f"⚠️ [REGIME JUMP DETECTED] {old_regime} -> {new_regime}")
                    jump_event = {
                        "time": _now_est().isoformat(),
                        "from": old_regime,
                        "to": new_regime,
                        "bias_delta": analysis.get("overall_sentiment_bias", 0) - prev.get("overall_sentiment_bias", 0)
                    }
                    self.redis.lpush("market_regime:jumps", json.dumps(jump_event))
                    self.redis.ltrim("market_regime:jumps", 0, 49)  # Keep last 50

                    # --- Regime Jump Alerting (fire-and-forget) ---
                    if not self._is_duplicate_regime_alert(old_regime, new_regime):
                        vix = analysis.get("vix_level", "N/A")
                        bias_delta = analysis.get("overall_sentiment_bias", 0) - prev.get("overall_sentiment_bias", 0)

                        severity, emoji = self._classify_jump_severity(old_regime, new_regime)

                        # Telegram alert (tiered by severity)
                        tg_msg = (
                            f"{emoji} <b>Regime Shift [{severity}]</b>: {old_regime} \u27a1\ufe0f {new_regime}\n"
                            f"VIX: {vix}\n"
                            f"Bias \u0394: {bias_delta:+.3f}"
                        )
                        if severity == "HIGH":
                            tg_msg += "\n\u26a0\ufe0f PANIC enter/exit \u2014 verify positions immediately"
                        elif severity == "MEDIUM":
                            tg_msg += "\n\u26a0\ufe0f Cross-polar shift \u2014 L1 filters adjusted"
                        else:
                            tg_msg += "\nL1 filters adjusted"
                        self._send_telegram_alert(tg_msg)

                        # Redis pub/sub event
                        try:
                            self.redis.publish("events.regime_change", json.dumps({
                                "type": "regime_change",
                                "source": "macro_news_analyst",
                                "timestamp": _now_est().isoformat(),
                                "data": {
                                    "from_regime": old_regime,
                                    "to_regime": new_regime,
                                    "severity": severity,
                                    "vix_level": vix,
                                    "bias_delta": bias_delta,
                                }
                            }))
                        except Exception as e:
                            logger.warning(f"Redis pub/sub regime event failed: {e}")

            # 2. 如果是盘前/早盘（EST 11:00 前），存入 morning_snapshot
            if now_est.hour < 11:
                self.redis.set("market_regime:morning_snapshot", json.dumps(analysis), ex=43200) # 12h

            # 3. 存入 Macro News 缓存 (Display / Logging)
            self.redis.setex(self.cache_key, self.ttl_seconds, json.dumps(analysis))
            logger.info(f"✅ Macro news updated from research: {analysis['market_regime']}")

            # 4. ✅ Dual-Write: 主动写入 Market Regime (Risk Control)
            fred_cov = analysis.get("fred_covariates", {})
            regime_payload = {
                "regime": analysis.get("market_regime", "NEUTRAL").upper(),
                "confidence": 0.9 if now_est.hour >= 13 else 0.8,
                "vix_level": analysis.get("vix_level", 20.0),
                "spy_trend": analysis.get("spy_trend", "neutral"),
                "overall_bias": analysis.get("overall_sentiment_bias", 0.0),
                "timestamp": analysis["timestamp"],
                "valid_until": (now_est.timestamp() + self.regime_ttl_seconds),
                "source": "macro_analyst",
                "data_source": analysis.get("data_source", "unknown"),
                "fred_regime_hint": fred_cov.get("slow_signal_regime_hint", "neutral"),
                "fred_bias": fred_cov.get("slow_signal_bias", 0.0),
                "trade_allowed": True
            }

            # 特殊逻辑：如果是大熊市或恐慌，禁止交易或减少暴露
            if regime_payload["regime"] in ["BEAR_LONG", "PANIC"]:
                regime_payload["trade_allowed"] = False
                regime_payload["recommended_action"] = "halt_trading"
            elif regime_payload["regime"] == "BEAR_SHORT":
                regime_payload["recommended_action"] = "reduce_exposure"
            elif "bear" in regime_payload["regime"].lower():
                regime_payload["recommended_action"] = "reduce_exposure"
            else:
                regime_payload["recommended_action"] = "normal_trading"

            # 写入 Redis (TTL 5h — bridges gap between scheduled writes)
            grid_key = "market_regime:current"
            self.redis.setex(grid_key, self.regime_ttl_seconds, json.dumps(regime_payload))
            logger.info(f"🚀 [Dual-Write] Pushed regime '{regime_payload['regime']}' to {grid_key} (Bias: {regime_payload['overall_bias']})")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to update macro news: {e}")
            return False


# 单元测试
if __name__ == "__main__":
    # 简单测试
    import redis as redis_module

    # 使用本地 Redis（仅用于测试）
    client = redis_module.Redis(host='localhost', port=6379, db=0)

    analyst = MacroNewsAnalyst(redis_client=client)

    # 获取宏观新闻
    macro = analyst.get_macro_news()
    print("Macro News:", json.dumps(macro, indent=2))
