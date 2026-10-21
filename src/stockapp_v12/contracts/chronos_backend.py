"""ChronosBackend — passive Protocol for V12 forecast backends.

This is the authoritative seam between V12 (consumer) and any prediction
backend (provider). Today the only impl is ``services.chronos_service.ChronosService``
in repo-root ``src/`` — reached via sys.path. Post-launch, an in-tree impl
under ``v12/backends/`` will satisfy this Protocol and the sys.path import
becomes optional (injected adapter pattern).

Invariant: this Protocol is duck-typed. There are NO isinstance checks
in V12 production code, NO abstract-method enforcement at runtime. The
Protocol exists to *document the seam*, not to gate it.

Stability guarantee (Design § 3.1): ``detect`` and ``detect_batch`` signatures
are frozen for v12.x. New optional kwargs (e.g. ``temperature: float = 1.0``)
may be added via PEP 3107 keyword-only addition; **positional-arg changes
are a major bump.**
"""

from __future__ import annotations

from typing import Any, Dict, Protocol

import pandas as pd


class ChronosBackend(Protocol):
    """Backend producing per-symbol prediction signals from daily OHLCV bars.

    The detection payload schema (``signals[i]['meta']``) is documented in
    ``services.forecast_service.forecast_daily`` — keys consumed downstream:
    ``current_price``, ``forecast_price``, ``forecast_q10``, ``forecast_q90``,
    ``horizon``.

    Implementations MUST:
      - Return ``[]`` (empty list) on soft failure, never raise, for ``detect``.
      - Return ``{}`` (empty dict) for missing symbols in ``detect_batch``;
        raise only on a hard backend load failure (caller already catches).
      - Be pickle-safe if the caller serializes the backend (NUC shadow harness
        re-imports per cron run, but future parallel evaluation may pickle).
    """

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        """Run inference on a single symbol's daily-bar DataFrame."""
        ...

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, list[dict[str, Any]]]:
        """Run inference on a batch of symbols in one backend call (preferred)."""
        ...


__all__ = ["ChronosBackend"]