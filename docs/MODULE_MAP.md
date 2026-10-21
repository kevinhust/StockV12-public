# StockApp — 模块地图（Part 1: 拆分方案）

> 创建日期: 2026-07-09 · 最近更新: 2026-07-26（architecture decoupling Phase 0–2）
> 目的: 为后续 Part 2 的逐模块 review 提供索引。
> Layer A 业务模块对齐 `docs/SYSTEM_OVERVIEW.md §4`。
> Layer B 实施模块对齐实际目录布局（post-migration 2026-07-09；Phase 1 新增 adapters/app）。

## 2026-07-09 → 2026-07-26 更新摘要

- **M1 core** — 新增 `core/nyse_calendar.py`（Sprint I）+ `core/_v11_compat.py`（Sprint J6）；`core/limits.py` 新增 `StageFallbacks` dataclass（Sprint J2）
- **M6 pipeline** — 新增 `pipeline/types.py`，`EntryContext` / `EntryResult` 从 services/entry_orchestrator.py 迁出（Sprint J5，打破 services↔pipeline 循环依赖）
- **M7 services** — 新增 `services/_notifier_compat.py`（Sprint J7，canonical notifier shim 位置）；`services/notification_service.py` 标记为 **DEPRECATED**（Sprint K3，import 时发 `DeprecationWarning`）
- **M5 bridge** — `bridge/live_guard.py` 新增 R-03_SINGLE_CAP / R-04_EXPOSURE_CAP（Sprint I）
- **M9 audit** — `audit/audit_ledger.py` 在每次写入后增加 `fsync`（Sprint H2）
- **M7 services** — `services/position_manager.py` 新增 `lowest_close_since_entry` MAE 跟踪 + `_close_position` 改用 redis pipeline() 原子事务（Sprint I, Sprint J1）
- **M7 services** — `services/daily_review.py` `apply_immediately` 默认值改为 `False`（Sprint J3）
- **M16 adapters** — Phase 1：`adapters/clock`、`adapters/marketdata`（Fake/Composite）、`adapters/legacy_v11`（Chronos cage）
- **M17 app** — Phase 1：composition root `app/wiring.build_runtime` + thin demo `scripts/demo_wiring_runtime.py`

---

## Layer A — 业务模块（10 个，对齐 SYSTEM_OVERVIEW §4）

| # | 模块 | 职责 | 主要代码位置 |
|---|------|------|--------------|
| B1 | **SwingScanner** | 日线候选 → `watchlist:tier1` | `services/swing_scanner.py` |
| B2 | **CatalystScorer** | tier2 排序 + catalyst 元数据 | （外部 / 暂未实装 / `scripts/run_catalyst_scorer.sh`） |
| B3 | **EntryOrchestrator** | 单 symbol 7 阶段入口管线 | `services/entry_orchestrator.py` + `pipeline/types.py` (EntryContext/EntryResult) |
| B4 | **LLMAnalyst** | 结构化 `AnalystOutput` | `pipeline/analyst.py` |
| B5 | **RiskGatekeeper** | 硬门控 + 风险 sizing | `bridge/gatekeeper.py` + V1 archive `src/core/risk_controller.py` (通过 `core/_v11_compat.py` 桥接) |
| B6 | **BracketOrderService** | 委托下单 + SL/trail | `core/execution/bracket_order.py` + `core/paper_broker.py` |
| B7 | **PositionManager** | 状态机 + trail ratchet + min-hold + MFE/MAE | `services/position_manager.py` + `bridge/live_guard.py` (R-03/R-04) |
| B8 | **DailyReview** | HOLD/TRIM/EXIT 决策 | `services/daily_review.py` |
| B9 | **TradeJournal** | append-only 决策审计 | `pipeline/journal.py` + `audit/audit_ledger.py` |
| B10 | **Scheduler** | cron + 失败钩子（编排） | 外部 V1 `archive/.../lifecycle_scheduler.py` |

---

## Layer B — 实施模块（18 个，按依赖从底向上）

### B-tier 0 — 基础设施 / 兼容垫片

