---
title: Gate Decision Architecture — Trading-first (Accepted)
project: StockApp-V12
date: 2026-07-26
status: accepted
type: architecture
tags: [gate, risk, promote, halt, shadow, trading, architecture]
updated: 2026-07-26
related:
  - "[[../04-decisions/ADR-010-post30day-training-and-observation-plan]]"
  - "[[../04-decisions/ADR-008-round2-closure-walk-forward-gate]]"
  - "[[../04-decisions/Round-4-Council-Halt-Rule-and-PnL-Moratorium]]"
  - "[[../09-progress/overall-progress]]"
repo: docs/GATE_DECISION_ARCHITECTURE.md
accepted: 2026-07-26
---

# Gate Decision Architecture — 交易优先（Accepted）

> **立场**：先按**整条交易系统**设计 gate，再映射到现有 ADR/脚本。  
> **不预设推翻 ADR-010**；本文件回答「该有几道门、各自拦什么、什么永远不拦交易」。  
> **平衡**：投资安全（错 promote / 失控下单） vs 效率（少口径、少会议、少假 FAIL）。
>
> **Status:** ✅ **Accepted** (2026-07-26)
> **Vault SSOT:** `Projects/StockApp-V12/03-architecture/gate-decision-architecture.md`
> **Related:** [ADR-010](ADR-010-Post30Day-Training-and-Observation-Plan.md) · Round-4 Halt · ADR-008 walk-forward health

---

## 1. 问题诊断（为什么乱）

今天「gate」一词混用了 5 类完全不同的决策：

| 混用对象 | 实际是 | 频率 | 失败代价 |
|---|---|---|---|
| R-01..R-10 / EntryGuard | **资本与仓位硬约束** | 每笔 | 真金白银 |
| HaltGate 3d/40% | **当日信号熔断** | 每日 | 少交易 / 错过机会 |
| ADR-008 SUMMARY | **模型长期是否「不离谱」** | 日更汇总 | 观察名单资格 |
| Stage-1 hit≥0.53 / 7-gate | **研究/历史 promote 清单** | 偶发 | 会议噪音 |
| ADR-010 D3 pack | **换决策路径** | 月级 | 换错主模型 |

结果：

- **安全被稀释**：真正挡钱的门和诊断红旗挤在同一张表里。  
- **效率被拖死**：同一模型可同时 PASS/FAIL 三套口径，无法一句话决策。  
- **激励错位**：绝对 hit_rate 门槛让「基线自己也不过」的系统永远像失败产品。

---

## 2. 设计原则

1. **一门一责** — 每道门只回答一个 yes/no 问题。  
2. **越靠近钱越硬** — 资本门不可关；研究门不可单独否决交易。  
3. **相对优于绝对** — 换模型比的是「是否优于当前决策路径」，不是「是否 ≥ 理论 53%」。  
4. **样本不足 = 不决策**，不是 FAIL — `INSUFFICIENT` 与 `FAIL` 分开。  
5. **PnL 默认展示、默认不否决**（延续 Round-4 教训：selection bias + 稀少成交翻转排名）。  
6. **Human 只出现在换路径**，不出现在每日 halt。  
7. **Observation 免费，Decision 昂贵** — 观察集可松；决策路径变更极严。

---

## 3. 目标架构：三道门 + 一层诊断

```text
                    ┌─────────────────────────────────────┐
  Market data       │  L0  CAPITAL  (永远开启)              │
  → Forecast        │  R-01..R-10 · sizing · entry window   │
  → Rules           │  失败 → 禁止开仓 / 强制风控动作         │
                    └──────────────┬──────────────────────┘
                                   │ only if L0 allows
                    ┌──────────────▼──────────────────────┐
  Decision-path     │  L1  SESSION  (每日, 自动)            │
  forecasts only    │  HaltGate + ensemble spread/fallback  │
                    │  失败 → 今日不交易(paper/live约定)     │
                    └──────────────┬──────────────────────┘
                                   │ fills / shadow labels
                    ┌──────────────▼──────────────────────┐
  Challenger vs     │  L2  PATH CHANGE  (稀有, 人审)         │
  current champion  │  相对不差 + 样本够 + halt不更差 + 人批  │
                    │  失败 → 留在 observation，不换路径     │
                    └─────────────────────────────────────┘

  旁路（永不单独否决 L0/L1/L2）:
  ┌──────────────────────────────────────────────────────┐
  │  D  DIAGNOSTIC  H2H hit/MASE · multi-window rank ·   │
  │     Stage-1 0.53 · champion 7-gate (research)        │
  │  用途: 红旗、排序、复盘；输出 WARN / INFO 非 PASS/FAIL │
  └──────────────────────────────────────────────────────┘
```

### 3.1 L0 — CAPITAL（投资安全底盘）

