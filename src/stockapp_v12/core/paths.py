"""Repository layout paths for the V12 package.

Post-migration (2026-07-09): v12/ was promoted to project root.
V12_ROOT is now the project root itself; the REPO_ROOT alias is kept
for backward compatibility with code that imported it.

For src/stockapp_v12/core/paths.py:
  parents[0] = .../core/        (file's directory)
  parents[1] = .../stockapp_v12/
  parents[2] = .../src/
  parents[3] = project root     ← V12_ROOT
"""

from pathlib import Path

# Post-migration: parents[3] resolves to project root (was v12/ pre-migration,
# but `parents[3]` magic was already pointing to v12/ there; the new layout
# gives project root at the same depth, so the number stays).
V12_ROOT = Path(__file__).resolve().parents[3]
# Repo root == V12_ROOT after promotion (kept as alias)
REPO_ROOT = V12_ROOT
V12_CONFIG_DIR = V12_ROOT / "config"
REPO_CONFIG_DIR = V12_ROOT / "config"
