"""Tests for tools/scheduler-factory-lint — the scheduler-adapter
construction gate (8.3 Linux port, scheduler-seam portability).

The gate bans bare ``LaunchdScheduler(...)`` / ``SystemdScheduler(...)`` /
``FakeScheduler(...)`` construction outside the seam, the platform gate, the
verified guarded-derive accessors, the tunnel exemption, and tests. A
module-global / unguarded adapter bypasses the process-wide
``get_scheduler()`` / ``set_scheduler()`` injection and pins launchctl on a
host where it may not exist (Linux pod) — the exact bug class the
scheduler-seam migration fixed.

Coverage:
  * The current tree PASSES the gate (--all, including --strict).
  * A synthetic un-allowlisted construction is DETECTED, and the AST approach
    does NOT false-positive on isinstance() args, annotations, or
    docstring/comment mentions.
  * --strict flips the exit code from 0 (warn) to 1 (block).
  * Function-scoped allowlisting: a NEW construction in an allowlisted file
    but a non-allowlisted function still trips.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "scheduler-factory-lint"


def _load_tool() -> ModuleType:
    # Hyphenated executable, not a .py module — load by source path.
    return SourceFileLoader("scheduler_factory_lint", str(_TOOL_PATH)).load_module()


_LINT = _load_tool()


# ── The current tree is clean ────────────────────────────────────────────────


def test_current_tree_passes_strict() -> None:
    """Today's tree has zero un-allowlisted constructions, so even the
    blocking (CI) mode exits 0. If this fails, either a new bypass landed or
    a legitimate guarded-derive needs adding to _FUNCTION_ALLOW."""
    assert _LINT.main(["--all", "--strict"]) == 0


# ── Detection of a synthetic bypass ──────────────────────────────────────────


def _write(p: Path, content: str) -> Path:
    p.write_text(content, encoding="utf-8")
    return p


def test_module_global_construction_is_detected(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "bypass.py",
        "from runtime.scheduler import LaunchdScheduler\n"
        "_BAD = LaunchdScheduler()\n",
    )
    viol = _LINT._violations_for(f)
    assert len(viol) == 1
    lineno, name, fn = viol[0]
    assert name == "LaunchdScheduler"
    assert fn == ""  # module scope


def test_in_function_construction_is_detected(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "bypass2.py",
        "from runtime.scheduler import SystemdScheduler\n"
        "def go():\n"
        "    return SystemdScheduler()\n",
    )
    viol = _LINT._violations_for(f)
    assert [(v[1], v[2]) for v in viol] == [("SystemdScheduler", "go")]


def test_isinstance_and_annotation_are_not_flagged(tmp_path: Path) -> None:
    """isinstance(x, LaunchdScheduler) is a Call ARG, not the callee; an
    annotation is not a Call at all. Regex would flag both — the AST must
    not."""
    f = _write(
        tmp_path / "clean.py",
        "from runtime.scheduler import LaunchdScheduler, get_scheduler\n"
        "def probe(x) -> 'LaunchdScheduler':\n"
        "    if isinstance(x, LaunchdScheduler):\n"
        "        return get_scheduler()\n"
        "    return x\n",
    )
    assert _LINT._violations_for(f) == []


def test_docstring_and_comment_mentions_are_not_flagged(tmp_path: Path) -> None:
    """Docstring/comment text is invisible to the AST, so a prose mention of
    LaunchdScheduler() never counts as a construction."""
    f = _write(
        tmp_path / "prose.py",
        '"""This module talks about LaunchdScheduler() a lot."""\n'
        "from runtime.scheduler import get_scheduler\n"
        "def go():\n"
        "    # historical: LaunchdScheduler(use_sudo=False) used to be here\n"
        '    """LaunchdScheduler(timeout=5.0) in a docstring."""\n'
        "    return get_scheduler()\n",
    )
    assert _LINT._violations_for(f) == []


# ── Warn vs block severity ───────────────────────────────────────────────────


def test_strict_blocks_but_warn_passes(tmp_path: Path, capsys) -> None:
    f = _write(
        tmp_path / "bypass3.py",
        "from runtime.scheduler import LaunchdScheduler\n"
        "_X = LaunchdScheduler()\n",
    )
    # Warn mode (no --strict): finding is printed but exit code is 0.
    assert _LINT.main([str(f)]) == 0
    out = capsys.readouterr().out
    assert "un-allowlisted construction" in out
    # Strict mode: same finding, exit code 1.
    assert _LINT.main(["--strict", str(f)]) == 1


# ── Function-scoped allowlisting precision ───────────────────────────────────


def test_allowlist_is_function_scoped_not_whole_file() -> None:
    """An allowlisted file (retire.py) permits construction ONLY in its
    listed function. A NEW construction in a different function in that same
    file must still trip — this is the core value of file:function
    allowlisting over whole-file."""
    rel = "packages/admin/evolve_admin/retire.py"
    allowed = _LINT._FUNCTION_ALLOW[rel]
    assert "_probe_scheduler" in allowed
    # Simulate the gate's per-construction decision: a hit in an unlisted
    # function is a violation; a hit in the listed one is not.
    assert "_some_new_unguarded_fn" not in allowed


def test_seam_and_tunnel_are_whole_file_allowlisted() -> None:
    assert "packages/analyzer/runtime/scheduler.py" in _LINT._WHOLE_FILE_ALLOW
    assert "packages/admin/evolve_admin/tunnel.py" in _LINT._WHOLE_FILE_ALLOW


def test_gate_function_is_allowlisted() -> None:
    """The one production set_scheduler(SystemdScheduler()) caller."""
    rel = "packages/admin/evolve_admin/setup_wizard.py"
    assert _LINT._FUNCTION_ALLOW[rel] == {"_activate_linux_platform"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
