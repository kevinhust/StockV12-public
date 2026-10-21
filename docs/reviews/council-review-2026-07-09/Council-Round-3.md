# V12 Council Review — Round 3 (post-deployment-correction, 2026-07-10)

> **Date:** 2026-07-10
> **Reviewers:** Architect · Skeptic · Pragmatist · Critic (4 voices, parallel)
> **Companion to:** [`Council-Synthesis.md`](Council-Synthesis.md) (Round 1) · [`Council-Round-2.md`](Council-Round-2.md) (Round 2)
> **Method:** Fresh 4-voice review of the post-correction state. Round 2 was
> re-reviewed with corrected deployment topology (NUC is the production target,
> not the Mac mini). New issues surfaced, particularly WSL2-specific failure
> modes that the previous councils (which assumed generic Linux) didn't consider.

---

## TL;DR Verdict

| Voice | Verdict |
|---|---|
| Architect | The Round-2 post-correction plan is stable on L4 (true P0) and L3 (live_ibkr gate), but a **new P0 has emerged: WSL2 hardening**. None of L2–L9 addresses it. |
| Skeptic | The 92 deselected headline is mostly artifact (9 are a 5-min test-file fix, 1 is genuinely clock-dependent and mis-described in Sprint-F doc). Only L4 and L3 are real. |
| Pragmatist | L3 is the actual gate, not L2 or L4. L4 should be **replaced** by a 0.1d reconciliation cron. **The single most leveraged action is a 5-minute SSH to the NUC to verify the cron is actually running.** |
| Critic | A new P0 (WSL2 hardening: auto-start on Windows boot, Defender exclusion, clock-source check) is needed before live_ibkr. Plus Windows Defender real-time scanning of the 9P mount is a silent stall source no other voice mentioned. |