| # | 模块 | 路径 | 行数 | 职责 | 关键文件 |
|---|------|------|------|------|----------|
| **M0** | `_v11_compat` | `src/stockapp_v12/core/_v11_compat.py` | ~85 | V1 archive 路径注入（多候选回退） | `_v11_compat.py` |
| **M1** | `core` | `src/stockapp_v12/core/` | ~? | limits / sizing / precision / paths / bracket_order / paper_broker / nyse_calendar / v11_compat | `limits.py` (V12RiskLimits + StageFallbacks), `sizing.py`, `precision.py`, `paths.py`, `execution/bracket_order.py`, `paper_broker.py`, `nyse_calendar.py` (Sprint I), `_v11_compat.py` (Sprint J6) |
| **M2** | `contracts` | `src/stockapp_v12/contracts/` | ~? | Protocol/接口定义（chronos_backend, market_data, notifier, clock, broker） | `chronos_backend.py`, `market_data_client.py`, `notifier.py`, `clock.py`, `broker.py` (Phase 0/1 re-export of `core.execution.bracket_order.BrokerAdapter` — short-term `contracts→core` edge; Phase 2 may invert) |
| **M3** | `schemas` | `src/stockapp_v12/schemas/` | ~? | 纯数据模型（forecast + Phase-2 signal/yolo DTO） | `forecast.py`, `signal.py` (SignalRuleConfig/Context/Result + SR-*), `yolo.py` (YoloFilterDecision) |
| **M4** | `config` | `src/stockapp_v12/config/` | ~? | YAML 加载 + 校验 | `loader.py`, `schema.py` |
| **M15** | `infra` | repo root | — | 测试/CI/env 配置 | `pyproject.toml`, `conftest.py`, `.gitignore`, `.github/workflows/` |

### B-tier 0.5 — 适配器 / 组合根（Phase 1 hexagonal edge）

| # | 模块 | 路径 | 行数 | 职责 | 关键文件 |
|---|------|------|------|------|----------|
| **M16** | `adapters` | `src/stockapp_v12/adapters/` | ~? | 端口实现 + 遗留 cage：clock、marketdata、legacy_v11 Chronos | `clock.py` (`SystemClock`/`FakeClock`), `marketdata/fake.py`, `marketdata/composite.py`, `legacy_v11/chronos.py` (`build_legacy_chronos_backend`) |
| **M17** | `app` | `src/stockapp_v12/app/` | ~? | **唯一** composition root：`Settings` / `Runtime` / `build_runtime` | `wiring.py`；消费 demo：`scripts/demo_wiring_runtime.py` |

### B-tier 1 — 桥接层

| # | 模块 | 路径 | 行数 | 职责 | 关键文件 |
|---|------|------|------|------|----------|
| **M5** | `bridge` | `src/stockapp_v12/bridge/` | ~? | V1/V2 兼容 + 风险/熔断/下单护栏 | `gatekeeper.py`, `killswitch.py`, `live_guard.py` |

### B-tier 2 — 业务编排层

| # | 模块 | 路径 | 行数 | 职责 | 关键文件 |
|---|------|------|------|------|----------|
| **M6** | `pipeline` | `src/stockapp_v12/pipeline/` | ~? | 入口管线 / LLM analyst / journal / stages / **共享 types** | `analyst.py`, `entry_guard.py`, `journal.py`, `stages.py`, `types.py` (EntryContext/EntryResult). **Phase 2:** 禁止 import `services`（DTO 只依赖 `schemas`） |
| **M7** | `services` | `src/stockapp_v12/services/` | ~? | 核心 service 引擎 + shim；DTO re-export from schemas | `entry_orchestrator.py`, `swing_scanner.py`, `yolo_filter.py` (re-export `schemas.yolo.YoloFilterDecision`), `signal_rule_engine.py` (engine; DTO re-export `schemas.signal`), `position_manager.py`, `daily_review.py`, `forecast_service.py`, `feature_loader.py`, `longbridge_screener.py`, `_notifier_compat.py`, `notification_service.py` (**DEPRECATED**), `veto_alarm.py`, `ensemble_chronos_backend.py` |
| **M8** | `eval` | `src/stockapp_v12/eval/` | ~? | backtest / predict / quantiles / metrics / ADR-008 gate | `backtest_pnl.py`, `predict.py`, `quantiles.py`, `metrics.py`, `adr008_gate.py`, `constants.py`, `test_data.py`, `__init__.py` |

### B-tier 3 — 横切关注点

| # | 模块 | 路径 | 行数 | 职责 | 关键文件 |
|---|------|------|------|------|----------|
| **M9** | `audit` | `src/stockapp_v12/audit/` | ~? | NDJSON 决策审计 + 持久化 | `audit_ledger.py` |
| **M10** | `notifiers` | `src/stockapp_v12/notifiers/` | ~? | 多通道通知（log / redis pubsub / webhook）— 实现侧；经 `app.wiring` 或 `make_notifier_from_env` 注入 | `log_notifier.py`, `redis_pubsub_notifier.py`, `webhook_notifier.py` |
| **M11** | `scripts` | `src/stockapp_v12/scripts/` | ~? | 库内置运维工具（health preflight） | `preflight.py` |

### B-tier 4 — 顶层运维 & 验证

