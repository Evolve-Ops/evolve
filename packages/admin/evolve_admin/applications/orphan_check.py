"""orphan_check.py — AST-based orphan-function detector for forge.

Spec: docs/spec-forge-side-effects-2026-06-02.md §13.4. PR 6 of that spec
adds this as a critic-cycle check: a top-level function with zero call
sites is flagged so the bot LLM either wires it up or removes it.

The 2026-06-02 audit on personal-bot ea-pack found that ``ea_config()``
was defined in morning_brief.py and evening_sweep.py but never called —
the LLM intended to honor "Configurable timing via bot config" by
scaffolding a config loader, then forgot to wire it up. Three findings
(dead_code + two missing_functionality) all traced to that one root
cause. This check catches it at forge time, before approval.

Pure Python — no LLM, no network. Operates over a snapshot of generated
files and returns a list of ``OrphanFinding`` dicts.

Exclusions (in order of precedence):

  1. **Files under ``tests/``** — every function in a test file is
     expected to be called by the test runner via discovery, not by
     name from other functions.
  2. **Functions whose name starts with ``test_`` or ``_test_``** —
     same reason as (1) for tests defined outside ``tests/``.
  3. **Functions named ``main``** — conventional entry point, often
     invoked via ``if __name__ == "__main__":`` whose call site doesn't
     show up as a Name node in the call-graph walk.
  4. **Decorated entry points** — any function carrying a decorator from
     the documented entry-point list (``@app.route``, ``@app.post``,
     ``@click.command``, ``@click.group``, ``@pytest.fixture``,
     ``@app.cli.command``, ``@app.before_request``, ``@app.errorhandler``,
     etc.). The decorator registers the function with a framework that
     dispatches by other means.
  5. **Functions whose name starts with ``_``** — module-private
     conventionally; the LLM uses these as helpers. If genuinely
     orphan, ruff/pyright will catch it later; we don't want to add
     noise to forge here.
  6. **Functions referenced by ``__all__``** — exported intentionally,
     even if not called inside the module.

The detector walks AST nodes (not text) so:
  - Imports inside functions, ``functools.partial(fn)``, decorator
    references, ``getattr(module, "fn")`` chains — all count as calls
    where the AST can see the name.
  - Indirect dispatch (``fn = mapping[user_input]``) does NOT count;
    callers should add the function to ``__all__`` or refactor.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Decorator names (last-attribute or bare-name match) that register a
# function as an entry point via some framework. A function decorated
# with any of these is NEVER orphan even if no AST call site references
# it. Match is case-sensitive on the last path component, e.g.
# ``app.post`` matches because ``"post"`` is in the set.
_ENTRY_POINT_DECORATORS = frozenset({
    # Flask / Quart / etc.
    "route", "get", "post", "put", "delete", "patch",
    "before_request", "after_request", "errorhandler",
    "teardown_appcontext", "before_first_request",
    "context_processor", "template_filter", "template_global",
    # Click
    "command", "group", "pass_context", "pass_obj",
    # pytest
    "fixture", "parametrize",
    # FastAPI
    "websocket",
    # Generic CLI / entry-point conventions
    "main", "entrypoint", "entry_point",
    # asyncio
    "coroutine",
})

# Conventional entry-point function names. Bare ``main`` is the most
# common one but bots may also use ``cli`` or ``run`` as top-level
# entry points.
_ENTRY_POINT_NAMES = frozenset({"main", "cli", "run", "app"})


@dataclass
class OrphanFinding:
    """One orphan function the LLM should either wire up or remove."""
    file: str            # path relative to workspace
    function: str        # function name
    line: int            # 1-based line number of the def
    reason: str          # one-line human-readable diagnosis
    severity: str = "minor"  # critic-level severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "function": self.function,
            "line": self.line,
            "reason": self.reason,
            "severity": self.severity,
        }


def find_orphans(
    workspace: Path,
    *,
    files: list[str] | None = None,
) -> list[OrphanFinding]:
    """Scan ``workspace`` for top-level Python functions with no call sites.

    Walks every ``.py`` file under workspace (or only ``files`` when
    provided — typically ``manifest.files[*].path`` filtered to ``.py``).
    Returns a list of ``OrphanFinding`` instances; empty list when every
    function has at least one call site.

    Parameters
    ----------
    workspace
        Root directory to walk. Paths in findings are relative to it.
    files
        Optional explicit list of paths (workspace-relative). When
        omitted, every ``.py`` file under ``workspace`` is scanned.

    Returns
    -------
    list[OrphanFinding]
        One entry per orphan function. Empty when nothing is flagged.
    """
    py_files = _collect_python_files(workspace, files)
    if not py_files:
        return []

    # Pass 1: collect every top-level function def per file.
    defs_by_file: dict[Path, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    all_names_called: set[str] = set()
    all_exports: set[str] = set()

    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        defs_by_file[path] = _top_level_function_defs(tree)
        all_names_called.update(_collect_called_names(tree))
        all_exports.update(_collect_dunder_all(tree))

    # Pass 2: flag every def whose name isn't in the called-names set or
    # exports, applying the exclusion rules.
    findings: list[OrphanFinding] = []
    for path, defs in defs_by_file.items():
        rel_path = _safe_relative(path, workspace)
        if _is_test_file(rel_path):
            continue
        for fn in defs:
            if _is_excluded(fn, all_exports):
                continue
            if fn.name in all_names_called:
                continue
            findings.append(OrphanFinding(
                file=rel_path,
                function=fn.name,
                line=fn.lineno,
                reason=(
                    f"top-level function {fn.name!r} has no call sites; "
                    f"wire it up or remove it before forge approval"
                ),
            ))
    return findings


# ── Helpers ─────────────────────────────────────────────────────────────────


def _collect_python_files(
    workspace: Path, files: list[str] | None,
) -> list[Path]:
    if files is not None:
        out: list[Path] = []
        for rel in files:
            if not rel or not rel.endswith(".py"):
                continue
            p = workspace / rel.lstrip("/")
            if p.is_file():
                out.append(p)
        return out
    try:
        return [p for p in workspace.rglob("*.py")
                if p.is_file()
                and ".venv" not in p.parts
                and "__pycache__" not in p.parts
                and "site-packages" not in p.parts]
    except (OSError, PermissionError):
        return []


def _top_level_function_defs(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Direct module-level defs only. Nested defs and class methods are
    excluded — the LLM uses nested functions as closures / methods are
    framework-dispatched (and method-orphan detection is a different
    problem with much higher false-positive rate)."""
    return [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _collect_called_names(tree: ast.Module) -> set[str]:
    """Every bare name that appears as a Call().func or as a Name() node
    referenced from anywhere in the AST.

    Includes:
      - direct calls (``foo()``)
      - attribute lookups (``module.foo()`` → ``foo`` and ``module``)
      - bare references (``handler = my_fn``; ``functools.partial(fn)``)
      - decorator names (``@my_decorator``)
      - imports (``from . import foo`` → ``foo`` exported as a name)

    This is intentionally permissive — better to miss a real orphan than
    to surface a false positive on a function that's wired up via a less
    common pattern.
    """
    names: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            names.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            names.add(node.attr)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
                if alias.asname:
                    names.add(alias.asname)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            # Don't count the def's OWN name as a call site (that'd
            # make every function self-claiming). Visit decorators
            # and body separately.
            for dec in node.decorator_list:
                self.visit(dec)
            for sub in node.body:
                self.visit(sub)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

    _Visitor().visit(tree)
    # NB: we do NOT discard top-level def names here. A function being
    # called from inside another function's body IS a legitimate call
    # site. Self-recursion (``def foo(): foo()``) is technically a way
    # to dodge orphan detection, but it's rare in forge-emitted code
    # and the false-negative cost is acceptable.
    return names


def _collect_dunder_all(tree: ast.Module) -> set[str]:
    """Names listed in ``__all__`` at module level — treated as called."""
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            out.add(elt.value)
    return out


def _is_test_file(rel_path: str) -> bool:
    """Files under tests/ or named test_*.py — runner-discovered, never orphan."""
    parts = Path(rel_path).parts
    if "tests" in parts or "test" in parts:
        return True
    name = Path(rel_path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _is_excluded(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, exports: set[str],
) -> bool:
    """Per the spec §13.4 exclusion list."""
    name = fn.name
    if name in exports:
        return True
    if name.startswith("_"):
        # Module-private convention; out of scope for forge.
        return True
    if name.startswith("test_") or name.startswith("_test_"):
        return True
    if name in _ENTRY_POINT_NAMES:
        return True
    # Decorated entry points
    for dec in fn.decorator_list:
        if _decorator_is_entry_point(dec):
            return True
    return False


def _decorator_is_entry_point(dec: ast.AST) -> bool:
    """Walk a decorator node and decide if it registers ``fn`` as an
    entry point with a framework that dispatches by other means."""
    # `@route(...)` → Call → Name
    # `@app.route(...)` → Call → Attribute → Name(app), attr='route'
    # `@app.route` (no call) → Attribute → ...
    # `@route` → Name
    target = dec
    if isinstance(target, ast.Call):
        target = target.func
    if isinstance(target, ast.Attribute):
        return target.attr in _ENTRY_POINT_DECORATORS
    if isinstance(target, ast.Name):
        return target.id in _ENTRY_POINT_DECORATORS
    return False


def _safe_relative(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace).as_posix())
    except ValueError:
        return str(path.as_posix())