| 问题 | 今天是否允许承担新风险？ |
|---|---|
| **输入** | 持仓、日内亏损、symbol 冷却、entry window、regime yaml |
| **规则** | 现有 R-01..R-10 + EntryGuard + SR 规则；**不改哲学** |
| **产出** | ALLOW / BLOCK（带 reason code） |
| **owner** | Gatekeeper / Entry 路径 |
| **效率** | 已实现；勿再叠模型 hit_rate |

**安全**：最高优先级。  
**效率**：不参与模型迭代讨论。

### 3.2 L1 — SESSION（当日效率 + 尾部风险）

| 问题 | 今天决策路径的信号是否「最近过烂」到该停手？ |
|---|---|
| **作用对象** | **仅 decision path**（当前 ensemble A+B 及其 halt 日志） |
| **规则（建议单一主变体）** | 沿用 HaltGate：滚动 3 日可交易准确率 &lt; 40% → halt（可保留 persistent 作旁路实验，但不进主路径） |
| **辅规则** | ensemble spread 过大 → fallback / 降杠杆（已有），不另开 promote 语义 |
| **产出** | TRADE / HALT_TODAY |
| **不做** | 不触发 retrain；不改 observation 名单；不写「模型 FAIL」 |

**安全**：挡住连亏 regime 的机械下单。  
**效率**：一天一次、自动、无 human。

观察模型也可算 Halt **仅供统计**，但 **不得** 因观察模型 halt 而停 decision path。

### 3.3 L2 — PATH CHANGE（换谁说了算）

| 问题 | 是否把挑战者写入 **唯一决策路径**？ |
|---|---|
| **作用对象** | challenger（如 `phase2_wf_*`、seat ensemble）vs **current decision path baseline** |
| **默认答案** | NO（安全默认） |
| **技术条件（全满足才进入人审）** | 见下表 |
| **产出** | `INSUFFICIENT` / `REJECT` / `READY_FOR_HUMAN` → 仅 human 可 → `PROMOTED` |

| # | 条件 | 安全意图 | 效率意图 |
|---|---|---|---|
| C1 | **样本** | 同窗口 actionable trades ≥ floor（建议 20d 内 ≥15，或 ≥20 个交易日有日志） | 避免一周噪声 |
| C2 | **相对质量** | 20d：hit_rate **不低于** baseline；MASE **不差于** baseline×1.05（或并列） | 不要求绝对 0.53 |
| C3 | **稳定性** | 同期 L1 halt 率 **不显著差于** baseline（如 halt 天数 ≤ baseline+1） | 防「赚时更野」 |
| C4 | **路径隔离已验证** | 观察目录独立；未污染 ensemble 历史 | 可回滚 |
| C5 | **Human** | 书面确认 + 回滚计划（保留旧 A+B 权重或开关） | 月级一次即可 |

**明确不进 L2 硬条件**：

- 绝对 hit_rate ≥ 0.53  
- cum PnL 排名第一  
- 单日大胜  
- IS MASE  
- 7-gate 全绿（见 §4 降级）

**PnL**：L2 报告里展示 20d / full PnL，**默认不否决**；若 human 要加硬条件，必须单独签字（例外通道），避免静默恢复 Round-4 已否决的 bias。

### 3.4 D — DIAGNOSTIC（旁路）

| 工具 | 新语义 |
|---|---|
| Offline H2H（10d/20d） | `WARN` 若 hit≪0.5 或远差于 baseline；**不**产出 promote FAIL |
| E-EVAL multi-window | 排序与 n_trades 过滤；标签 `rank` 非 `gate` |
| Stage-1 0.53 | 历史概念；改称 **red-flag line**，仅 INFO |
| champion 7-gate | **research-only / deprecated for live path**（与 L1/L2 冲突处以 L1/L2 为准） |
| ADR-008 SUMMARY PASS | 解释为 **「长期不离谱」资格**，属 L2 的 soft prior，不是独立第四门 |

映射建议：ADR-008 的 MASE/win_rate/n_dates 检查 → 收进 **L2 的「资格预检」** 或周报，不再与 L1 混称 promotion_gates。

---

## 4. 与现状的映射（不推翻，只收口）

| 现状 | 新架构位置 |
|---|---|
| R-01..R-10 / Entry | **L0** |
| HaltGate paper cron | **L1** |
| Ensemble spread / fallback | **L1 辅** |
| ADR-008 walk-forward in SUMMARY | **L2 预检 / 周报**（改名避免 “promotion” 误导） |
| ADR-010 D3 五条 | **L2 主体**（C1–C5 对齐） |
| ADR-010 观察 vs 决策分离 | **保留**（效率核心：观察免费） |
| Stage-1 0.53 | **D** |
| champion 7-gate | **D**（或删硬 PnL 后与 L2 合并，二选一） |
| E-EVAL / compare_shadow | **D → 喂 L2 报告** |
| Human approve | **仅 L2** |

ADR-010 的训练禁令（v3 长 FT、E07 模板等）属于 **训练政策**，不是交易 gate；继续放在 ADR，不进 L0–L2。

