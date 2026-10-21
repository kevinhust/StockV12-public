# 交易决策全链路文档

本文档描述从**选股**到**最终交易信号**的完整数据流与决策机制。

---

## 概览：3 层漏斗 + Agentic 决策引擎

```
选股层                    形态检测层              决策执行层
────────────────────────  ──────────────────────  ──────────────────────────
L1: IntradayScanner  →  pattern_detection      →  ShadowRunner
L2: CatalystScorer       scheduler               →  CrewOrchestrator (LangGraph)
                                                 →  RiskGatekeeper
                                                 →  ExecutionRouter
                                                     →  IB Gateway (真实下单)
```

---

## Mainline vs Archive

- **Mainline entry docs**:
  - `docs/TRADING_PIPELINE.md` (this file)
  - `docs/DEPLOYMENT.md`
  - `docs/IBKR_TROUBLESHOOTING.md`
- Detailed parameter encyclopedias and historical deep-dives are moved to `docs/_archive/`.

---

## 第一层：L1 选股 — IntradayScanner

**文件：** `src/services/selection/intraday_scanner.py`

**触发时间：** 04:00 EST（盘前）、12:00 EST（午间）

**筛选指标：**

| 指标 | 阈值 | 说明 |
|------|------|------|
| ATR% | > 5.5% | 波动率 |
| Beta | > 1.65 | 市场敏感度 |
| 成交量 | > $35M/天 | 流动性 |
| 价格 | $10 - $200 | 可交易范围 |
| 相对成交量 | > 1.8x | 当日异动 |

**评分公式：**
```
score = rel_vol_norm * 0.40 + atr_norm * 0.25 + beta_norm * 0.20 + gap_norm * 0.15
```
Gap 上跳 >10% 加成 1.15x，>5% 加成 1.08x；gap down 惩罚。

**市场状态自适应：** 根据当前市场 regime 动态调整阈值（BULL/NEUTRAL/BEAR/PANIC）

**输出到 Redis：**
- `watchlist:tier1` (ZSET: symbol → score)
- `watchlist:tier1:details` (HASH: 完整指标 JSON)
- Kafka 事件：`events.watchlist.tier1_ready`

**数据源：** Polygon.io（免费订阅，5 req/min），TimescaleDB（Beta 预计算值）

---

## 选股漏斗：参数与配置映射（v2.4 摘要）

> 目的：让后续开发者只看这份主线文档就能知道“参数从哪里来、怎么改”。更完整的参数百科已归档到 `docs/_archive/SELECTION_PIPELINE_FULL.md`。

### 配置入口

- `config/scanner_config.yaml`
  - `funnel_mode`: `long_only` / `bidirectional`
  - `filters.*`: 价格区间、成交额、ATR%、gap 黑天鹅阈值
  - `regime_adaptation.*`: 按 `market_regime:current` 动态覆盖阈值
  - `ranking.weights.*`: composite score 权重
- `config/risk_regimes.yaml`
  - `l2_catalyst_multipliers*`: L2 regime 乘数（long_only vs bidirectional）
  - `global_limits.*`: 组合级硬约束（单票、行业集中、日内交易次数、最大暴露等）

### L1（IntradayScanner）关键语义（摘要）

- **Universe 构建**：`src/services/selection/run_scanner.py`（carryover + Polygon movers + Finnhub + defaults）
- **硬过滤顺序**（first-fail skip）：bar 数、价格、ATR%、成交额、gap 黑天鹅阈值
- **评分**：相对成交量/ATR/Beta/Gap 的加权 composite score（gap 权重在盘前可能被重分配）
- **输出**：`watchlist:tier1`（ZSET）+ `watchlist:tier1:details`（HASH）+ `events.watchlist.tier1_ready`

### L2（CatalystScorer）关键语义（摘要）

- **新闻 hash cache**：`watchlist:tier2:last_news_hash:{symbol}`（24h）→ headlines 不变则跳过 LLM
- **综合分**：tech×0.45 + catalyst×0.35 + sentiment×0.20，再乘 regime multiplier
- **输出**：`watchlist:tier2` / `watchlist:tier2:details` / `watchlist:current` / `watchlist:ranked` + `events.watchlist.tier2_ready`

### L3（Forced Patrol）关键语义（摘要）

- **目的**：避免“无形态就不巡检”的漏检；10:30 强制将 tier2 top8 注入 `events.pattern`
- **实现**：`src/services/lifecycle_scheduler.py` 中的 `tier3_forced_inspection`

