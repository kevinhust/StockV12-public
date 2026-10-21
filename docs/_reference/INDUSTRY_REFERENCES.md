# Industry References — Swing Trading Best Practices

> Sources and rationale behind V12 design decisions.

---

## Methodologies Referenced

### 1. Mark Minervini — SEPA (Specific Entry Point Analysis)

**Book**: *Trade Like a Stock Market Wizard* (2013), *Think & Trade Like a Champion* (2017)

**Key principles adopted in V12**:

| SEPA Principle | V12 Implementation |
|---------------|-------------------|
| Trend template (8 criteria) | SwingScanner: above 20 EMA, within 25% of 52w high |
| Risk-first position sizing | 1% risk per trade, ATR-based stop |
| Cut losses at 7–8% max | SL at 1–1.5× ATR (typically 5–8% for target stocks) |
| Let winners run | Trailing stop, no fixed TP |
| Concentrate in best setups | Max 8 positions, quality-filtered |
| Avoid buying extended stocks | Pivot proximity scoring |
| Earnings as catalyst | CatalystScorer `earnings` type |

**Minervini's position sizing**:
```
Risk per trade = 1% of account
Position size = Risk amount / Stop distance
```
This is exactly V12's `calculate_position_size()` formula.

### 2. William O'Neil — CAN SLIM

**Book**: *How to Make Money in Stocks* (1988, multiple editions)

**Key principles adopted**:

| CAN SLIM | V12 Element |
|----------|-------------|
| C — Current quarterly earnings | CatalystScorer `earnings` detection |
| A — Annual earnings growth | Not directly (we're shorter-term) |
| N — New (product, management) | CatalystScorer `product`, `manda` types |
| S — Supply/demand (volume) | Volume breakout score in SwingScanner |
| L — Leader (relative strength) | Relative strength vs SPY in scoring |
| I — Institutional sponsorship | Not implemented (future) |
| M — Market direction | Regime-based trading gates |

**O'Neil on concentrated portfolios**:
> "The winning strategy is to have at most 5-8 stocks, and really understand
> them. Over-diversification is a hedge for ignorance."

V12: max 8 concurrent positions.

### 3. Kristjan Qullamaggie (Modern Swing)

**Source**: Twitter/X (@Qullamaggie), YouTube educational series

**Key principles**:

| Principle | V12 Implementation |
|-----------|-------------------|
| "Wait for setups" — don't force trades | Max 3 trades/day, 0 on most days |
| Daily close above pivot + volume | SwingScanner breakout detection |
| Hold 2–10 days typically | Min hold 1 day, target 2–15 days |
| Trail with 10 or 20 EMA | ATR-based trailing (similar concept) |
| "The best trades work right away" | DailyReview: exit after 5 days of loss |
| No trading in choppy markets | BEAR_LONG/PANIC → block all entries |

### 4. Prop Trading Desk Standards

**Sources**: SMB Capital (Mike Bellafiore), T3 Trading Group practices

| Prop Standard | V12 Rule |
|--------------|----------|
| Daily loss limit: 2% | R-01: -2% NAV circuit breaker |
| Max drawdown: 5–10% | R-02: -8% hard stop |
| Review every trade | TradeJournal with MFE/MAE |
| No revenge trading | 48h symbol cooldown after exit |
| Size down in adverse conditions | Regime-based sizing multiplier |
| No trading in first 5 min | Entry window opens 09:35 (5 min after open) |

---

## Academic/Quantitative References

### Position Sizing: Kelly Criterion vs Fixed Risk

V11 used Kelly-inspired sizing (conviction-weighted). V12 uses fixed-risk (1% per trade).

**Why the change**:
- Kelly assumes you know your true edge (we don't, with LLM-based signals)
- Fixed-risk is robust to estimation error
- Most professional traders use 0.5–2% risk per trade
- Reference: *The Mathematics of Money Management* (Ralph Vince, 1992)

### Trailing Stops: ATR-based vs Percentage-based

V12 uses ATR-based trailing stops instead of fixed percentage.

**Why**:
- ATR adapts to each stock's volatility
- A 5% stop makes sense for a $500 low-vol stock but not for a $15 high-vol stock
- Reference: Wilder (1978), *New Concepts in Technical Trading Systems* (original ATR)
- Modern practice: Keltner Channels, ATR-based exits widely used

### Regime Detection

V12 retains V11's VIX-based regime system (6 states).

**Supporting research**:
- Ang & Bekaert (2002), "Regime switches in interest rates"
- Hamilton (1989), Markov-switching models
- Practical: VIX > 25 correlates with mean-reverting behavior; VIX < 18 correlates with trend persistence

---

## What V12 Intentionally Does NOT Do

| Approach | Why Not |
|----------|---------|
| Pairs trading | Requires co-integration monitoring, different skillset |
| Options strategies | Complexity, theta management, different risk model |
| Sector rotation | Our edge is stock-level, not macro-level |
| Mean reversion | V12 is trend-following; mean reversion is a different system |
| News-based ultra-fast trading | That's HFT territory; we wait for daily confirmation |
| Machine learning signal generation | LLM for analysis OK, but no black-box signal model |
| Portfolio optimization (Markowitz) | Overkill for 3–8 positions; simple rules suffice |

---

## Recommended Reading

### Essential (directly applicable to V12)
1. Minervini, *Trade Like a Stock Market Wizard* — SEPA entry criteria
2. O'Neil, *How to Make Money in Stocks* — CAN SLIM fundamentals
3. Schwager, *Market Wizards* series — Trader psychology and risk management

### Advanced
4. Tharp, *Trade Your Way to Financial Freedom* — Position sizing and expectancy
5. Covel, *Trend Following* — Trailing stop philosophy
6. Douglas, *Trading in the Zone* — Psychology of consistent execution

### Quantitative Background
7. Wilder, *New Concepts in Technical Trading Systems* — ATR, RSI origins
8. Vince, *The Mathematics of Money Management* — Kelly and optimal f
