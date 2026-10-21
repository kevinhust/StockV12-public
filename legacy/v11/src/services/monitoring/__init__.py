"""
StockApp Monitoring System

监控系统架构：
1. MetricsCollector - 收集各类指标
2. AlertManager - 检查阈值并发送告警
3. Storage - 存储指标到 Redis 和 TimescaleDB
"""

import time
import redis
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable
from datetime import datetime, timedelta
from enum import Enum
import json

logger = logging.getLogger("v8.monitoring")


class AlertSeverity(Enum):
    """告警严重级别"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """告警对象"""
    name: str
    severity: AlertSeverity
    message: str
    value: float
    threshold: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "timestamp": self.timestamp
        }


@dataclass
class Metric:
    """指标数据"""
    name: str
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "value": self.value,
            "labels": self.labels,
            "timestamp": self.timestamp
        }


class AlertHandler:
    """告警处理器接口"""

    def handle(self, alert: Alert):
        """处理告警"""
        raise NotImplementedError


class LogAlertHandler(AlertHandler):
    """日志告警处理器"""

    def handle(self, alert: Alert):
        """输出到日志"""
        if alert.severity == AlertSeverity.CRITICAL:
            logger.error(f"🚨 [{alert.name}] {alert.message} (value={alert.value}, threshold={alert.threshold})")
        elif alert.severity == AlertSeverity.WARNING:
            logger.warning(f"⚠️  [{alert.name}] {alert.message} (value={alert.value}, threshold={alert.threshold})")
        else:
            logger.info(f"ℹ️  [{alert.name}] {alert.message} (value={alert.value}, threshold={alert.threshold})")


class RedisAlertHandler(AlertHandler):
    """Redis 告警处理器（存储到 Redis 列表）"""

    def __init__(self, redis_client: redis.Redis):
        # 创建一个新的 Redis 客户端，不自动解码（用于存储二进制数据）
        self.redis = redis.Redis(
            host=redis_client.connection_pool.connection_kwargs.get('host', 'localhost'),
            port=redis_client.connection_pool.connection_kwargs.get('port', 6379),
            password=redis_client.connection_pool.connection_kwargs.get('password'),
            decode_responses=False  # 不自动解码
        )
        self.alert_key = "monitoring:alerts"

    def handle(self, alert: Alert):
        """存储告警到 Redis"""
        try:
            # 存储 JSON 数据（作为 bytes）
            alert_json = json.dumps(alert.to_dict())
            result = self.redis.lpush(self.alert_key, alert_json)
            # 只保留最近 1000 条告警
            self.redis.ltrim(self.alert_key, 0, 999)
            logger.debug(f"Stored alert to Redis: {alert.name} (result={result})")
        except Exception as e:
            logger.error(f"Failed to store alert to Redis: {e}")


@dataclass
class AlertRule:
    """告警规则"""
    name: str
    metric_name: str
    threshold: float
    operator: str  # ">", "<", ">=", "<=", "==", "!="
    severity: AlertSeverity
    message_template: str
    cooldown_sec: int = 300  # 5 分钟冷却期

    def evaluate(self, value: float) -> Optional[Alert]:
        """评估指标值，如果满足条件则返回告警"""
        condition = False
        if self.operator == ">":
            condition = value > self.threshold
        elif self.operator == "<":
            condition = value < self.threshold
        elif self.operator == ">=":
            condition = value >= self.threshold
        elif self.operator == "<=":
            condition = value <= self.threshold
        elif self.operator == "==":
            condition = value == self.threshold
        elif self.operator == "!=":
            condition = value != self.threshold

        if condition:
            message = self.message_template.format(
                metric=self.metric_name,
                value=value,
                threshold=self.threshold
            )
            return Alert(
                name=self.name,
                severity=self.severity,
                message=message,
                value=value,
                threshold=self.threshold
            )
        return None


class AlertManager:
    """告警管理器"""

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self.rules: List[AlertRule] = []
        self.handlers: List[AlertHandler] = []
        self.last_alert_time: Dict[str, float] = {}

        # 添加默认处理器
        self.handlers.append(LogAlertHandler())
        if redis_client:
            self.handlers.append(RedisAlertHandler(redis_client))

    def add_rule(self, rule: AlertRule):
        """添加告警规则"""
        self.rules.append(rule)

    def add_handler(self, handler: AlertHandler):
        """添加告警处理器"""
        self.handlers.append(handler)

    def evaluate(self, metric: Metric) -> List[Alert]:
        """评估指标，返回触发的告警"""
        alerts = []

        for rule in self.rules:
            # 只评估匹配的指标
            if rule.metric_name != metric.name:
                continue

            # 检查冷却期
            last_time = self.last_alert_time.get(rule.name, 0)
            if time.time() - last_time < rule.cooldown_sec:
                continue

            # 评估规则
            alert = rule.evaluate(metric.value)
            if alert:
                alerts.append(alert)
                self.last_alert_time[rule.name] = time.time()

        # 处理告警
        for alert in alerts:
            for handler in self.handlers:
                try:
                    handler.handle(alert)
                except Exception as e:
                    logger.error(f"Alert handler failed: {e}")

        return alerts


class MetricsStorage:
    """指标存储"""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.prefix = "monitoring:metrics"

    def store(self, metric: Metric):
        """存储指标到 Redis"""
        try:
            key = f"{self.prefix}:{metric.name}"
            # 使用 Redis Sorted Set 存储时间序列数据
            score = metric.timestamp
            value = json.dumps(metric.to_dict())
            self.redis.zadd(key, {value: score})
            # 只保留最近 1 小时的数据
            self.redis.zremrangebyscore(key, 0, time.time() - 3600)
        except Exception as e:
            logger.error(f"Failed to store metric: {e}")

    def query(self, metric_name: str, since_sec: int = 3600) -> List[Metric]:
        """查询指标"""
        try:
            key = f"{self.prefix}:{metric_name}"
            since = time.time() - since_sec
            values = self.redis.zrangebyscore(key, since, "+inf")
            result = []
            for v in values:
                try:
                    # 处理 bytes 和 str
                    if isinstance(v, bytes):
                        data = json.loads(v.decode('utf-8'))
                    else:
                        data = json.loads(v)
                    result.append(Metric(**data))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug(f"Failed to parse metric data: {e}")
            return result
        except Exception as e:
            logger.error(f"Failed to query metric: {e}")
            return []


class MetricsCollector:
    """指标收集器"""

    def __init__(
        self,
        redis_client: redis.Redis,
        alert_manager: Optional[AlertManager] = None
    ):
        self.redis = redis_client
        # 如果没有传入 alert_manager，创建一个新的（并传入 redis_client）
        if alert_manager is None:
            alert_manager = AlertManager(redis_client=redis_client)
        self.alert_manager = alert_manager
        self.storage = MetricsStorage(redis_client)
        self.metrics_buffer: List[Metric] = []

        # 默认告警规则
        self._setup_default_rules()

    def _setup_default_rules(self):
        """设置默认告警规则"""
        # 1. 执行时间 p99 > 15 秒
        self.alert_manager.add_rule(AlertRule(
            name="execution_p99_high",
            metric_name="shadow_runner.execution_time",
            threshold=15.0,
            operator=">",
            severity=AlertSeverity.WARNING,
            message_template="Shadow Runner 执行时间过长: {value:.2f}s > {threshold}s"
        ))

        # 2. 执行时间 p99 > 30 秒（严重）
        self.alert_manager.add_rule(AlertRule(
            name="execution_p99_critical",
            metric_name="shadow_runner.execution_time",
            threshold=30.0,
            operator=">",
            severity=AlertSeverity.CRITICAL,
            message_template="Shadow Runner 执行时间严重超时: {value:.2f}s > {threshold}s"
        ))

        # 3. Redis 内存使用 > 80%
        self.alert_manager.add_rule(AlertRule(
            name="redis_memory_high",
            metric_name="redis.memory_usage_pct",
            threshold=80.0,
            operator=">",
            severity=AlertSeverity.WARNING,
            message_template="Redis 内存使用过高: {value:.1f}% > {threshold}%"
        ))

        # 4. API 超时率 > 10%
        self.alert_manager.add_rule(AlertRule(
            name="api_timeout_high",
            metric_name="llm.api.timeout_rate",
            threshold=0.10,
            operator=">",
            severity=AlertSeverity.WARNING,
            message_template="LLM API 超时率过高: {value:.1%} > {threshold}%"
        ))

        # 5. HOLD 比例 > 90%
        self.alert_manager.add_rule(AlertRule(
            name="hold_ratio_high",
            metric_name="shadow_runner.hold_ratio",
            threshold=0.90,
            operator=">",
            severity=AlertSeverity.INFO,
            message_template="HOLD 比例过高: {value:.1%} > {threshold}%"
        ))

    def record_metric(self, metric: Metric):
        """记录指标"""
        # 存储到 Redis
        self.storage.store(metric)

        # 评估告警
        self.alert_manager.evaluate(metric)

    def record_shadow_runner_metrics(
        self,
        execution_time: float,
        decision: str,
        symbol: str = None
    ):
        """记录 Shadow Runner 指标"""
        # 执行时间
        self.record_metric(Metric(
            name="shadow_runner.execution_time",
            value=execution_time,
            labels={"symbol": symbol or "unknown", "decision": decision}
        ))

    def record_api_metrics(
        self,
        provider: str,
        model: str,
        duration_sec: float,
        success: bool,
        tokens: int = 0
    ):
        """记录 LLM API 指标"""
        # API 调用时间
        self.record_metric(Metric(
            name="llm.api.duration",
            value=duration_sec,
            labels={"provider": provider, "model": model, "success": str(success)}
        ))

        # API 超时（假设 30 秒为超时）
        timeout = duration_sec > 30.0
        if timeout:
            self.record_metric(Metric(
                name="llm.api.timeout",
                value=1.0,
                labels={"provider": provider, "model": model}
            ))

    def record_redis_metrics(self):
        """记录 Redis 指标"""
        try:
            info = self.redis.info("memory")
            used_memory = info.get("used_memory", 0)
            max_memory = info.get("maxmemory", 0) or 1024 * 1024 * 1024  # 默认 1GB

            memory_usage_pct = (used_memory / max_memory) * 100

            self.record_metric(Metric(
                name="redis.memory_usage_pct",
                value=memory_usage_pct
            ))

            self.record_metric(Metric(
                name="redis.used_memory_bytes",
                value=used_memory
            ))

        except Exception as e:
            logger.error(f"Failed to collect Redis metrics: {e}")

    def get_recent_metrics(
        self,
        metric_name: str,
        since_sec: int = 3600
    ) -> List[Metric]:
        """获取最近的指标"""
        return self.storage.query(metric_name, since_sec)

    def get_p99(
        self,
        metric_name: str,
        since_sec: int = 3600
    ) -> Optional[float]:
        """计算指标的 p99 值"""
        metrics = self.get_recent_metrics(metric_name, since_sec)
        if not metrics:
            return None

        values = [m.value for m in metrics]
        values.sort()

        # 计算 p99
        index = int(len(values) * 0.99)
        return values[index] if values else None


# 全局单例
_global_collector: Optional[MetricsCollector] = None


def get_global_collector(
    redis_client: Optional[redis.Redis] = None
) -> MetricsCollector:
    """获取全局 MetricsCollector 实例"""
    global _global_collector
    if _global_collector is None and redis_client is not None:
        _global_collector = MetricsCollector(redis_client)
    return _global_collector


def reset_global_collector():
    """重置全局 MetricsCollector（测试用）"""
    global _global_collector
    _global_collector = None