| # | 模块 | 路径 | 行数 | 职责 | 关键文件 |
|---|------|------|------|------|----------|
| **M12** | `scripts_ops` | `scripts/` | 27 .py + 12 .sh | 项目根级运维脚本（backtest、train、shadow、preflight shell、cron 安装）；含 composition-root smoke `demo_wiring_runtime.py` | `demo_wiring_runtime.py`, `run_experiment.py`, `train_chronos_*.py`, `shadow_*.py`, `*_cron.sh`, `nuc_post_migration_refresh.sh` 等 |
| **M13** | `tests` | `tests/` | 51 文件 | 测试套件 | `test_*.py`, `conftest.py` |
| **M14** | `docs` | `docs/` | — | 规范文档（SYSTEM_OVERVIEW, RISK_MANAGEMENT, POSITION_LIFECYCLE 等） | 见 `docs/README.md` 列表 |

---

## 依赖图（简化）

```
            ┌──────────── M14 docs ────────────┐
            │                                  │
M15 infra   │   M2 contracts  M3 schemas       │
   ▲        │       ▲            ▲             │
   │        │       │            │             │
   │   ┌────┤   M4 config       │             │
   │   │    │       │            │             │
   │   │    │   M1 core ────► M0 _v11_compat  │
   │   │    │       │                          │
   │   │    │       ▼                          │
   │   │    │   M5 bridge (V1↔V2)             │
   │   │    │       │                          │
   │   │    │       ▼                          │
   │   │    │   M6 pipeline                    │
   │   │    │       │                          │
   │   │    │       ▼                          │
   │   │    │   M7 services                    │
   │   │    │       │                          │
   │   │    │       ▼                          │
   │   │    │   M8 eval                        │
   │   │    │                                  │
   │   │    │   ┌─── M9 audit (audit_ledger)   │
   │   │    │   │                              │
   │   │    │   ├─── M10 notifiers             │
   │   │    │   │                              │
   │   │    │   └─── M11 scripts (preflight)   │
   │   │    │                                  │
   │   │    │   M16 adapters ──► M2 ports      │
   │   │    │       ▲                          │
   │   │    │       │ (wires only)             │
   │   │    │   M17 app (composition root)     │
   │   │    │                                  │
   │   │    └──► M13 tests (consumes all)      │
   │   │                                       │
   └──►─────── M12 scripts_ops (CLI entry) ───┘
```

依赖规则（应严格执行）：
- ✅ 下层可被上层依赖；上层**禁止**反向依赖下层
- ✅ `M0 _v11_compat` 只在跨层兼容处被使用，不进入业务热路径
- ✅ `M16 adapters` 实现 `M2 contracts`；可临时调用 archive（仅 `legacy_v11` 与既有 seam）
- ✅ `M17 app` 是**唯一** composition root，将 adapters 接到 Runtime；业务层不 import `app`
- ⚠️ `M5 bridge` 是 V1↔V2 接口，需要严格接口隔离
- ✅ `M6 pipeline` Phase 2 起禁止 import `services`（边界检查器 `pipeline_no_services_adapters_app`）
- ✅ `M3 schemas.signal` / `schemas.yolo` 是 SR/YOLO DTO 的 canonical home；`services.*` 仅 re-export
- ⚠️ `M7 services._notifier_compat` 是 notifier 的 canonical shim；`services.notification_service` 已被标记为 DEPRECATED
- ⚠️ Phase 1 后：`SwingScanner` 默认构造仍可能经 Longbridge seam 触达 archive；**测试/注入路径**必须用 `MarketDataClient`（Fake/Composite）

---

## Part 2 — Review 顺序建议

按依赖底向上逐模块 review，每个模块覆盖：

1. **职责清晰度**：模块是否只做一件事
2. **接口设计**：Protocol/类型注解/错误类型
3. **错误处理**：异常路径、fallback、日志
4. **可测试性**：fixture、mock 难度、覆盖率
5. **安全**：输入校验、密钥处理、路径穿越
6. **文档**：docstring、与 `docs/SYSTEM_OVERVIEW.md` 的对齐
7. **重复代码**：与 M0/M5 等垫片是否在重复造轮子

**建议顺序**（自下而上）：
```
M0 → M1 → M15 → M2 → M3 → M4 → M16 → M17 → M5 → M6 → M7 → M8 → M9 → M10 → M11 → M12 → M13 → M14
```

---

## 待用户确认

1. **粒度**：上面 18 个实施模块（含 M0/M15/M16/M17）够细吗？要不要更粗（比如按 Layer A 的 10 个业务模块直接 review）？
2. **review 深度**：每个模块需要展开到多细？是「文件级」还是「函数级」？
3. **范围排除**：`archive/`、`docs/` 里的内容是否纳入 review（建议 archive 跳过，因为它本身是被替代的旧实现）？
4. **交付方式**：希望每 review 一个模块就出报告，还是全部 review 完一次性汇总？
