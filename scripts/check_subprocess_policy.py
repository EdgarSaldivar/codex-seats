"""Reject unbounded or shell-mediated subprocess use in production Python.

Tests are deliberately outside this gate: they may use the standard subprocess
APIs to exercise CLI boundaries.  Production synchronous execution has exactly
one implementation, ``codex_seats.bounded_process.run``.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

SYNC_APIS = {
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
    "run",
}
PRODUCTION_TREES = ("lib", "scripts")


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


class PolicyVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []
        self.subprocess_modules: set[str] = set()
        self.sync_functions: set[str] = set()
        self.bounded_modules: set[str] = set()
        self.bounded_functions: set[str] = set()

    def error(self, node: ast.AST, message: str) -> None:
        self.errors.append(f"{self.path}:{node.lineno}: {message}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            if alias.name == "subprocess":
                self.subprocess_modules.add(local)
            if alias.name == "codex_seats.bounded_process":
                if alias.asname:
                    self.bounded_modules.add(alias.asname)
                else:
                    self.bounded_modules.add("codex_seats.bounded_process")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "subprocess":
            for alias in node.names:
                if alias.name == "*":
                    self.error(node, "wildcard subprocess import bypasses the bounded runner")
                elif alias.name in SYNC_APIS:
                    self.sync_functions.add(alias.asname or alias.name)
        if module == "codex_seats":
            for alias in node.names:
                if alias.name == "bounded_process":
                    self.bounded_modules.add(alias.asname or alias.name)
        if module == "codex_seats.bounded_process":
            for alias in node.names:
                if alias.name == "run":
                    self.bounded_functions.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self.sync_functions:
            self.error(node, f"direct subprocess.{node.func.id} is forbidden")
        elif isinstance(node.func, ast.Attribute):
            owner = _dotted_name(node.func.value)
            if owner in self.subprocess_modules and node.func.attr in SYNC_APIS:
                self.error(node, f"direct subprocess.{node.func.attr} is forbidden")

        is_bounded_run = (
            isinstance(node.func, ast.Name)
            and node.func.id in self.bounded_functions
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and _dotted_name(node.func.value) in self.bounded_modules
        )
        if is_bounded_run and not any(kw.arg == "timeout" for kw in node.keywords):
            self.error(node, "bounded_process.run requires an explicit timeout keyword")

        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self.error(node, "shell=True is forbidden")

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"wait", "communicate"}
            and not node.args
            and not node.keywords
        ):
            self.error(node, f"unbounded no-argument {node.func.attr}() is forbidden")
        self.generic_visit(node)


def check_file(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: cannot inspect Python source: {exc}"]
    visitor = PolicyVisitor(path)
    visitor.visit(tree)
    return visitor.errors


def production_python_files(root: Path) -> Iterable[Path]:
    for tree in PRODUCTION_TREES:
        directory = root / tree
        if directory.is_dir():
            yield from sorted(path for path in directory.rglob("*.py") if path.is_file())


def check_root(root: Path) -> list[str]:
    errors: list[str] = []
    for path in production_python_files(root):
        errors.extend(check_file(path))
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_root(root)
    if errors:
        print("subprocess policy failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print("subprocess policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