---

## 5. 决策流程（运营视角）

### 每日（自动，&lt;1 分钟思考）

1. 数据新鲜度（E-DATA soft）→ 坏数据则 **停 shadow/决策**（偏 L0 数据完整）  
2. 跑 decision path 预测  
3. **L1** Halt？ → 是则今日无新仓  
4. **L0** 逐笔风控  
5. 观察模型照跑，只写 JSON，**不参与 3–4**

### 每周（15 分钟）

1. E-EVAL / compare 出表（D）  
2. 看 L1 halt 频率、decision path 20d hit  
3. **不**开 promote 会，除非 L2 已 `READY_FOR_HUMAN`

### 月级 / 漂移触发（L2）

1. 挑战者观察满样本 → 跑 **一条** `evaluate_path_change(challenger, baseline)`  
2. `INSUFFICIENT` → 继续等  
3. `REJECT` → 记 daily-log，不换  
4. `READY_FOR_HUMAN` → 人批 → 改 ensemble 配置 → 保留回滚

---

## 6. 效率 vs 安全 — 显式权衡

| 选择 | 偏安全 | 偏效率 | 本架构取舍 |
|---|---|---|---|
| 绝对 hit 0.53 | 严 | 几乎永不 promote | **丢出 L2**，进 D |
| 相对 baseline 20d | 中 | 可迭代 | **L2 核心** |
| PnL 硬门 | 假安全（bias） | 乱跳 | **展示 only** |
| n_trades 150（7-gate） | 过严 | 卡死 | **降到 15–30 与窗口匹配** |
| 多观察模型 | 安全（多样本） | 算力 | **保持观察集；决策唯一** |
| 自动 promote | — | 快 | **禁止** |
| Halt 自动 | 安全 | 偶发少交易 | **L1 保留** |
| 人审每日 | 过慢 | — | **禁止；仅 L2** |

**一句话**：  
- **安全**靠 L0 永远在 + L1 自动熔断 + L2 默认 NO + 禁止自动 promote。  
- **效率**靠一门一责、观察免费、诊断不否决、样本不足不决策、人只在换路径出现。

---

## 7. 命名规范（减少沟通税）

| 禁用混称 | 改称 |
|---|---|
| “没过 gate” | 指明 **L0/L1/L2/D** |
| promotion_gates in daily SUMMARY | `longitudinal_health` 或 `eligibility_check` |
| Stage-1 gate | `h2h_red_flag` |
| 7-gate pass | `research_scorecard` |

---

## 8. 落地顺序（实现 backlog）

1. **文档收口**：文档已 Accepted；README / overall-progress 链 L0–L2–D。  
2. **[x] 脚本**：`scripts/evaluate_path_change.py` → INSUFFICIENT|REJECT|READY_FOR_HUMAN。  
3. **[x] SUMMARY 文案**：longitudinal_health + 保留 legacy 键。  
4. **[x] champion_selector**：RESEARCH_ONLY=True（layer D）。  
5. **H2H 模板**：结论段只写 diagnostic，L2 另附。

不强制先改 ADR-010 正文；行为与 L2 对齐即可。若日后要改 ADR，再开短 addendum。

---

## 9. 验收标准（架构是否变简单）

- [ ] 任何人问「今天能不能开仓」→ 只查 **L0+L1**  
- [ ] 问「能不能换模型」→ 只查 **L2 一条报告 + human**  
- [ ] 问「H2H 0.45 怎么办」→ **D，不自动改路径**  
- [ ] 代码里不超过 **两处** hard promote 阈值表（L2 + 可选 eligibility）  
- [ ] 新实验默认进 observation，**零** promote 会议

---

## 10. 状态

**✅ Accepted** — 2026-07-26（用户确认；未开完整 Council；不推翻 ADR-010）。

### 生效约定

| 问题 | 只查 |
|---|---|
| 今天能不能开仓？ | **L0 + L1** |
| 能不能换决策路径？ | **L2 + Human** |
| H2H / multi-window / 0.53 / 7-gate？ | **D only**（不否决交易、不单独 promote） |

### 后续实现（unlocked，非阻塞 Accept）

1. [x] `evaluate_path_change` → INSUFFICIENT | REJECT | READY_FOR_HUMAN (`scripts/evaluate_path_change.py`)
2. [x] SUMMARY / H2H 文案：longitudinal_health + layer D wording
3. [x] `champion_selector` 7-gate → RESEARCH_ONLY (layer D)
4. 可选：repo mirror `docs/GATE_DECISION_ARCHITECTURE.md`（与本页同步）

### 与 ADR 关系

- **不推翻** ADR-008 / ADR-010 / Round-4。
- ADR-010 D3 五条 **映射为 L2**；训练禁令仍属训练政策。
- 若未来改 L2 硬规则（恢复绝对 0.53 或 PnL 硬门）→ 再开窄题 Council / addendum。
