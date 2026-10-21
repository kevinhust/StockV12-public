"""AST-based import boundary checker for stockapp_v12.

Default mode: --warn (exit 0, print violations).
Strict mode: --strict (exit 1 if any violation).
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class Rule:
    name: str
    importer_glob: str  # relative to root, e.g. "src/stockapp_v12/pipeline/**/*.py"
    forbidden_module_prefixes: Tuple[str, ...]


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    module: str
    message: str


DEFAULT_RULES: Tuple[Rule, ...] = (
    Rule(
        # Phase 2–3: pipeline must not depend on services/app, or adapters
        # other than adapters.stores (Redis/memory port impls for Journal /
        # EntryGuard BC redis_client= wrappers). Prefer injecting store=.
        name="pipeline_no_services_adapters_app",
        importer_glob="src/stockapp_v12/pipeline/**/*.py",
        forbidden_module_prefixes=(
            "stockapp_v12.adapters.clock",
            "stockapp_v12.adapters.legacy_v11",
            "stockapp_v12.adapters.marketdata",
            "stockapp_v12.app",
            "stockapp_v12.services",
        ),
    ),
    Rule(
        name="schemas_no_services_adapters_app",
        importer_glob="src/stockapp_v12/schemas/**/*.py",
        forbidden_module_prefixes=(
            "stockapp_v12.adapters",
            "stockapp_v12.app",
            "stockapp_v12.services",
            "stockapp_v12.pipeline",
        ),
    ),
    Rule(
        name="contracts_no_adapters",
        importer_glob="src/stockapp_v12/contracts/**/*.py",
        forbidden_module_prefixes=(
            "stockapp_v12.adapters",
            "stockapp_v12.app",
            "stockapp_v12.services",
        ),
    ),
    Rule(
        name="core_no_adapters_app",
        importer_glob="src/stockapp_v12/core/**/*.py",
        forbidden_module_prefixes=("stockapp_v12.adapters", "stockapp_v12.app"),
    ),
)


def _iter_imports(path: Path) -> Iterable[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def find_violations(
    root: Path, rules: Sequence[Rule] = DEFAULT_RULES
) -> List[Violation]:
    out: List[Violation] = []
    for rule in rules:
        for path in root.glob(rule.importer_glob):
            if "__pycache__" in path.parts:
                continue
            for mod in _iter_imports(path):
                for prefix in rule.forbidden_module_prefixes:
                    if mod == prefix or mod.startswith(prefix + "."):
                        out.append(
                            Violation(
                                rule=rule.name,
                                path=str(path.relative_to(root)),
                                module=mod,
                                message=f"{path}: imports {mod} (rule {rule.name})",
                            )
                        )
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check stockapp_v12 import boundaries (warn by default)."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any violation is found (default: warn-only, exit 0).",
    )
    parser.add_argument(
        "--warn",
        action="store_true",
        default=True,
        help="Warn-only mode (default). Exit 0 even when violations exist.",
    )
    args = parser.parse_args(argv)
    viols = find_violations(args.root.resolve())
    for v in viols:
        print(v.message)
    if args.strict and viols:
        print(f"FAIL: {len(viols)} boundary violation(s)", file=sys.stderr)
        return 1
    print(f"OK ({'strict' if args.strict else 'warn'}): {len(viols)} known issue(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