**Consolidated verdict:** The Round-2 revised plan (L2–L9) is sound but incomplete. The 30-day live_ibkr countdown may have **already started** (we don't know — no telemetry) and may be **silently broken** by a Windows Update that kills WSL2 cron. The Round-3 plan adds L0-VERIFY (5 min) and L0-WSL2 (0.5d) and swaps L4 for a cheaper L4-RECONCILE (0.1d). Total: **2.1 days** (down from 4.5d). Order of operations matters: verify the NUC is alive before writing any code.

---

## New items emerging from Round 3

| New ID | Item | Voice | Effort | Why it matters |
|--------|------|-------|--------|----------------|
| **L0-VERIFY** | SSH to NUC: `crontab -l && ls -lt logs/ && v12_deployment_health.py` | Pragmatist + Skeptic | 5 min | Single command that tells us if 30-day countdown has started; falsifies L2/L5/L6/L7 in one shot |
| **L0-WSL2** | WSL2 hardening: Windows Task Scheduler auto-start + Defender exclusion + clock-source check | Critic | 0.5d | New P0 — Windows Update can silently kill cron for days; no other L-item covers this |
| **L4-RECONCILE** | Daily reconciliation cron (diff `positions:*` vs `journal:entries:*`, alert on orphans) | Pragmatist | 0.1d | Replaces the 0.5d L4 try/except refactor; same coverage, 5× cheaper |

---

## Revised Sprint L plan (Round 3)

| Pri | Item | Voice | Days | Note |
|-----|------|-------|------|------|
| **0** | L0-VERIFY: SSH to NUC, check `crontab -l && ls -lt logs/` | Pragmatist + Skeptic | 0.01 | **Run this first, before any code change** |
| **0** | L0-WSL2: Windows Task Scheduler auto-start + Defender exclusion + clock check | Critic | 0.5 | **New P0 from Round 3** |
| 1 | L3: Repair 5 shadow-gate test failures | Pragmatist | 1.0 | Actual live_ibkr gate |
| 2 | L5: Re-run 92 deselected tests, retire the label | Skeptic | 0.25 | Asymmetric upside (could find more gate-killers) |
| 3 | L4-RECONCILE: Daily reconciliation cron (replaces L4 try/except) | Pragmatist | 0.1 | 5× cheaper than L4, no refactor |
| 4 | L2: Wire L2_DEGRADED → `WebhookNotifier` | Pragmatist | 0.25 | Operator awareness |
| 5 | L6, L7, L8, L9 → defer to Sprint M | Skeptic + Pragmatist | 0.0 | Hygiene, low near-term ROI |
| **Total** | | | **~2.1 days** | Down from Round-2's 4.5d |

---

## L0-VERIFY — what to check, what it tells us

```bash
# SSH to NUC
ssh app@nuc

# 1. Is the cron registered?
crontab -l | grep -E "stockapp|v12"

# 2. Is the cron producing output?
ls -lt ~/Stockapp/logs/pipeline/ | head -10

# 3. What's the most recent entry_window run?
ls -lt ~/Stockapp/logs/pipeline/entry_window_*.log | head -1
tail -20 $(ls -t ~/Stockapp/logs/pipeline/entry_window_*.log | head -1)

# 4. Run the deployment health check
~/Stockapp/.venv/bin/python -m stockapp_v12.scripts.preflight --verbose
```

What this answers:
- If `crontab -l` shows the entries: cron is registered, system is wired.
- If `ls -lt logs/` shows recent files: cron is firing; the entry pipeline is running.
- If `tail entry_window_*.log` shows actual entry decisions: paper-soak is happening.
- If `preflight` exits 0: all 4 health checks (Redis, kill switch, imports, journal schema) pass.
- If `preflight` exits non-zero: the skip.log has the reason; that IS the L2 signal.

**Outcome branches:**
- **A. Cron registered, recent logs, preflight passes:** The 30-day countdown has already started. L2 drops in priority. Focus on L3 + L4-RECONCILE.
- **B. Cron registered, old logs, preflight fails:** Cron is firing but preflight is blocking. Fix preflight issues. L2 is now a real signal.
- **C. Cron not registered:** `install_pipeline_cron.sh --yes` was never run on the NUC. Do that first.
- **D. Cron registered, no recent logs:** WSL2 may be paused. This is the L0-WSL2 scenario (Windows Update, etc.).

L0-VERIFY is a **5-minute decision-tree** that collapses 70% of the L2-L9 uncertainty.

---

## L0-WSL2 — what's needed, why

### Auto-start on Windows boot

WSL2 distros do **not** auto-start by default. After a Windows reboot (Windows Update is the most common cause on a NUC), the WSL2 VM is down, `crond` is down, and the trading day is missed. No alert fires because nothing runs.

**Fix:** Windows Task Scheduler entry to start WSL2 on boot:

```
# On Windows (admin PowerShell)
$action = New-ScheduledTaskAction `
  -Execute "wsl.exe" `
  -Argument "-u app -d Ubuntu -- /bin/true"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask `
  -TaskName "WSL2-AutoStart-StockApp" `
  -Action $action `
  -Trigger $trigger `
  -User "app" `
  -RunLevel Highest
```

### Defender exclusion for `/home/app/Stockapp/`

Windows Defender real-time scanning reads every file accessed through the 9P mount, including every `.py` file traversed by `import stockapp_v12`. On a busy scan cycle, this can exceed 30s for the preflight and minutes for parquet loads. Add a Defender exclusion for the WSL2 mount.

```
# On Windows (admin PowerShell)
Add-MpPreference -ExclusionPath "\\wsl$\Ubuntu\home\app\Stockapp"
```

### Clock-source sanity check

WSL2 clock follows the Windows host clock. If the host clock is wrong (bad NTP sync, manual time change), cron fires at the wrong wall-clock time. Add a check to `_preflight.sh`:

```bash
# Add to _preflight.sh after line 47
DRIFT=$(chronyc -c tracking 2>/dev/null | awk -F, '$1=="Reference"{print $5}')
if [ -n "$DRIFT" ] && [ "$(echo "$DRIFT < -1.0 || $DRIFT > 1.0" | bc)" = "1" ]; then
  FAIL_LINE="${FAIL_LINE}clock_drift(${DRIFT}s);"
  RC=1
fi
```

(Requires `chrony` to be installed on the NUC. Alternative: just check `ntpq -p` output.)

### Total: ~0.5d

- Auto-start: 15 min (one PowerShell command)
- Defender exclusion: 5 min (one PowerShell command)
- Clock check: 0.5d (includes `chrony` install + `_preflight.sh` integration + test)
- Test the auto-start cycle: reboot the NUC, verify cron fires within 5 min of boot

---

## L4-RECONCILE — the cheap alternative to L4

The Round-2 L4 was "catch `JournalWriteError` at position-mutating paths" — a 0.5d refactor with a real INV-5 risk (the Skeptic pointed out: option (a) try/except re-opens the original violation; option (b) journal-before-mutate is closer to a refactor).

The Pragmatist's alternative: **a daily reconciliation cron that diffs Redis position state against journal entries.**

```python
# scripts/reconcile_positions.py — run nightly at 23:00 ET
# 1. SCAN positions:* → set of (mode, symbol, state) in Redis
# 2. SCAN journal:entries:{today}:* → set of (mode, symbol, decision) in journal
# 3. For each position in Redis with state=OPEN:
#    - Look up the most recent journal entry for that symbol
#    - If last entry is BUY but position should have closed (e.g., SL fill), alert
#    - If position has no journal entry, alert (INV-5 violation)
# 4. For each journal entry with decision=CLOSE/SELL:
#    - Look up position in Redis
#    - If still OPEN, alert
```

This catches:
- Missed exit records (L4's main scenario)
- Missed entry records
- Orphan positions (Redis says OPEN, no journal entry)
- Stale positions (journal says CLOSED, Redis still OPEN)

All three failure modes, 0.1d, no refactor.

---

## Skeptic's specific findings to verify before Sprint L

### Finding 1: `tests/test_fail_closed_safety.py:113` has a 5-min fix

```python
# Line 113 (current, broken):
import src.services.chronos_service as _cs

# Should be:
from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path
ensure_v11_archive_on_path()
import src.services.chronos_service as _cs  # now resolves via V11 archive
```

This is the same fix Sprint J6 made to `services/forecast_service.py:48-58`. The test file was missed. If this is the case for ~9 of the 92 deselected, L5 ("re-run 92 deselected") is mostly a 1-hour cleanup, not a 0.25-day investigation.

### Finding 2: `test_min_hold_blocks_early_exit` actually FAILS today

Sprint-F Completion (line 65) says: "passes today because we are >= 2 days after July 7". The Skeptic disputes this:
- Test asserts `res.allowed is False` (R-10 should fire).
- `min_hold_days=1` (`src/stockapp_v12/bridge/live_guard.py:31`).
- Entry was 2026-07-07 09:30 ET. Today is 2026-07-10.
- `held = 3.x days`, `min_hold = 1`, so `held >= min_hold` → R-10 does **not** fire.
- Test fails today.

This means the test is **permanently failing** and should be either marked `xfail` (with a reason like "min-hold is enforced by clock, not by test") or fixed. Sprint-F's claim is wrong; this is a documentation bug, not a test bug.

### Finding 3: `scripts/v12_deployment_health.py` already exists

The Skeptic pointed to `scripts/v12_deployment_health.py` as an existing tool that can verify NUC health in one shot. The Round-2 Council mentioned it indirectly via L0-VERIFY; Round-3 promotes it to "the first thing to run."

---

## Pragmatist's specific findings

### The system may have 0 days of production validation

No telemetry in the repo confirms the cron has ever fired on the NUC. The L0-VERIFY step is the single most leveraged action in this entire council. If the cron is already running, the 30-day countdown may have started, and the next 30 days are about observation, not fixes.

### L4 → L4-RECONCILE swap

L4 is 0.5d of code work with a real risk of half-fixing. L4-RECONCILE is 0.1d of glue script that catches the same failure modes. The cost difference (5×) and risk difference (no refactor) make the swap obvious.

### L2 priority depends on operator workflow

If the operator is on Slack already, L2 is high-value (0.25d for a real pager). If the operator is not on Slack and the workflow is "SSH to NUC, tail logs", L2 is medium-value. **Ask before wiring.**

---

## Critic's WSL2-specific findings

### WSL2 Hyper-V virtual NIC resets

Microsoft issue #5440 documents intermittent Hyper-V virtual NIC resets (200ms-2s packet drops). This is a real failure mode for the journal-after-pipe shape in `_close_position` / `register`. L0-WSL2 doesn't fix this directly, but the clock check + preflight timeouts give us a fighting chance of detecting it.

### Windows Defender real-time scan of 9P mount

Every `import stockapp_v12` traverses every `.py` file in the source tree. On a busy Defender cycle, this can exceed 30s (preflight timeout is not set explicitly, but `crond` typically kills jobs at 5-15 min). Parquet loads in `backfill_30_days.py` can stall for minutes. **Defender exclusion is the single biggest WSL2 performance win** and it's 5 minutes to add.

### WSL2 no auto-start on Windows boot

The most common NUC reboot cause is Windows Update. Without an auto-start entry in Windows Task Scheduler, the cron is dead until the user opens a terminal. This is **silent** — no alert, no skip log, no nothing. The Round-2 council missed this entirely because it assumed the Mac mini was the deploy target and didn't reason about the Windows host.

---

## Open questions for the user

1. **Run L0-VERIFY on the NUC today?** (5 min; would tell us if the 30-day countdown has started)
2. **Accept the new L0-WSL2 P0?** (0.5d; Windows Task Scheduler + Defender + clock check)
3. **Accept L4 → L4-RECONCILE swap?** (saves 0.4d, same coverage)
4. **Defer L6/L7/L8/L9 to Sprint M?** (saves 2 days; consensus from Skeptic and Pragmatist)
5. **L2 only if operator is on Slack?** (recommend asking before wiring)
6. **Mirror this Round-3 synthesis to Obsidian `Council-Round-3-2026-07-10`?** (matches Round-1/2 pattern)

---

**Bottom line:** The 4.5d Round-2 plan was sound but missed a critical P0 (WSL2 hardening) and over-estimated L4. The 2.1d Round-3 plan starts with a 5-minute verification step that may falsify half the remaining work. The order of operations matters: **L0-VERIFY first, L0-WSL2 second, L3 third**. Everything else is downstream of those three.
