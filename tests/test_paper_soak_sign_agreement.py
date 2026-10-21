"""TDD tests for SOAK-7 sign-agreement helpers (paper_soak_checklist.py).

Background (v3 postmortem → v12/docs/V3_POSTMORTEM_AND_TRAINING_PLAN.md,
v12/docs/H2H_V3_REPORT.md "Directional (sign-correctness) metrics"):
phase2_v2 (the live Chronos model) scored hit_rate=0.30 on a 10-symbol OOS
panel — i.e. sign(forecast) was correct <50% of the time. This is a smoking
gun for the P6 paper-soak: when Chronos triggers an entry, the direction
is essentially random.

SOAK-7 tracks **live** sign agreement between Chronos forecast (recorded at
entry-attempt time) and actual trade side (BUY/SELL). It is the paper-soak
counterpart to the H2H OOS hit_rate metric. It does not gate the soak
(it's a warn-only check by design — see config/paper_soak.yaml), because
a low live agreement rate during P6 is consistent with the known OOS
finding and we want to *measure* it, not block on it.

These tests cover only the pure functions. The Redis I/O and full
check_sign_agreement integration are exercised via the runner.
"""

from __future__ import annotations

import pytest

from scripts.paper_soak_checklist import (
    sign,
    side_to_sign,
    sign_match,
    sign_agreement_rate,
)


# ── sign() ──────────────────────────────────────────────────────────────
class TestSign:
    def test_positive(self):
        assert sign(1.5) == 1
        assert sign(0.001) == 1

    def test_negative(self):
        assert sign(-1.5) == -1
        assert sign(-0.001) == -1

    def test_zero(self):
        assert sign(0) == 0
        assert sign(0.0) == 0

    def test_above_threshold(self):
        # Default threshold is 0.0 — anything strictly above counts as +1.
        # Below threshold counts as 0.
        assert sign(0.5) == 1
        assert sign(-0.5) == -1


# ── side_to_sign() ──────────────────────────────────────────────────────
class TestSideToSign:
    def test_buy(self):
        assert side_to_sign("BUY") == 1

    def test_sell(self):
        assert side_to_sign("SELL") == -1

    def test_none(self):
        assert side_to_sign(None) == 0

    def test_unknown_string(self):
        # Defensive: any unrecognized side counts as 0 (flat / no signal).
        assert side_to_sign("HOLD") == 0
        assert side_to_sign("") == 0


# ── sign_match() ────────────────────────────────────────────────────────
class TestSignMatch:
    def test_both_positive(self):
        assert sign_match(0.6, "BUY") is True

    def test_both_negative(self):
        assert sign_match(-0.7, "SELL") is True

    def test_disagree_pos_fc_sell_side(self):
        # Forecast positive but went SELL → no agreement
        assert sign_match(0.6, "SELL") is False

    def test_disagree_neg_fc_buy_side(self):
        assert sign_match(-0.6, "BUY") is False

    def test_flat_fc_buy_counts_as_no_match(self):
        # sign(0) = 0, side_to_sign("BUY") = 1 → no match
        # (A flat forecast + BUY = direction is determined by something other
        # than Chronos, so we don't claim Chronos "called it right".)
        assert sign_match(0.0, "BUY") is False

    def test_flat_fc_sell_no_match(self):
        assert sign_match(0.0, "SELL") is False

    def test_flat_fc_flat_side_matches(self):
        # Both flat → "match" by convention (neither disagreed with the other).
        assert sign_match(0.0, None) is True


# ── sign_agreement_rate() ───────────────────────────────────────────────
class TestSignAgreementRate:
    def test_perfect(self):
        pairs = [(0.6, "BUY"), (-0.7, "SELL"), (0.5, "BUY")]
        out = sign_agreement_rate(pairs)
        assert out["n"] == 3
        assert out["n_agree"] == 3
        assert out["rate"] == 1.0
        assert out["n_flat_fc"] == 0

    def test_all_wrong(self):
        pairs = [(0.6, "SELL"), (-0.7, "BUY"), (0.5, "SELL")]
        out = sign_agreement_rate(pairs)
        assert out["n"] == 3
        assert out["n_agree"] == 0
        assert out["rate"] == 0.0

    def test_mixed(self):
        # 2 agree (BUY/+, SELL/-), 1 disagree (BUY/+ with -fc)
        pairs = [(0.6, "BUY"), (-0.7, "SELL"), (-0.5, "BUY")]
        out = sign_agreement_rate(pairs)
        assert out["n"] == 3
        assert out["n_agree"] == 2
        assert out["rate"] == pytest.approx(2 / 3, abs=1e-4)

    def test_empty(self):
        out = sign_agreement_rate([])
        assert out["n"] == 0
        assert out["n_agree"] == 0
        assert out["rate"] is None
        assert out["pass"] is None  # not enough data to pass/fail

    def test_flat_fc_counted_separately(self):
        # Flat forecast + BUY does NOT count as "agree", but is still in n.
        # It IS counted in n_flat_fc for transparency.
        pairs = [(0.6, "BUY"), (0.0, "BUY"), (-0.5, "SELL")]
        out = sign_agreement_rate(pairs)
        assert out["n"] == 3
        assert out["n_agree"] == 2  # (0.6, BUY) + (-0.5, SELL)
        assert out["n_flat_fc"] == 1  # (0.0, BUY)
        assert out["rate"] == pytest.approx(2 / 3, abs=1e-4)

    def test_pass_true_when_above_floor(self):
        # rate=0.6 ≥ 0.5 → pass=True
        pairs = [(0.6, "BUY"), (-0.7, "SELL"), (0.5, "BUY"), (-0.4, "SELL")]
        out = sign_agreement_rate(pairs, floor=0.5)
        assert out["rate"] == 1.0
        assert out["pass"] is True

    def test_pass_false_when_below_floor(self):
        # rate=0.0 < 0.5 → pass=False
        pairs = [(0.6, "SELL"), (-0.7, "BUY"), (0.5, "SELL"), (-0.4, "BUY")]
        out = sign_agreement_rate(pairs, floor=0.5)
        assert out["rate"] == 0.0
        assert out["pass"] is False

    def test_pass_none_on_empty_even_with_floor(self):
        out = sign_agreement_rate([], floor=0.5)
        assert out["pass"] is None  # vacuously — needs data to verdict


