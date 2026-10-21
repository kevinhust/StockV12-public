# Champion Selection — Operator Runbook

## What is this?

Champion model selection is the mechanism for picking which of the 9
shadow models drives trading decisions. The champion + challenger pair
runs daily via NUC cron; an automated HaltGate can swap them on poor
performance; quarterly reviews re-evaluate all candidates.

## Daily Operations

### View current champion and challenger

```bash
cat /home/app/Stockapp/models/chronos/_shadow/_state/champion_config.json
```

### View recent halt swaps

```bash
cat /home/app/Stockapp/models/chronos/_shadow/_state/halt_swap.json 2>/dev/null
ls -lt /home/app/Stockapp/models/chronos/_shadow/_state/*.json
```

### Run evaluation manually

```bash
cd /home/app/Stockapp
PYTHONPATH=src:scripts:. .venv/bin/python scripts/champion_evaluation.py --report
```

## Quarterly Review (every 60 trading days)

When the reminder log fires (`/home/app/Stockapp/logs/quarterly_review_reminder.txt`):

1. Run full evaluation
2. Compare PnL/Sharpe/MASE across all candidates
3. Decide: keep current champion, swap, or restart from scratch
4. Update `_shadow/_state/champion_config.json`:

```bash
cat > /home/app/Stockapp/models/chronos/_shadow/_state/champion_config.json <<EOF
{
  "champion": "<new_champion>",
  "challenger": "<new_challenger>",
  "promoted_at": "$(date +%Y-%m-%d)",
  "promotion_rationale": "<reason>",
  "next_review_date": "<+60 trading days>"
}
EOF
```

5. Commit the new config:

```bash
cd /Users/dev/Stockapp
git add models/chronos/_shadow/_state/champion_config.json
git commit -m "promote: champion=X, challenger=Y (quarterly review)"
```

## HaltGate Swap Recovery

If a halt swap occurs and the original champion recovers, do NOT
auto-restore. Wait for the next quarterly review. Manual override
only if the original champion has 7+ consecutive days of >55% win
rate AFTER the halt trigger date.

## Troubleshooting

### No model passes all 8 gates

This is a healthy failure. The evaluation script will print "No model
passed all 8 gates". Two options:
- Wait: more OOS data accumulates weekly.
- Lower the gate: but this is dangerous and requires council review.

### HaltSwapFile writes but cron doesn't pick it up

Check:
1. File exists and is valid JSON.
2. `STATE_DIR` in run_shadow_cron.sh matches the file's parent dir.
3. Cron output: `/tmp/shadow_cron.log` should print "halt_swap detected".
