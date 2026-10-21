"""
LLM Reasoning Scorer V2.1
Evaluates the information value of LLM reasoning.
Enhanced by Gemini Deep Review Round 2.

Features:
- Macro event detection (Fed, Earnings, etc.)
- Time-decay scoring (freshness check)
- Asymmetric information boost
"""

import time
import logging
from loguru import logger
from typing import Optional, Tuple


class LLMReasoningScorer:
    """
    LLM 推理评分器

    评估 LLM 推理的信息价值，当 LLM 基于宏观事件（财报、联储决策）
    时给予信息不对称优势加成。
    """

    # 宏观事件关键词 (信息不对称优势)
    MACRO_KEYWORDS = [
        "earnings", "eps", "revenue", "guidance",  # 财报
        "fed", "rate", "fomc", "powell",  # 联储
        "cpi", "inflation", "jobs", "unemployment",  # 宏观经济
        "ceo", "merger", "acquisition", "lawsuit", "fda",  # 公司事件
    ]

    # 时效性关键词 (事件越新，权重越高)
    TIME_KEYWORDS = [
        "breaking", "just in", "announced", "today", "yesterday",
    ]

    def score(
        self,
        llm_reasoning: str,
        event_timestamp: Optional[float] = None
    ) -> Tuple[float, float]:
        """
        评估 LLM 推理的信息价值

        Args:
            llm_reasoning: LLM 给出的推理文本
            event_timestamp: 事件发生时间 (用于时效性校验)

        Returns:
            tuple: (score, boost_factor)
            - score: 基础分数 (0.0 ~ 1.0)
            - boost_factor: 仓位乘数加成 (1.0 ~ 1.68)
        """
        score = 0.0
        boost_factor = 1.0

        if not llm_reasoning:
            return score, boost_factor

        reasoning_lower = llm_reasoning.lower()

        # 1. 宏观事件检测
        for keyword in self.MACRO_KEYWORDS:
            if keyword in reasoning_lower:
                score += 0.15  # 每个宏观关键词 +0.15
                boost_factor = 1.4  # 宏观事件给 40% 加成

                # 检查时效性
                if event_timestamp:
                    event_age_hours = (time.time() - event_timestamp) / 3600

                    if event_age_hours < 24:
                        # 新鲜事件 (<24h) 额外加成
                        boost_factor *= 1.2
                        logger.info(f"📰 Fresh macro event detected (age: {event_age_hours:.1f}h)")
                    elif event_age_hours > 72:
                        # 过期事件 (>72h) 降权
                        boost_factor *= 0.7
                        score *= 0.5
                        logger.warning(f"🕐 Stale macro event, reducing weight (age: {event_age_hours:.1f}h)")

        # 2. 时效性关键词检测
        for keyword in self.TIME_KEYWORDS:
            if keyword in reasoning_lower:
                score += 0.1  # 时效性关键词加分
                boost_factor = min(boost_factor, 1.2)  # 时效性上限 1.2

        return round(score, 2), round(boost_factor, 2)

    def get_macro_context(self, llm_reasoning: str) -> dict:
        """
        提取 LLM 推理中的宏观上下文

        Returns:
            dict: {
                "has_earnings": bool,
                "has_fed": bool,
                "has_ma": bool,
                "keywords_found": list[str]
            }
        """
        reasoning_lower = llm_reasoning.lower() if llm_reasoning else ""

        context = {
            "has_earnings": any(k in reasoning_lower for k in ["earnings", "eps", "revenue", "guidance"]),
            "has_fed": any(k in reasoning_lower for k in ["fed", "rate", "fomc", "powell"]),
            "has_ma": any(k in reasoning_lower for k in ["merger", "acquisition", "ceo"]),
            "keywords_found": [k for k in self.MACRO_KEYWORDS if k in reasoning_lower]
        }

        return context


# Singleton instance
llm_reasoning_scorer = LLMReasoningScorer()