# ── check_sign_agreement (integration with fake redis) ──────────────────
class FakeRedisForSOAK7:
    """Minimal in-memory redis stub: get(key), keys(pattern), mget(keys)."""

    def __init__(self, mapping: dict[str, str] | None = None):
        self.store: dict[str, str] = dict(mapping or {})

    def get(self, key: str):
        return self.store.get(key)

    def keys(self, pattern: str):
        # Pattern like "soak:forecast:chronos:*" — treat trailing * as glob.
        import fnmatch
        return [k for k in self.store if fnmatch.fnmatchcase(k, pattern)]

    def mget(self, keys):
        return [self.store.get(k) for k in keys]


class TestCheckSignAgreementIntegration:
    def _cfg(self):
        return {
            "redis": {
                "chronos_signal_key_prefix": "soak:forecast:chronos",
                "trade_side_key_prefix": "soak:trade:side",
            },
            "sign_agreement_floor": 0.5,
        }

    def test_empty_redis_passes_with_warn_note(self):
        from scripts.paper_soak_checklist import check_sign_agreement
        r = FakeRedisForSOAK7()
        out = check_sign_agreement(r, self._cfg(), days=5)
        assert out["n_entries"] == 0
        assert out["pass"] is None
        assert "no entries" in out["note"]

    def test_perfect_agreement_passes(self):
        from scripts.paper_soak_checklist import check_sign_agreement
        # 3 entries on 2026-06-29, all forecasts match sides.
        store = {
            "soak:forecast:chronos:2026-06-29:AAPL": "0.6",
            "soak:trade:side:2026-06-29:AAPL":        "BUY",
            "soak:forecast:chronos:2026-06-29:NVDA": "-0.7",
            "soak:trade:side:2026-06-29:NVDA":        "SELL",
            "soak:forecast:chronos:2026-06-29:TSLA": "0.5",
            "soak:trade:side:2026-06-29:TSLA":        "BUY",
        }
        r = FakeRedisForSOAK7(store)
        out = check_sign_agreement(r, self._cfg(), days=5)
        assert out["n_entries"] == 3
        assert out["n_agree"] == 3
        assert out["rate"] == 1.0
        assert out["pass"] is True

    def test_partial_agreement_returns_rate_and_fails_below_floor(self):
        from scripts.paper_soak_checklist import check_sign_agreement
        # 4 entries: 1 agree, 3 disagree → rate=0.25 < 0.5 floor → pass=False
        store = {
            "soak:forecast:chronos:2026-06-29:AAPL":  "0.6",
            "soak:trade:side:2026-06-29:AAPL":         "BUY",   # agree
            "soak:forecast:chronos:2026-06-29:NVDA": "-0.7",
            "soak:trade:side:2026-06-29:NVDA":         "BUY",   # disagree
            "soak:forecast:chronos:2026-06-29:TSLA":  "0.5",
            "soak:trade:side:2026-06-29:TSLA":         "SELL",  # disagree
            "soak:forecast:chronos:2026-06-29:JPM":  "-0.4",
            "soak:trade:side:2026-06-29:JPM":          "BUY",   # disagree
        }
        r = FakeRedisForSOAK7(store)
        out = check_sign_agreement(r, self._cfg(), days=5)
        assert out["n_entries"] == 4
        assert out["n_agree"] == 1
        assert out["rate"] == 0.25
        assert out["pass"] is False

    def test_skips_entries_without_side(self):
        """An entry_attempt without a recorded side = no actual trade = skip."""
        from scripts.paper_soak_checklist import check_sign_agreement
        store = {
            # Forecast only, no trade-side key → was filtered out, skip
            "soak:forecast:chronos:2026-06-29:AAPL": "0.6",
            # Both forecast + side → evaluate
            "soak:forecast:chronos:2026-06-29:NVDA": "-0.7",
            "soak:trade:side:2026-06-29:NVDA":        "SELL",
        }
        r = FakeRedisForSOAK7(store)
        out = check_sign_agreement(r, self._cfg(), days=5)
        assert out["n_entries"] == 1
        assert out["n_agree"] == 1
        assert out["rate"] == 1.0

    def test_skips_unparseable_forecast(self):
        """If forecast value is not a float, skip that pair (do not crash)."""
        from scripts.paper_soak_checklist import check_sign_agreement
        store = {
            "soak:forecast:chronos:2026-06-29:AAPL": "not-a-number",
            "soak:trade:side:2026-06-29:AAPL":        "BUY",
            "soak:forecast:chronos:2026-06-29:NVDA": "-0.7",
            "soak:trade:side:2026-06-29:NVDA":        "SELL",
        }
        r = FakeRedisForSOAK7(store)
        out = check_sign_agreement(r, self._cfg(), days=5)
        assert out["n_entries"] == 1  # only NVDA evaluated
        assert out["n_agree"] == 1