---

## 第二层：L2 催化剂评分 — CatalystScorer

**文件：** `src/services/selection/catalyst_scorer.py`

**触发时间：** 08:45 EST（早盘）、12:15 EST（午盘）

**流程：**

```
读取 watchlist:tier1
    → 获取相关新闻 (Polygon Sentiment)
    → 新闻 Hash 缓存（24h TTL，相同新闻跳过长耗 LLM 调用）
    → LLM 分析催化剂（DeepSeek → OpenAI → xAI fallback）
    → 应用 regime 倍率
    → 过滤无催化剂标的
    → 写入 watchlist:tier2
    → 桥接到 watchlist:current / watchlist:ranked（供下游消费者）
    → Kafka 事件：events.watchlist.tier2_ready
```

**催化剂评分：**

| 分数 | 类型 | 示例 |
|------|------|------|
| 6-10 | Strong | 财报超预期、FDA 审批、M&A、分析师上调评级 |
| 0-3 | Weak | "动量 play"、泛宏观、旧新闻 |

**综合评分：**
```
composite = tech_score * 0.45 + catalyst_norm * 0.35 + sentiment * 0.20
```
再乘以 regime 倍率（BULL 1.2x，BEAR 0.8x，PANIC 0.5x）

**输出到 Redis：**
- `watchlist:tier2` (ZSET)
- `watchlist:tier2:details` (HASH，含 catalyst_score、catalyst_type、llm_summary)
- `watchlist:current` + `watchlist:ranked`（桥接层，下游消费者专用）
- Kafka 事件：`events.watchlist.tier2_ready`

---

## 第三层：形态检测 — Pattern Detection

**文件：**
- `src/services/pattern_detector.py`（规则检测器）
- `scripts/pattern_detection_scheduler.py`（调度器）

**触发：** 每 5 分钟（市场时段内），也可被 `events.watchlist.tier2_ready` 触发

**数据源：** Polygon.io → 429 时 fallback 到 yfinance（延迟 ~15min）

### 规则检测器（Rule-based，生产路径）

检测形态：

| 形态 | 条件 |
|------|------|
| `volume_spike` | 成交量突增 |
| `oversold_bounce` | RSI < 30 |
| `overbought_reversal` | RSI > 70 |
| `bullish_breakout` | 价格突破阻力位 |
| `bearish_breakout` | 价格跌破支撑位 |
| `golden_cross` | MACD 金叉 |
| `death_cross` | MACD 死叉 |

检测到形态后 → 发布到 `events.pattern` → 触发 ShadowRunner

### YOLO 检测（Shadow Mode，实验路径）

- YOLO ML 模型从 K 线图像中检测形态
- 置信度 ≥ 0.70（`PRODUCTION_CONFIDENCE_THRESHOLD`）时 **晋升**为生产路径
- 结果存入 Redis：`yolo_detections:{symbol}:latest`
- 高置信度结果同样发布到 `events.pattern`

**为什么需要两层？**
- YOLO 是实验性模型，先在 shadow 跑积累数据
- Rule-based 是确定性规则，作为兜底路径
- YOLO 置信度达标才晋升，保证不会因为模型不稳定导致乱交易

---

## 决策引擎 — ShadowRunner + CrewOrchestrator

**文件：**
- `src/services/shadow_runner.py`（入口）
- `src/agentic_layer/graph.py`（LangGraph 编排）
- `src/agentic_layer/nodes/`（各 Agent 节点）

### ShadowRunner 入口

```
监听 events.pattern（Kafka/Redis Pub/Sub）
    ↓
收到形态事件 {symbol, pattern_type, confidence, price}
    ↓
从 Redis 读取最新价格 + tier2 催化剂上下文
    ↓
调用 CrewOrchestrator（LangGraph Agent）
    ↓
Fan-out 到所有 active modes（shadow_* + live_*）
    ↓
各 mode 独立通过 RiskGatekeeper
    ↓
通过 ExecutionRouter 执行（shadow = 记录，live = 真实下单）
```

### LangGraph 决策图

