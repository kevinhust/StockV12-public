"""
Root conftest.py — path setup, env bootstrapping, shared fixtures.

Post-migration (2026-07-09): v12/ was promoted to project root.
This conftest mirrors the V11-era structure but now bootstraps the
unified src/ tree (formerly v12/src/) and supports the package layout
stockapp_v12.* that lives under src/.

This file is auto-discovered by pytest (no plugin registration needed).
"""

import sys
from pathlib import Path

# ── Path Setup ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"

# Ensure both project root and src/ are importable
for p in (str(PROJECT_ROOT), str(SRC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# V11 bridge (post-migration): a handful of tests + the V12 bridge layer
# still depend on archived V11 modules (HardRiskGatekeeper, lifecycle_scheduler,
# LongbridgeDataService). Make the canonical archive root importable so
# `from src.core.risk_controller import ...` resolves in tests.
sys.path.insert(0, str(PROJECT_ROOT / "stockapp_v12" / "core"))
from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path
ensure_v11_archive_on_path()

# ── Env Bootstrapping ────────────────────────────────────────────────────
# Optional: unit CI may omit python-dotenv; .env is operator-local only.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass