"""Lock-in: ``primary_model_floor_advisor`` stays retired.

Retired 2026-06-04 — the generator surfaced "lower {bot}'s primary
model to a cheaper tier" proposals whose stated impact ("doesn't
change routed behavior for any specific session class") was false on
every active pod bot post-anchor-rollout. The premise relied on the
"heartbeat-override leak" being the dominant primary consumer; PR
#1737 / PR #1764 closed that leak. The remaining primary consumers
are user-turn / ambiguous / productive sessions — i.e., human chat
— which the PR #1774 revert explicitly chose to keep on Sonnet for
member bots.

The 3 proposals that triggered the retirement (one each for the
Discord team bot, the Slack team bot, and the security-focused bot
on the live pod) were asking to re-enact the PR #1765 regression on
three bots, one bot at a time. Dismissed manually on the pod
2026-06-04.

This file pins the retirement so a future session doesn't reinvent
the same generator under the same name with the same flawed premise.
Re-introducing it requires deliberately removing these tests AND
having a recorded design conversation about how the new version
distinguishes background-routing leakage from human-chat usage.

Full rationale:
internal/decision-retire-primary-model-floor-advisor-2026-06-04.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

_GENERATORS_DIR = _ANALYZER_DIR / "generators"


# ── The generator directory is gone ────────────────────────────────────────


def test_generator_directory_absent():
    """The generator's source directory must not exist. Catches a
    half-retirement where the code stayed but the registry entry
    didn't — leaving a latent path that could be re-wired by accident."""
    gen_dir = _GENERATORS_DIR / "primary_model_floor_advisor"
    assert not gen_dir.exists(), (
        f"primary_model_floor_advisor directory exists at {gen_dir}. "
        f"Retirement requires removing the directory so the import path "
        f"doesn't resolve. See "
        f"internal/decision-retire-primary-model-floor-advisor-2026-06-04.md "
        f"for the rationale and the design-conversation gate for any "
        f"re-introduction."
    )


def test_no_python_module_named_primary_model_floor_advisor():
    """Importability check — the package layout must not expose the
    name as an importable module. A stale __pycache__/ directory or
    a dangling __init__.py would let ``from generators.primary_model_
    floor_advisor.observe import ...`` succeed against bytecode even
    though the source is gone."""
    import importlib
    with pytest.raises(ImportError):
        importlib.import_module(
            "generators.primary_model_floor_advisor.observe",
        )


# ── The runner registry doesn't reference it ──────────────────────────────


def test_context_factories_registry_does_not_reference_floor_advisor():
    """The generator's entry in ``_CONTEXT_FACTORIES`` in
    generator_runner.py must be gone (or commented out with the
    retirement note). The factory function ``_make_primary_model_
    floor_advisor_ctx`` must not be defined either — orphan factories
    just rot."""
    runner_src = (_ANALYZER_DIR / "generator_runner.py").read_text()
    # The registry maps id strings to factory tuples. A literal
    # "primary_model_floor_advisor": (factory, ...) entry means the
    # registry still routes it. Comments / retirement-note mentions of
    # the name are fine.
    code_only = "\n".join(
        ln for ln in runner_src.splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert '"primary_model_floor_advisor":' not in code_only, (
        "generator_runner._CONTEXT_FACTORIES still maps "
        "'primary_model_floor_advisor' to a factory. Remove the entry "
        "(comments are fine; an actual mapping is not). See the "
        "retirement decision doc."
    )
    assert "def _make_primary_model_floor_advisor_ctx" not in code_only, (
        "Orphan factory _make_primary_model_floor_advisor_ctx still "
        "defined in generator_runner.py — remove it; it can never be "
        "reached now that the registry entry is gone."
    )


# ── No downstream chip / tile detector ────────────────────────────────────


def test_cost_opt_tiles_does_not_export_floor_chip_detector():
    """cost_opt_tiles.detect_primary_off_floor_chip — the tile-row
    surface that consumed the generator's pending proposals — must
    also be gone. With no producer, the consumer would just degrade
    to dead code."""
    import cost_opt_tiles
    assert not hasattr(cost_opt_tiles, "detect_primary_off_floor_chip"), (
        "cost_opt_tiles still exports detect_primary_off_floor_chip; "
        "remove it now that its data source (primary_model_floor_advisor) "
        "is retired."
    )


# ── No live proposals on the test pod or in this repo ─────────────────────


def test_no_source_file_imports_primary_model_floor_advisor():
    """Defense against lingering code dependencies. Mentions of the
    generator name in comments / docstrings are fine (and useful — they
    document the history). What's NOT fine is ANY source file still
    importing the generator's package — that's a real functional
    dependency that would fail at import time once the directory is
    gone, OR worse, succeed against stale bytecode in __pycache__.

    Walk packages/analyzer and packages/admin for any
    ``import generators.primary_model_floor_advisor`` or
    ``from generators.primary_model_floor_advisor`` statement. AST
    parsing would be more rigorous but a string match for the import
    keyword pattern is enough — the false-positive rate on real
    Python source is essentially zero.
    """
    repo_root = _ANALYZER_DIR.parent.parent
    search_roots = [
        _ANALYZER_DIR,
        repo_root / "packages" / "admin",
    ]
    import_patterns = (
        "import generators.primary_model_floor_advisor",
        "from generators.primary_model_floor_advisor",
    )
    # This test file itself names the package as a string argument to
    # importlib.import_module() to prove the module can NOT be imported.
    # Exempt it so the scan doesn't trip on its own assertion-helper code.
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    for root in search_roots:
        if not root.exists():
            continue
        for f in root.rglob("*.py"):
            if f.resolve() == self_path:
                continue
            try:
                src = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for pat in import_patterns:
                if pat in src:
                    offenders.append(
                        f"{f.relative_to(repo_root)}: '{pat}'"
                    )
                    break
    assert not offenders, (
        "Source files still import primary_model_floor_advisor:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\nThe generator directory was retired; any lingering "
        "import would fail at runtime. Remove the import (and any "
        "code that depended on it) — see the retirement decision doc."
    )


# ── The retirement decision doc exists ────────────────────────────────────


def test_retirement_decision_doc_exists():
    """A future operator should be able to find the rationale for why
    this generator was retired without spelunking git history. The
    decision doc is the durable artifact."""
    repo_root = _ANALYZER_DIR.parent.parent
    doc_path = (
        repo_root / "internal"
        / "decision-retire-primary-model-floor-advisor-2026-06-04.md"
    )
    assert doc_path.exists(), (
        f"Retirement decision doc missing at {doc_path}. Retiring a "
        "generator without writing down the reasoning is exactly how "
        "the same flawed premise gets re-introduced six months later."
    )
    # Sanity: the doc names the relevant PRs so the chain back to
    # source is intact.
    content = doc_path.read_text()
    for pr in ("#1737", "#1764", "#1774", "#1786"):
        assert pr in content, (
            f"Retirement decision doc must reference PR {pr} so the "
            "chain to the design conversation is preserved."
        )
