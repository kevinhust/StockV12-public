# tests/test_compare_shadow_models.py
from scripts.compare_shadow_models import pairwise_deltas


def test_pairwise_deltas_basic():
    report = {
        "windows": {"full": None, "10d": 10},
        "models": {
            "phase2_v2": {
                "windows": {
                    "full": {
                        "pnl_pct": 10.0,
                        "hit_rate": 0.5,
                        "win_rate": 0.5,
                        "mase_mean": 2.0,
                        "n_trades": 20,
                        "sufficient": True,
                        "rankable": True,
                    },
                    "10d": {
                        "pnl_pct": 1.0,
                        "hit_rate": 0.4,
                        "win_rate": 0.4,
                        "mase_mean": 2.1,
                        "n_trades": 10,
                        "sufficient": False,
                        "rankable": False,
                    },
                }
            },
            "phase2_wf": {
                "windows": {
                    "full": {
                        "pnl_pct": 12.0,
                        "hit_rate": 0.55,
                        "win_rate": 0.52,
                        "mase_mean": 1.9,
                        "n_trades": 22,
                        "sufficient": True,
                        "rankable": True,
                    },
                    "10d": {
                        "pnl_pct": 2.0,
                        "hit_rate": 0.45,
                        "win_rate": 0.45,
                        "mase_mean": 2.0,
                        "n_trades": 12,
                        "sufficient": False,
                        "rankable": False,
                    },
                }
            },
        },
    }
    d = pairwise_deltas(report, "phase2_v2", "phase2_wf")
    assert d["full"]["delta_pnl_pct"] == 2.0
    assert d["full"]["delta_hit_rate"] == 0.05
    assert d["full"]["delta_mase"] == -0.1
    assert d["full"]["both_rankable"] is True
    assert d["10d"]["both_rankable"] is False
