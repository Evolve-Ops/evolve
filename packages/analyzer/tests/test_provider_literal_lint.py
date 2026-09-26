"""Tests for tools/provider-literal-lint — the §Addendum 3.B three-homes guard.

Acceptance (spec §B): a planted provider/model literal in covered LOGIC fails
the lint; the three homes (catalog DATA, provider ADAPTERS, tests) pass.

The guard is a standalone script (no .py extension), so it is loaded via
SourceFileLoader. Counting is exercised on synthetic source strings and on the
real covered modules (which must be at their checked-in baseline = 0).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL = _REPO_ROOT / "tools" / "provider-literal-lint"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("provider_literal_lint", str(_TOOL))
    spec = importlib.util.spec_from_loader("provider_literal_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


PLL = _load_tool()


def _count(tmp_path: Path, name: str, src: str) -> int:
    p = tmp_path / name
    p.write_text(src)
    return PLL._count_logic_literals(p)


# ── A planted literal in logic is counted (guard fails) ────────────────────


def test_qualified_model_literal_in_logic_is_counted(tmp_path):
    src = 'def resolve(x):\n    fallback = "anthropic/claude-opus-4-8"\n    return fallback\n'
    assert _count(tmp_path, "a.py", src) == 1


def test_provider_conditional_literal_is_counted(tmp_path):
    src = 'def pick(provider):\n    if provider == "openai":\n        return 1\n    return 0\n'
    assert _count(tmp_path, "b.py", src) == 1


def test_attribute_provider_conditional_is_counted(tmp_path):
    """``model.provider == "anthropic"`` — attribute access — is a provider
    conditional and must count. This form evaded the pre-tightening regex (its
    ``[^\\w.]`` guard excluded the leading ``.``); it's the exact debt the
    band-derived rung refactor removed from suggest_rung_placement/structured."""
    src = 'def f(model):\n    if model.provider == "anthropic":\n        return 1\n    return 0\n'
    assert _count(tmp_path, "b_attr.py", src) == 1


def test_prov_shorthand_conditional_is_counted(tmp_path):
    """The ``prov == "x"`` shorthand (common alias for provider) is a provider
    conditional and must count — it also evaded the pre-tightening regex, which
    only matched ``provider\\w*``."""
    src = 'def f(prov):\n    if prov == "google":\n        return 1\n    elif prov == "xai":\n        return 2\n    return 0\n'
    assert _count(tmp_path, "b_prov.py", src) == 2


def test_provider_substring_variable_is_not_counted(tmp_path):
    """A variable that merely CONTAINS 'prov' mid-word (``improve_provider``,
    ``approved``) is not a provider receiver — no false positive."""
    src = 'approved = "yes"\nimprove_provider_label = "openai"\n'
    assert _count(tmp_path, "b_neg.py", src) == 0


def test_ts_qualified_literal_in_logic_is_counted(tmp_path):
    src = 'function f() {\n  const m = "google/gemini-2.0-flash";\n  return m;\n}\n'
    assert _count(tmp_path, "c.ts", src) == 1


# ── The three homes pass (not counted) ─────────────────────────────────────


def test_comment_literal_is_not_counted(tmp_path):
    src = 'def f():\n    # routes anthropic/claude-haiku-4-5 by default\n    return 1\n'
    assert _count(tmp_path, "d.py", src) == 0


def test_docstring_literal_is_not_counted(tmp_path):
    src = 'def f():\n    """Resolves e.g. anthropic/claude-opus-4-8 -> opus."""\n    return 1\n'
    assert _count(tmp_path, "e.py", src) == 0


def test_allow_region_literal_is_not_counted(tmp_path):
    src = (
        "# provider-literal-allow-begin: catalog DATA\n"
        'CATALOG = {"models": ["anthropic/claude-fable-5", "openai/gpt-4o"]}\n'
        "# provider-literal-allow-end\n"
    )
    assert _count(tmp_path, "f.py", src) == 0


def test_allow_line_literal_is_not_counted(tmp_path):
    src = 'SENTINEL = "evolve/blocked"  # provider-literal-allow: not a real provider\n'
    assert _count(tmp_path, "g.py", src) == 0


def test_ts_block_comment_literal_is_not_counted(tmp_path):
    src = 'const x = 1;\n/* example: anthropic/claude-opus-4-8 */\nconst y = 2;\n'
    assert _count(tmp_path, "h.ts", src) == 0


def test_non_provider_keyword_not_standard_is_not_counted(tmp_path):
    src = 'judge = {"rung": "sonnet-class", "provider": "not-standard"}\n'
    assert _count(tmp_path, "i.py", src) == 0


def test_tuple_assignment_of_layer_names_is_not_counted(tmp_path):
    # The `layer, rung, models = "bot", b_rung, b_models` shape must NOT
    # be mistaken for a provider literal.
    src = 'layer, rung, models = "bot", b_rung, b_models\n'
    assert _count(tmp_path, "j.py", src) == 0


# ── The real covered modules are at baseline (clean) ───────────────────────


def test_all_covered_modules_at_baseline():
    """Every covered module's current literal count must equal its checked-in
    baseline — i.e. the guard passes on the real tree. This is the ratchet
    invariant the CI gate enforces."""
    baseline = PLL._load_baseline()
    for rel in PLL._COVERED:
        p = _REPO_ROOT / rel
        assert p.exists(), f"covered module missing: {rel}"
        n = PLL._count_logic_literals(p)
        allowed = baseline.get(rel, 0)
        assert n <= allowed, (
            f"{rel}: {n} literal(s) exceeds baseline {allowed} — a new provider/"
            f"model literal entered covered logic"
        )
