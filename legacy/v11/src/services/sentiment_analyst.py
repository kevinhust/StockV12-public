"""
Sentiment Analyst Service
基于 Polygon News API 和 Web Search 的情绪分析
"""

import os
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from polygon import RESTClient
import redis
import sys

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.utils.normalization import normalize_sentiment

logger = logging.getLogger("v8.sentiment")


class SentimentAnalyst:
    """股票情绪分析服务"""
    
    def __init__(self, redis_client: redis.Redis, polygon_api_key: str = None):
        """
        初始化 Sentiment Analyst
        
        Args:
            redis_client: Redis 客户端
            polygon_api_key: Polygon API Key（从环境变量读取）
        """
        self.redis = redis_client
        self.api_key = polygon_api_key or os.getenv("POLYGON_API_KEY")
        
        if not self.api_key:
            logger.warning("⚠️ POLYGON_API_KEY not set, news analysis will be disabled")
            self.client = None
        else:
            self.client = RESTClient(api_key=self.api_key)
    
    def analyze_symbol(self, symbol: str, use_cache: bool = True, fallback_to_search: bool = True) -> Dict:
        """
        分析个股最新新闻和情绪
        
        Args:
            symbol: 股票代码
            use_cache: 是否使用缓存
            fallback_to_search: Polygon 覆盖不足时是否使用 web_search fallback
        
        Returns:
            情绪分析结果
        """
        # 1. 检查缓存
        cache_key = f"news:{symbol}:latest"
        if use_cache:
            cached = self.redis.get(cache_key)
            if cached:
                logger.debug(f"✅ Using cached news for {symbol}")
                return json.loads(cached)
        
        # 2. 获取新闻数据
        news_items = self._fetch_news(symbol, fallback_to_search=fallback_to_search)
        
        if not news_items:
            # 无新闻，返回中性结果
            result = {
                "sentiment_score": 0.0,
                "key_headlines": [],
                "main_catalysts": [],
                "relevance_note": "No recent news",
                "raw_items": [],
                "timestamp": datetime.utcnow().isoformat()
            }
            logger.info(f"ℹ️ No news found for {symbol}")
        else:
            # 3. 分析情绪
            result = self._analyze_news_items(news_items, symbol)
            result["raw_items"] = news_items[:5]  # 只保留前 5 条
            result["timestamp"] = datetime.utcnow().isoformat()
        
        # 4. 存入缓存（60 min TTL - 降低实时性）
        if use_cache:
            self.redis.setex(cache_key, 3600, json.dumps(result))  # 60 分钟 TTL
        
        return result
    
    def _get_news_time_window(self) -> tuple:
        """
        Return (published_utc_gte, window_label) for news fetch.
        During pre-market (before 09:30 EST): yesterday 16:00 → now
        During trading hours: last 4 hours
        """
        from zoneinfo import ZoneInfo
        now_est = datetime.now(ZoneInfo("America/New_York"))
        if now_est.hour < 9 or (now_est.hour == 9 and now_est.minute < 30):
            # Pre-market: fetch from yesterday's close (16:00 EST = 21:00 UTC)
            yesterday = now_est - timedelta(days=1)
            gte = datetime(yesterday.year, yesterday.month, yesterday.day, 21, 0)
            return gte.strftime("%Y-%m-%dT%H:%M:%SZ"), "overnight"
        else:
            # Trading hours: last 4 hours
            gte = datetime.utcnow() - timedelta(hours=4)
            return gte.strftime("%Y-%m-%dT%H:%M:%SZ"), "intraday"

    def _fetch_news(self, symbol: str, fallback_to_search: bool = True) -> List[Dict]:
        """
        获取新闻数据

        Args:
            symbol: 股票代码
            fallback_to_search: 是否使用 web_search fallback

        Returns:
            新闻列表
        """
        news_items = []

        # 1. Polygon News API（主数据源）
        if self.client:
            try:
                logger.debug(f"🔍 Fetching Polygon news for {symbol}")
                gte_str, window = self._get_news_time_window()
                news = self.client.list_ticker_news(
                    ticker=symbol,
                    published_utc_gte=gte_str,
                    limit=10,
                    order="desc"
                )
                
                news_items = [{
                    "title": n.title,
                    "summary": n.description or "",
                    "published": n.published_utc,
                    "source": "polygon"
                } for n in news]
                
                logger.info(f"✅ Fetched {len(news_items)} news items from Polygon for {symbol}")
                
            except Exception as e:
                logger.error(f"❌ Polygon news fetch failed for {symbol}: {e}")
        
        # 2. Web Search Fallback
        if fallback_to_search and len(news_items) < 3:
            logger.info(f"🔄 Using web_search fallback for {symbol}")
            # 这里需要调用 web_search（由外部提供）
            # 暂时返回空，后续集成
            pass
        
        return news_items
    
    def _analyze_news_items(self, news_items: List[Dict], symbol: str) -> Dict:
        """
        分析新闻条目
        
        Args:
            news_items: 新闻列表
            symbol: 股票代码
        
        Returns:
            分析结果
        """
        # 提取标题和摘要
        headlines = [item['title'] for item in news_items]
        summaries = [item['summary'] for item in news_items if item['summary']]
        
        # Calculate raw sentiment from headlines and summaries
        sentiment_score = self._calculate_sentiment(headlines + summaries)
        
        # 归一化到 0.0 ~ 1.0 (ADR-002: 0.0=Bear, 0.5=Neutral, 1.0=Bull)
        normalized_sentiment = normalize_sentiment(sentiment_score)
        
        # 提取催化剂
        catalysts = self._extract_catalysts(headlines)
        
        # 评估相关性
        relevance_note = self._assess_relevance(sentiment_score, catalysts)
        
        return {
            "sentiment_score": round(normalized_sentiment, 4),
            "raw_sentiment": round(sentiment_score, 4),
            "key_headlines": headlines[:5],
            "main_catalysts": catalysts,
            "relevance_note": relevance_note,
            "source": "polygon" if news_items else "none"
        }
    
    def _calculate_sentiment(self, texts: List[str]) -> float:
        """
        计算情绪分数（简单关键词匹配）
        
        Args:
            texts: 文本列表
        
        Returns:
            情绪分数 (-1.0 ~ +1.0)
        """
        positive_words = [
            "up", "gain", "surge", "rally", "beat", "strong", 
            "growth", "bullish", "upgrade", "buy", "positive", 
            "exceeds", "outperform", "record", "boost"
        ]
        negative_words = [
            "down", "drop", "fall", "plunge", "miss", "weak",
            "decline", "bearish", "downgrade", "sell", "negative",
            "below", "concern", "risk", "cut", "loss"
        ]
        
        score = 0.0
        total_words = 0
        
        for text in texts:
            text_lower = text.lower()
            words = text_lower.split()
            
            for word in words:
                total_words += 1
                if word in positive_words:
                    score += 0.1
                elif word in negative_words:
                    score -= 0.1
        
        # 归一化到 -1.0 ~ +1.0
        if total_words > 0:
            normalized_score = score / (total_words * 0.1) * 2.0
        else:
            normalized_score = 0.0
        
        return max(-1.0, min(1.0, normalized_score))
    
    def _extract_catalysts(self, headlines: List[str]) -> List[str]:
        """
        提取催化剂（财报、评级等）
        
        Args:
            headlines: 标题列表
        
        Returns:
            催化剂列表
        """
        catalysts = []
        
        for headline in headlines:
            headline_lower = headline.lower()
            
            if "earning" in headline_lower:
                catalysts.append("Earnings mentioned")
            elif "upgrade" in headline_lower:
                catalysts.append("Analyst upgrade")
            elif "downgrade" in headline_lower:
                catalysts.append("Analyst downgrade")
            elif "guidance" in headline_lower:
                catalysts.append("Guidance update")
            elif "dividend" in headline_lower:
                catalysts.append("Dividend action")
            elif "split" in headline_lower:
                catalysts.append("Stock split")
        
        return catalysts
    
    def _assess_relevance(self, sentiment_score: float, catalysts: List[str]) -> str:
        """
        评估新闻相关性
        
        Args:
            sentiment_score: 情绪分数
            catalysts: 催化剂列表
        
        Returns:
            相关性说明
        """
        if abs(sentiment_score) < 0.2 and not catalysts:
            return "Low impact news, no strong catalysts"
        elif abs(sentiment_score) > 0.5:
            direction = "positive" if sentiment_score > 0 else "negative"
            return f"Strong {direction} sentiment, may override technical signals"
        elif catalysts:
            catalyst_str = ", ".join(catalysts)
            return f"Catalysts detected: {catalyst_str}"
        else:
            return "Moderate sentiment, consider as secondary factor"


# 单元测试
if __name__ == "__main__":
    # 简单测试
    analyst = SentimentAnalyst(redis_client=None, polygon_api_key="test")
    
    # 测试情绪分析
    test_headlines = [
        "Company XYZ beats earnings expectations by 20%",
        "Analysts upgrade XYZ to buy rating",
        "Strong growth in Q3 driven by product demand"
    ]
    
    score = analyst._calculate_sentiment(test_headlines)
    print(f"Sentiment Score: {score}")
    
    # 测试催化剂提取
    catalysts = analyst._extract_catalysts(test_headlines)
    print(f"Catalysts: {catalysts}")
