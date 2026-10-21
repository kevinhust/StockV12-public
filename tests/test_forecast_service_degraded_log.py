"""TS-degraded: ForecastService must log WARN when falling back to degraded output.

Council follow-up (2026-06-25) — morning journal showed entry_window cron at
08:57:18 firing SR-01 with `expected_return 0.0000 < 0.01`, which is only
possible via the degraded path (forecast_mean == current_price). The fix is
to make every degraded fallback log a WARN so silent no-trades are visible.
"""
import logging
import pandas as pd

from stockapp_v12.services.forecast_service import ForecastService


def _df():
    return pd.DataFrame({"close": [100.0, 101.0, 102.0]})


def test_degraded_output_logs_warning_when_called_directly(caplog):
    """`_degraded_output` invoked directly must log WARN with symbol + reason."""
    svc = ForecastService(backend=None)  # force backend None
    with caplog.at_level(logging.WARNING, logger="stockapp_v12.services.forecast_service"):
        out = svc._degraded_output("AAPL", _df(), as_of=None, model_id="chronos-2-zero")
    assert out is not None
    assert out.degraded is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("AAPL" in m and "degraded" in m.lower() for m in msgs), \
        f"expected WARN mentioning AAPL + degraded; got {msgs}"


def test_forecast_daily_logs_warning_when_backend_missing(caplog):
    """`forecast_daily` must WARN when backend resolves to None."""
    svc = ForecastService(backend=None)
    with caplog.at_level(logging.WARNING, logger="stockapp_v12.services.forecast_service"):
        out = svc.forecast_daily("NVDA", _df())
    assert out is not None
    assert out.degraded is True
    assert out.expected_return_pct == 0.0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NVDA" in m for m in msgs)


def test_forecast_daily_batch_logs_warning_when_backend_missing(caplog):
    """`forecast_daily_batch` must WARN for each symbol when backend is None."""
    svc = ForecastService(backend=None)
    frames = {"AAPL": _df(), "NVDA": _df(), "TSLA": _df()}
    with caplog.at_level(logging.WARNING, logger="stockapp_v12.services.forecast_service"):
        out = svc.forecast_daily_batch(frames)
    assert all(o is not None and o.degraded for o in out.values())
    msgs = [r.getMessage() for r in caplog.records]
    for sym in frames:
        assert any(sym in m for m in msgs), f"missing WARN for {sym}; got {msgs}"


def test_forecast_daily_batch_logs_warning_when_detect_returns_empty(caplog):
    """Per-symbol WARN when backend.detect_batch returns empty for a symbol."""

    class _StubBackend:
        model_id = "stub"

        def detect(self, df):
            return []

        def detect_batch(self, frames):
            return {sym: [] for sym in frames}

    svc = ForecastService(backend=_StubBackend())
    with caplog.at_level(logging.WARNING, logger="stockapp_v12.services.forecast_service"):
        out = svc.forecast_daily_batch({"AAPL": _df(), "NVDA": _df()})
    assert out["AAPL"].degraded is True
    assert out["NVDA"].degraded is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("AAPL" in m for m in msgs)
    assert any("NVDA" in m for m in msgs)


def test_real_forecast_does_not_log_degraded_warning(caplog):
    """Sanity: real backend output must NOT trigger degraded WARN."""

    class _RealBackend:
        model_id = "stub-real"

        def detect(self, df):
            return [{
                "meta": {
                    "current_price": float(df.iloc[-1]["close"]),
                    "forecast_price": float(df.iloc[-1]["close"]) * 1.05,
                    "forecast_q10": float(df.iloc[-1]["close"]) * 0.99,
                    "forecast_q90": float(df.iloc[-1]["close"]) * 1.10,
                    "horizon": 10,
                }
            }]

        def detect_batch(self, frames):
            return {sym: self.detect(df) for sym, df in frames.items()}

    svc = ForecastService(backend=_RealBackend())
    with caplog.at_level(logging.WARNING, logger="stockapp_v12.services.forecast_service"):
        out = svc.forecast_daily("AAPL", _df())
    assert out.degraded is False
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("degraded" in m.lower() for m in warn_msgs), \
        f"unexpected degraded WARN on real path: {warn_msgs}"