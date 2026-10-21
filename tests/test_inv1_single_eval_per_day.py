"""INV-1: one entry evaluation per symbol per day."""
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard


def test_second_eval_skipped(fake_redis):
    def _ismember(key, member):
        return "entry:evaluated" in key

    fake_redis.sismember.side_effect = _ismember
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    skip, code = guard.should_skip_entry("NVDA")
    assert skip
    assert code == "INV-1_ALREADY_EVALUATED"


def test_begin_marks_evaluated(fake_redis):
    """P1-1: begin_entry_evaluation uses atomic Lua EVAL (not pipeline).

    SADD returns 1 → first caller → proceed (ok=True).
    """
    fake_redis.sismember.return_value = False  # no open position, not evaluated
    fake_redis.eval.return_value = 1  # SADD added 1 → first caller wins
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    ok, code = guard.begin_entry_evaluation("AAPL")
    assert ok
    assert code is None
    # Must have called eval (the atomic Lua path), not the old pipeline.
    fake_redis.eval.assert_called_once()
