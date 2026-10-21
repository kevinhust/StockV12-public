"""Application composition root package (wiring / DI)."""

from stockapp_v12.app.wiring import Runtime, Settings, build_runtime

__all__ = ["Runtime", "Settings", "build_runtime"]