```
entry
  ↓
data_prep → 获取 regime、价格、持仓
  ↓
suggestion_consumer → 接收外部信号（如 OpenClaw Supervisor）
  ↓
[条件分支]
  ├── 有外部强制命令 → risk_validation → execution（跳过分析）
  └── 正常流程
        ↓
    market_analysis（MarketAnalyst LLM Agent）
        → 输入：symbol, regime, price, 新闻, RAG 历史, tier2 催化剂
        → 输出：AnalysisSignal (signal_type, confidence, reasoning, size_modifier)
        → RAG 安全检查：对比历史失败形态，有匹配则 Override
        ↓
    head_trader（HeadTrader Agent）
        → 整合 MarketAnalyst 信号 + FusionEngine
        → FusionEngine = RL 模型 + LLM + YOLO 信号融合
        → 输出：TradeIntention (action, quantity, confidence, reason)
        ↓
    risk_validation（HardRiskGatekeeper）
        → 财务红线检查
        → Regime 锁定检查
        → 时间窗口检查
        ↓
    token_validator（HMAC 防伪造）
        → 60s 过期 token
        ↓
    execution（真实下单）
```

### FusionEngine — 多信号融合

```
fusion_input = {
    symbol,
    rl_target: V18 RL 模型目标仓位,
    llm_side: LLM 决策方向,
    llm_confidence: LLM 置信度,
    regime: 当前市场状态,
    yolo_confidence: YOLO 置信度,
    yolo_pattern: YOLO 形态类型,
    bid, ask: 当前买卖价,
}

fusion_output = {
    side: BUY / SELL / HOLD,
    size: 仓位比例 (0.0-1.0),
    confidence: 融合后置信度,
    sources: ["rl", "llm", "yolo"],
    regime_multiplier: 基于 regime 的调整,
    latency_multiplier: 基于信号延迟的调整,
}
```

---

## 风险管理 — HardRiskGatekeeper

**文件：** `src/core/risk_controller.py`

### 财务红线

| 规则 | 条件 | 动作 |
|------|------|------|
| R-01 | 单日亏损 > 5% | 禁止新 BUY |
| R-02 | 最大回撤 > 10% | 系统全面锁定 |
| R-03 | 单笔交易 > 5% 组合 | 拒绝 |
| R-04 | 杠杆率 > 1.5x | 拒绝 |

### Regime 锁定

| Regime | 动作 |
|--------|------|
| PANIC / BEAR_LONG | 禁止新 BUY |

### 时间窗口

| 时间段 | 规则 |
|--------|------|
| 09:30 - 09:40 | 需要置信度 > 0.7 |
| 15:40 - 16:00 | 禁止新开仓，只允许平仓 |

### 仓位计算

```
base_size = 1% 组合
base_size *= regime_multiplier
base_size *= confidence_scale  # confidence / 0.7, max 2.0x
base_size *= kelly_override   # 来自 post-mortem 调整
base_size = min(base_size, 5%)  # 硬上限 5%
```

### Post-Mortem Override

每日 16:30 生成前一日交易回顾，调整风控参数（kelly_fraction、min_confidence 等）。09:15 加载到系统。

---

## 订单执行 — ExecutionRouter

**文件：** `src/core/execution/router.py`

### 模式路由

```
shadow_*  → 记录意图到 Redis，不真实下单
live_*    → 路由到真实执行器
```

### Shadow 模式

1. 生成 intent_id（UUID）
2. 存入 Redis：`shadow_intentions:{runner_id}:{mode}`
3. 发布到 `events.trade_execution`
4. 返回 "SHADOW_FILLED" 状态

### 真实执行

- **IBExecutor**：通过 IB Gateway（port 4002）下单
  - 支持 MKT、LMT、STP、TRAIL 订单类型
  - 订阅订单状态回调：Filled、Cancelled、Rejected
- **LongbridgeExecutor**：备用执行通道

---

## 日间调度 — Lifecycle Scheduler

**文件：** `src/services/lifecycle_scheduler.py`

| 时间 (ET) | 任务 | 说明 |
|-----------|------|------|
| 03:55 | macro_anchor_early | VIX 水库 regime 基线 |
| 04:00 | premarket_scanner | L1 盘前全量扫描 |
| 08:31 | macro_news_heavy | LLM 宏观研究（CPI/NFP 后） |
| 08:45 | catalyst_scorer_am | L2 早盘催化剂评分 |
| 08:55 | self_check | 全链路健康检查 |
| 09:15 | morning_risk_load | 加载 post-mortem override |
| 10:30 | tier3_forced_inspection | 强制巡检 top tier2 标的 |
| 12:00 | midday_scanner | L1 午间刷新 |
| 12:15 | catalyst_scorer_pm | L2 午盘催化剂评分 |
| 13:00 | macro_research_pm | 下午宏观刷新 |
| 14:00 | causal_distiller | 因果推理蒸馏 |
| 15:45 | eod_closing | 收盘前处理 |
| 15:58 | eod_liquidation | 平掉所有仓位 |
| 16:30 | post_mortem | 日终复盘分析 |
| 20:00 | rag_integrity | RAG 知识完整性检查 |

