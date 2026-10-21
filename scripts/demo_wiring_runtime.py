#!/usr/bin/env python3
"""Smoke entrypoint: composition root with fakes (thin wrapper).

Prefer after install:
    stockapp-demo-wiring
Or:
    PYTHONPATH=src:scripts python scripts/demo_wiring_runtime.py
"""

from __future__ import annotations

from stockapp_v12.app.cli import demo_wiring

if __name__ == "__main__":
    demo_wiring()
