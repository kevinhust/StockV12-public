# V12 Risk Management

> **Scope**: Hard constraints, sizing, and runtime enforcement.  
> **Rule**: If a rule is not enforced in `RiskGatekeeper` (or named delegate), it does not exist.

**Implementation target**: `src/core/risk_controller.py` → `validate_order_intent()`

---

## Enforcement Model

```
Order intent (BUY/SELL, symbol, size_pct, confidence)
        │
        ▼
┌───────────────────┐
│ RiskGatekeeper    │  ← single entry/exit authority for risk
│ validate_order_   │
│ intent()          │
└─────────┬─────────┘
          │
    allow │ deny (reason code R-xx)
          ▼
   BracketOrderService / PositionManager
```

**Prohibited**: LLM, scanner, or scheduler bypassing Gatekeeper for live orders.

---

## Red Lines (Runtime Required)

| Code | Rule | Threshold | Config key | Enforcer | On violation |
|------|------|-----------|------------|----------|--------------|
| R-01 | Daily loss limit | PnL ≤ -2% NAV | `global_limits.daily_loss_limit_pct` | Gatekeeper | Block BUY |
| R-02 | Max drawdown | > -8% from peak | `global_limits.max_drawdown_pct` | Gatekeeper | Block BUY |
| R-03 | Single position cap | > 3% NAV | `global_limits.max_single_position_pct` | Gatekeeper | Reject order |
| R-04 | Portfolio exposure | > 40% NAV | `global_limits.max_portfolio_exposure_pct` | Gatekeeper | Block BUY |
| R-05 | Daily trade count | ≥ 3 entries | `global_limits.max_daily_trades` | Gatekeeper | Block BUY |
| R-06 | Symbol cooldown | < 48h since exit | `global_limits.symbol_cooldown_hours` | Gatekeeper | Block BUY |
| R-07 | Regime lock | PANIC, BEAR_LONG | `regime_params` + regime service | Gatekeeper | Block BUY |
| R-08 | Correlation | ρ > 0.75 vs holding | `global_limits.max_correlation_threshold` | Gatekeeper | Size × 0.5 or reject |
| R-09 | Entry window | Outside 09:35–15:30 ET | `scheduler.yaml` → `entry_window` | Gatekeeper | Block BUY |
| R-10 | Min hold | SELL with age < 1 day | `global_limits.min_hold_days` | Gatekeeper + PositionManager | Block SELL* |

\*Exception: SL fill at broker always allowed (PositionManager marks CLOSED).

### Counter keys (Redis)

| Key | Writer | Reader |
|-----|--------|--------|
| `risk:daily_pnl:{date}` | Portfolio sync | R-01 |
| `risk:daily_trades:{date}` | Gatekeeper after fill | R-05 |
| `risk:cooldown:{symbol}` | PositionManager on exit | R-06 |
| `market_regime:current` | Regime service | R-07, sizing |

---

## Position Sizing

### Policy (authoritative)

| Role | `confidence` | `risk_per_trade` / ATR stop |
|------|--------------|----------------------------|
| Allow / deny entry | **Yes** — must be ≥ `min_entry_confidence` (pipeline) | No |
| Base position size | **No** — must not appear in core formula | **Yes** — sole drivers |
| Optional size cap discount | **Optional** — see below | — |

**LLMAnalyst does not output `size_pct` or share count.**

### Formula (authoritative — core)

```python
risk_amount = nav * risk_per_trade_pct                    # default 0.01
stop_distance = atr_14 * sl_atr_multiple
shares = risk_amount / stop_distance
dollar = shares * entry_price
dollar = min(dollar, nav * max_single_position_pct)
dollar *= REGIME_SIZING[regime]                           # regime only
# confidence MUST NOT multiply dollar here
return dollar
```

### Optional confidence cap (not part of core formula)

If enabled in config (`sizing.confidence_cap_discount_enabled: true`):

```python
# Applied AFTER core formula, as a ceiling only (never sizes up)
max_dollar_from_confidence = nav * max_single_position_pct * min(confidence / 0.7, 1.0)
dollar = min(dollar, max_dollar_from_confidence)
```

Default for V12: **`confidence_cap_discount_enabled: false`** — confidence affects PASS/BUY only.

### Parameters

| Parameter | Default | Config path |
|-----------|---------|-------------|
| `risk_per_trade_pct` | 0.01 | `global_limits.risk_per_trade_pct` |
| `max_single_position_pct` | 0.03 | `global_limits.max_single_position_pct` |
| `sl_atr_multiple` | tier-based | `stop_loss.*_atr_multiple` |

### Regime sizing multipliers

| Regime | Multiplier |
|--------|------------|
| BULL_LONG | 1.0 |
| BULL_SHORT | 0.9 |
| NEUTRAL | 0.8 |
| BEAR_SHORT | 0.6 |
| BEAR_LONG | 0.0 (no new BUY) |
| PANIC | 0.0 |

---

## Stop Loss & Trailing (Risk Parameters)

Placed by **BracketOrderService**; distances from config:

| Tier | `sl_atr_multiple` | Config |
|------|-------------------|--------|
| bluechip | 1.5 | `stop_loss.bluechip_atr_multiple` |
| mid_vol | 1.5 | `stop_loss.mid_vol_atr_multiple` |
| high_vol | 1.2 | `stop_loss.high_vol_atr_multiple` |
| extreme_vol | 1.0 | `stop_loss.extreme_vol_atr_multiple` |

Trailing (managed by PositionManager, distances from risk config):

| Parameter | Default | Config |
|-----------|---------|--------|
| `activation_atr` | 1.0 | `trailing_stop.activation_atr` |
| `trail_distance_atr` | 2.0 | `trailing_stop.trail_distance_atr` |

**Rule**: SL only tightens after entry; trail price only ratchets up.

---

## Portfolio Limits

| Limit | Value | Config | Enforcer |
|-------|-------|--------|----------|
| Max concurrent positions | 8 | `global_limits.max_concurrent_positions` | PreMarket + Gatekeeper |
| Max per sector | 3 | `global_limits.max_stocks_per_sector` | Gatekeeper |
| Max sector exposure | 30% | `global_limits.max_sector_concentration_pct` | Gatekeeper |

---

## PANIC & Forced Exit

| Condition | Action | Owner |
|-----------|--------|-------|
| Regime → PANIC | Block new BUY; DailyReview queues EXIT all | Gatekeeper + DailyReview |
| Daily loss R-01 tripped | Block BUY only; SL remains | Gatekeeper |

**Removed from V11**: blanket EOD 15:45/15:58 liquidation (unless `eod_liquidation.panic_override` in trading_modes).

---

## Parameter Authority Registry

> **Canonical file**: `config/risk_regimes.yaml` (V12 section) + `config/scheduler.yaml` for window times.

| Parameter | YAML path | Consumer | Validator (test) |
|-----------|-----------|----------|------------------|
| `max_daily_trades` | `global_limits.max_daily_trades` | Gatekeeper R-05 | `test_r05_daily_cap` |
| `max_single_position_pct` | `global_limits.max_single_position_pct` | Gatekeeper R-03 | `test_r03_single_cap` |
| `max_portfolio_exposure_pct` | `global_limits.max_portfolio_exposure_pct` | Gatekeeper R-04 | `test_r04_exposure` |
| `daily_loss_limit_pct` | `global_limits.daily_loss_limit_pct` | Gatekeeper R-01 | `test_r01_daily_loss` |
| `max_drawdown_pct` | `global_limits.max_drawdown_pct` | Gatekeeper R-02 | `test_r02_drawdown` |
| `symbol_cooldown_hours` | `global_limits.symbol_cooldown_hours` | Gatekeeper R-06 | `test_r06_cooldown` |
| `min_hold_days` | `global_limits.min_hold_days` | Gatekeeper R-10 | `test_r10_min_hold` |
| `max_concurrent_positions` | `global_limits.max_concurrent_positions` | PreMarket, Gatekeeper | `test_max_positions` |
| `risk_per_trade_pct` | `global_limits.risk_per_trade_pct` | Sizing function | `test_sizing_risk_based` |
| `entry_window_open/close` | `scheduler.yaml` | Gatekeeper R-09 | `test_r09_window` |
| `max_correlation_threshold` | `global_limits.max_correlation_threshold` | Gatekeeper R-08 | `test_r08_correlation` |

**Change process**: Edit YAML → restart service → run bound test → update this table if path changes.

---

## V11 Gap (Why V12 Exists)

| Parameter | V11 YAML | V11 Code | V12 |
|-----------|----------|----------|-----|
| `max_daily_trades` | 12 | not enforced | 3, enforced |
| `daily_loss_limit_pct` | 2% | -5% | 2%, aligned |
| `max_single_position_pct` | 2% | 5% | 3%, aligned |
| `max_portfolio_exposure_pct` | 20% | 150% | 40%, aligned |
| `min_hold_days` | — | — | 1, enforced |

---

## Acceptance (Risk Module)

| ID | Criterion | Test |
|----|-----------|------|
| RK-1 | 4th BUY same day denied with R-05 | `test_r05_daily_cap` |
| RK-2 | BUY within 48h of exit denied with R-06 | `test_r06_cooldown` |
| RK-3 | SELL before min hold denied with R-10 | `test_r10_min_hold` |
| RK-4 | SL fill still closes position | `test_sl_bypasses_min_hold` |
| RK-5 | PnL -2.1% blocks new BUY | `test_r01_daily_loss` |
| RK-6 | All registry params loaded at startup | `test_config_load_matches_registry` |

Program-level checklist: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md#acceptance-checklist).

---

## Decision Explainability Contract

Every risk decision that blocks or permits capital must be journalized.

### DENY (Gatekeeper)

| Field | Required | Example |
|-------|----------|---------|
| `decision` | yes | `DENY` |
| `reason_code` | yes | `R-05`, `R-06`, `R-09` |
| `reason_detail` | yes | human-readable one line |
| `symbol` | yes | `NVDA` |
| `side` | yes | `BUY` |
| `stage` | yes | `entry` |
| `regime` | yes | `BULL_LONG` |
| `requested_size_pct` | if present | `0.02` |
| `allowed_size_pct` | always | `0` |

### BUY allow (Gatekeeper)

| Field | Required |
|-------|----------|
| `decision` | `ALLOW` |
| `reason_code` | `OK` or sub-code |
| `allowed_size_pct` | yes — from risk formula only |
| `risk_amount`, `stop_distance`, `atr_14` | yes — for audit replay |

Writer: TradeJournal (called by Gatekeeper).  
Analyst fields: [TRADING_PIPELINE.md](TRADING_PIPELINE.md#llmanalyst-output-contract).