---

## 事件总线 — Kafka Topics

| Topic | 用途 | 生产者 | 消费者 |
|-------|------|--------|--------|
| `events.watchlist.tier1_ready` | L1 扫描完成 | IntradayScanner | CatalystScorer |
| `events.watchlist.tier2_ready` | L2 评分完成 | CatalystScorer | ShadowRunner |
| `events.pattern` | 形态检测事件 | PatternDetector, YOLO | ShadowRunner |
| `events.trade_execution` | 订单事件 | IBExecutor, Router | TradeRecorder |
| `events.shadow_decision` | Shadow 决策 | ShadowRunner | 回测系统 |
| `events.yolo_shadow` | YOLO 结果 | PatternScheduler | 观察 |
| `events.regime_change` | Regime 变化 | MacroNewsAnalyst | 所有 Agent |

---

## Redis 数据结构

| Key | 类型 | 内容 |
|-----|------|------|
| `watchlist:tier1` | ZSET | L1 候选标的 + 评分 |
| `watchlist:tier1:details` | HASH | L1 完整指标 |
| `watchlist:tier2` | ZSET | L2 候选标的 + 催化剂评分 |
| `watchlist:tier2:details` | HASH | L2 催化剂详情 |
| `watchlist:current` | STRING | 当前 watchlist（JSON list） |
| `watchlist:ranked` | STRING | 带评分的排名列表 |
| `price:{symbol}:current` | STRING | IB Gateway 实时价格 |
| `market_regime:current` | STRING | 当前 regime 状态 |
| `shadow_intentions:*` | LIST | Shadow 模式记录的意图 |
| `yolo_detections:{symbol}:latest` | STRING | YOLO 最新检测结果 |
| `portfolio:snapshot:{mode}` | STRING | 各模式的持仓快照 |
| `risk:blacklist:*` | STRING/HASH | 动态黑名单 |

---

## 数据源

| 数据源 | 使用方 | 用途 |
|--------|--------|------|
| Polygon.io | IntradayScanner, CatalystScorer, PatternScheduler | 价格、新闻、历史 K 线 |
| yfinance | PatternScheduler | Polygon 429 fallback |
| TimescaleDB | IntradayScanner | Beta 预计算值 |
| IB Gateway | IBKRDataService, Streamer | 实时行情、订单执行 |
| Tavily | MarketAnalyst | 实时新闻聚合 |
| KnowledgeStore (RAG) | MarketAnalyst | 历史形态、失败案例 |
| Redis | 所有服务 | 缓存、状态、协调 |
| Kafka/Redpanda | 所有服务 | 事件总线 |

---

## 人工干预点

1. **09:15 morning_risk_load**：复盘 override 加载后，人工确认生效
2. **16:30 post_mortem**：每日交易复盘，生成风控参数调整
3. **Telegram 告警**：健康检查失败、regime 切换时触发
4. **动态黑名单**：通过 Redis Pub/Sub 实时更新（`risk:blacklist:updates`）

---

## 关键文件索引

| 组件 | 文件路径 |
|------|----------|
| L1 选股 | `src/services/selection/intraday_scanner.py` |
| L2 催化剂 | `src/services/selection/catalyst_scorer.py` |
| 形态检测器 | `src/services/pattern_detector.py` |
| 形态调度器 | `scripts/pattern_detection_scheduler.py` |
| 决策编排图 | `src/agentic_layer/graph.py` |
| 市场分析师 | `src/agentic_layer/nodes/market_analyst.py` |
| 交易决策者 | `src/agentic_layer/nodes/head_trader.py` |
| 风控门卫 | `src/core/risk_controller.py` |
| 执行路由 | `src/core/execution/router.py` |
| IB 执行器 | `src/core/execution/ib_executor.py` |
| Shadow Runner | `src/services/shadow_runner.py` |
| 生命周期调度 | `src/services/lifecycle_scheduler.py` |
| 实时行情推送 | `src/services/ibkr_realtime_streamer.py` |
| Agent 状态定义 | `src/agentic_layer/state.py` |
| Agent 模型定义 | `src/agentic_layer/agent_models.py` |
| 事件定义 | `src/core/events.py` |
| 数据服务 | `src/infrastructure/data_service.py` |
