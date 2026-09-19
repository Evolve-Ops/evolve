"""Audit: every generator's charter must allowlist every action kind its
module actually emits.

The bug this catches: a generator imports an action class from
``schema.proposal`` and uses it in an ``action=Kind(...)`` constructor,
but the generator's ``charter.yaml`` action_kind_allowed invariant
doesn't list that kind. At runtime, ``arbiter.ingest._check_invariants``
raises ``InvariantViolation`` and the proposal is silently dropped —
no operator-visible signal, no logs the operator routinely reads. The
generator emits proposals into a black hole.

Live example caught by this audit: plugin_curator emitted
``UpdatePluginBaseline`` proposals (for the K=3 pod-wide-rollout
adoption pattern that surfaced when codex + memory-core rolled out
across the test pod in 2026 Q2) but the charter only allowlisted four
other plugin kinds. Every adoption proposal hit the invariant and
disappeared.

This is the safety net: any future generator that adds a new emitted
kind without updating its charter will fail this test at PR time.

Strategy
--------
For each ``packages/analyzer/generators/<id>/charter.yaml``:

  1. Parse the charter; pull the ``action_kind_allowed`` invariant's
     ``allowlist`` (skip the generator if no such invariant exists —
     a generator can legitimately declare ``emits_proposals: false`` or
     run with a different gating strategy).

  2. AST-walk every ``.py`` under the generator directory. Detect uses
     of known action kinds in two ways (whichever fires):

       a. ``action=<Identifier>(...)`` — the canonical Proposal
          constructor pattern.
       b. ``from schema.proposal import <Kind>`` plus a call expression
          ``<Kind>(...)`` somewhere in the module body — covers
          helpers that build the action separately and pass it via a
          variable.

  3. The set of detected emitted kinds must be a subset of the
     allowlist. Otherwise this test fails with a clear diagnostic
     pointing at the specific generator + the specific kinds the
     charter is missing.

Limitations
-----------
The detector is static; it can be fooled by truly indirect dispatch
(e.g., a dict mapping strings to constructor callables). No such
pattern exists in the repo today; if one is introduced the test will
miss the corresponding kind — switch that generator to a generator-
specific dynamic test or add the kind to the allowlist defensively.

The detector also walks ALL files under the generator dir, including
``__init__.py`` and helpers. This is intentional — observation logic
sometimes lives outside ``observe.py``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dep of the analyzer suite
    pytest.skip("PyYAML required for charter audit", allow_module_level=True)


_ANALYZER_ROOT = Path(__file__).resolve().parent.parent
_GEN_ROOT = _ANALYZER_ROOT / "generators"
_SCHEMA_PATH = _ANALYZER_ROOT / "schema" / "proposal.py"


def _known_action_kinds() -> set[str]:
    """Pull every declared action kind from ``schema/proposal.py``.

    Each action dataclass declares ``kind: Literal["Name"] = "Name"``.
    Parsing for that pattern gives us the authoritative set without
    needing to import the module (the test suite shouldn't depend on
    the full schema's transitive imports being loadable).
    """
    text = _SCHEMA_PATH.read_text()
    return {m.group(1) for m in re.finditer(r'kind:\s*Literal\["([A-Za-z0-9_]+)"\]', text)}


_KNOWN_KINDS = _known_action_kinds()


def _charter_allowlist(path: Path) -> set[str] | None:
    """Return the ``action_kind_allowed`` allowlist for a charter, or
    ``None`` if the charter declares no such invariant.

    Returning ``None`` for "no invariant" rather than the empty set lets
    the caller distinguish "this generator is opt-out by design" from
    "this generator forbids everything" (a misconfiguration we'd want
    to flag, though no such case exists today).
    """
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return None
    for inv in data.get("invariants") or []:
        if not isinstance(inv, dict) or inv.get("check_kind") != "action_kind_allowed":
            continue
        params = inv.get("params") or {}
        return {str(a) for a in (params.get("allowlist") or [])}
    return None


def _emitted_kinds(gen_dir: Path) -> set[str]:
    """Discover action kinds the generator's code actually uses.

    Walks every ``.py`` under ``gen_dir``. Combines two detection
    strategies (union of both fires):

      - AST: any ``action=Name(...)`` keyword argument on any call,
        where ``Name`` is a known action kind. Also handles attribute
        access (``module.Kind(...)``).
      - Regex backstop: any name imported from ``schema.proposal`` (or
        ``..schema.proposal``) that appears as ``Name(`` somewhere in
        the module body. Catches the "build action in a helper, pass
        as a variable" pattern that AST keyword inspection misses.

    The two strategies overlap on the common case; we keep both
    because the cost is low and the regex backstop is the one that
    catches the silent-build helpers.
    """
    found: set[str] = set()
    for py in gen_dir.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        source = py.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        imported_kinds: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name in _KNOWN_KINDS:
                        imported_kinds.add(name)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg != "action" or not isinstance(kw.value, ast.Call):
                        continue
                    func = kw.value.func
                    if isinstance(func, ast.Name) and func.id in _KNOWN_KINDS:
                        found.add(func.id)
                    elif isinstance(func, ast.Attribute) and func.attr in _KNOWN_KINDS:
                        found.add(func.attr)

        for name in imported_kinds:
            if re.search(rf"\b{re.escape(name)}\s*\(", source):
                found.add(name)
    return found


def _all_generator_dirs() -> list[Path]:
    return sorted(p.parent for p in _GEN_ROOT.glob("*/charter.yaml"))


def test_action_kinds_extracted_from_schema_are_nonempty():
    """Sanity: if the regex pulled zero kinds, the audit is meaningless."""
    assert _KNOWN_KINDS, (
        "Failed to extract any action kinds from schema/proposal.py — "
        "the ``kind: Literal[...]`` declaration pattern may have "
        "changed; update _known_action_kinds() accordingly."
    )


@pytest.mark.parametrize("gen_dir", _all_generator_dirs(), ids=lambda p: p.name)
def test_charter_allowlist_covers_every_emitted_action_kind(gen_dir: Path):
    """Per-generator: charter allowlist ⊇ emitted kinds.

    Parametrized so a regression points at exactly one generator name in
    the failure header rather than a "N generators broken" blob.
    """
    charter = gen_dir / "charter.yaml"
    allow = _charter_allowlist(charter)
    if allow is None:
        # No allowlist invariant declared. Allowed: the charter may
        # legitimately opt out (e.g., user_profile_inferrer declares
        # emits_proposals: false). The check is moot for this generator.
        return

    emitted = _emitted_kinds(gen_dir)
    missing = emitted - allow

    assert not missing, (
        f"Generator {gen_dir.name!r} emits action kinds that its charter "
        f"action_kind_allowed invariant doesn't allowlist: {sorted(missing)!r}. "
        f"Every such proposal would be silently dropped at L1 ingest "
        f"(arbiter.ingest._check_invariants raises InvariantViolation). "
        f"Fix: add the missing kind(s) to "
        f"{charter.relative_to(_ANALYZER_ROOT.parent.parent)} "
        f"invariants[action_kind_allowed].params.allowlist, then bump the "
        f"charter fingerprint via tools/bump_charter_fingerprints.py."
    )
