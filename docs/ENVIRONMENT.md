# Environment Inventory — Chronos Training (2026-06-29)

> **Note (2026-07-10):** This doc inventories the **training environments** (dev machine + GPU box) for the Round-1 Chronos retrain. It is **not** the production deployment reference — that is [DEPLOYMENT.md](DEPLOYMENT.md). The production trading pipeline (cron + paper-soak) runs on the NUC, not on any machine described here.
>
> **Plan**: [Stage 2.x Round-1 plan](V3_LESSONS_AND_EXP_PLAN.md) · [Stage 1 CI gate](V3_POSTMORTEM_AND_TRAINING_PLAN.md)

本次 (2026-06-29) 盘点的 training environment 真值；与 Obsidian vault `Projects/StockApp-V12/model-training/Environment-Inventory.md` 同步。

## 机器拓扑

| 角色 | 机器 | 备注 |
|------|------|------|
| 数据 + 模型 development | **Mac mini**（Apple Silicon） | MPS 可用；AutoGluon Chronos MPS 路径未验证 |
| 真训练（CUDA） | **GPU box** via `ssh gpu` → `trainer@WSL2` | RTX 3060 / torch 2.9.1+cu128 |
| v2 production live | 两侧都有一份 `models/chronos/phase2_v2/`（2026-06-25 训练） | 不动 |

## Mac mini（本机 `/Users/dev/Stockapp`）

- Python 3.12.9（pyenv shims；**没有专门的 venv**）
- torch 2.9.1 — CUDA=False, MPS=True
- autogluon.timeseries 1.5.0
- pandas 2.3.3 · pyarrow 23.0.1 · pyyaml 6.0.3
- disk: 511 GB free / 931 GB total
- phase2_v2: ✅ 234 MB（2026-06-25）
- feature parquet: ✅ `data/features/daily/` 203 个文件
- 所有 repo 子目录（src/, scripts/, config/, plans/ 等）
- H2H eval 的权威输出在本机（`models/chronos/h2h_v3_report.json`）

**任何长时间训练 / GPU 训练 → 走 GPU box。** Mac mini 适合：验证 / 推理 / 数据准备 / git / 编辑 / Obsidian 整理。

## GPU box（`ssh gpu` → `/home/trainer/Stockapp`）

| 项 | 值 |
|---|---|
| OS | Linux 6.6.87.2-microsoft-standard-WSL2 |
| GPU | NVIDIA GeForce RTX 3060（CUDA via `/dev/dxg`，无 `/dev/nvidia*`）|
| venv | `/home/trainer/yolo_train_env` |
| Python | 3.12.3 |
| torch | 2.9.1+cu128 — CUDA available: 1 device |
| autogluon.timeseries | 1.5.0 |
| pandas / pyyaml / pyarrow / loguru | 2.3.3 / 6.0.3 / 20.0.0 / 0.7.3 |
| disk | 862 GB free / 1007 GB total |

GPU box 上 `~/Stockapp/` 是精简训练 env，**只有 5 个子目录**：
```
~/Stockapp/
  configs/experiments/        (8 个 exp_E*.yaml)
  data/features/daily/        (203 个 .parquet，从 Mac mini sync)
  models/chronos/             (phase2_v1/v2, baseline_v1/zeroshot, h2h_v3_report.json)
  experiments/                (rsync 早期残留空目录，没用)
  v12/scripts/                (run_experiment.py)
  v12/tests/                  (test_run_experiment.py)
  v12/docs/                   (V3_LESSONS_AND_EXP_PLAN.md)
```

**没有 src/、scripts/、config/、.claude/、Plans.md**——GPU box 不是 repo 完整克隆，只是训练需要的最小集合。

## rsync 同步约定（已建立一次，2026-06-29）

```bash
# 0. 准备目录（GPU box）
ssh gpu "mkdir -p ~/Stockapp/{models/chronos,data/features/daily,configs/experiments}"
ssh gpu "mkdir -p ~/Stockapp/v12/{scripts,tests,docs}"

# 1. 模型 + H2H 报告 + baseline 报告
rsync -avz --exclude='__pycache__' --exclude='*.pyc' \
  models/chronos/{phase2_v1,phase2_v2,baseline_v1,baseline_v1_zeroshot} \
  models/chronos/h2h_v3_report.json \
  gpu:~/Stockapp/models/chronos/

# 2. 特征 parquet
rsync -avz data/features/daily/ gpu:~/Stockapp/data/features/daily/

# 3. Round 1 编排
rsync -avz v12/scripts/run_experiment.py        gpu:~/Stockapp/v12/scripts/
rsync -avz v12/tests/test_run_experiment.py     gpu:~/Stockapp/v12/tests/
rsync -avz v12/docs/V3_LESSONS_AND_EXP_PLAN.md  gpu:~/Stockapp/v12/docs/
rsync -avz configs/experiments/                 gpu:~/Stockapp/configs/

# 4. venv 补齐（GPU box）
ssh gpu "source ~/yolo_train_env/bin/activate && pip install --quiet pytest pyyaml"
```

每次代码改动再做对应文件单向 rsync，不全量。

## ⚠️ v3 narrative evidence gap（重要诚实记录）

之前提交历史里有一段叙事："v3 retrain 1M 步完成 / 100 个 chunk 都 archive"。**今天 verify 没有发现物质证据**：

| 检查项 | Mac mini | GPU box |
|---|---|---|
| `_archive/phase2_v3_1M_2026-06-29/` 100 个 ckpt | ❌ | ❌ |
| 任何 `phase2_v3_*` 模型目录 | ❌ | ❌ |
| `h2h_v3_report.json` | ✅ 引用 v3 ckpt | ✅ |
| `/tmp/chronos_v3_1M.log`（v3 retrain 实时日志）| n/a | ❌（log 文件已不存）|

`h2h_v3_report.json` 数字本身不变（它是 eval JSON），但它引用的 ckpt 现在不存在 → **那些数字无法复现**。

这意味着 Round 1 **不依赖任何 v3 阶段的产物作 baseline**——用户决定（2026-06-29）"从现在开始重新训练都可以"，把入口设成 `autogluon/chronos-bolt-small` 的 clean fresh fine-tune，独立校准 hit_rate / mase。

## Round 1 训练起点（"重新开始"）

| 项 | 值 |
|---|---|
| 起点模型 | `autogluon/chronos-bolt-small`（HF 默认权重，不带 phase2_v2 warm-init）|
| 起点数据 | `data/features/daily/*.parquet`（203 sym）|
| 训练脚本 | `v12/scripts/train_chronos_baseline.py --mode finetune` |
| 评估 | `v12/scripts/eval_chronos_h2h_v3.py`（带 `--smoke` CI variant）|
| 编排 | `v12/scripts/run_experiment.py --config configs/experiments/exp_E0X.yaml` |
| Promotion gate（默认） | `hit_rate_mean ≥ 0.55` ∧ `mase_mean ≤ 2.5152 × 1.05` ∧ `hit_rate_std ≤ 0.05`|
| 输出 | `models/chronos/_experiments/<exp_id>_<name>/<run_id>/` + `index.json` + `experiment_dashboard.md`|

8 个实验见 [`configs/experiments/`](../../configs/experiments/)：E01 5-seed fresh 1k step baseline；E02 5k step；E03 LR/10；E04 warm-init v2（待 trainer 升级 `--base-model-path`）；E05 context 1024；E06 LR×3；E07 分类头（Round 2 deferred）；E08 BEAR-only（Round 2 deferred）。

## 当前 live 生产配置

- **Live model**：`models/chronos/phase2_v2/`（v12 trading pipeline 在用，**不**因本次盘点改变）
- **Live runtime**（scheduler、paper-soak monitor）在 **NUC（Linux WSL on Windows）**，不是本机（Mac mini）。本机只跑 dev / 验证 / 推理 / 数据准备 / git。详见 [DEPLOYMENT.md](DEPLOYMENT.md)。
- **Round 1 实验**：GPU box 上跑 `v12/scripts/run_experiment.py`，输出进 `models/chronos/_experiments/`

## 接下来

1. E01 smoke（GPU box 端到端跑 1 次，确认 train → smoke → full → aggregate → gate → dashboard 全链路）
2. dashboard / index 自动生成 verify 通过
3. 启动 E02–E06（按 GPU time budget 排，不撞 Mac mini 上的 live trading）
4. 6 实验跑完 → Council 4-voice 评审 → 决策 Round 2 / E07+E08
