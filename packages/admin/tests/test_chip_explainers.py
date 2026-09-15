"""Chip-explainer CI guard.

Per ``docs/principle-alerts-explain-and-remediate.md``, every chip
emitted in ``packages/analyzer/tile_metrics.py`` must carry the
explainer fields the popover renders — ``why`` and ``impact`` at
minimum, with ``remediations`` and ``remediation_note`` where
applicable.

Chips that genuinely cannot ship with an explainer must be listed in
``OPT_OUT`` below with a one-line rationale. The principle's soft rule
allows this ("chip can ship without remediation if doc explains why
none is possible") — but the rationale must be explicit, not implicit.

This test mirrors ``test_alerts_catalog.py::test_body_templates_start_with_approved_emoji``
— static analysis on emitter sites, fails CI on regression, prevents
silent drift back to dead-end chips.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TILE_METRICS = _REPO_ROOT / "packages" / "analyzer" / "tile_metrics.py"


# Chips that intentionally ship without ``why`` / ``impact`` fields.
# Adding an entry requires a one-line rationale — the bar is "we
# thought about it and decided there isn't one," not "we forgot."
#
# Empty as of 2026-06-01 — every chip in tile_metrics.py carries an
# explainer. Future PRs that add chips should either populate the
# fields or add an entry here with rationale.
OPT_OUT: dict[str, str] = {}


# Required explainer fields. ``remediations`` and ``remediation_note``
# are optional per the principle (a chip can ship with only the
# what/why/impact if no concrete remediation exists), so they're not
# in the required set.
REQUIRED_FIELDS = ("why", "impact")


def _extract_chip_dicts(source: str) -> list[ast.Dict]:
    """Return every dict literal passed to a ``chips.append(...)``
    call in the source. AST-based so it survives whitespace / comment
    drift inside the dict bodies.
    """
    tree = ast.parse(source)
    out: list[ast.Dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "append":
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "chips":
            continue
        if len(node.args) != 1:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Dict):
            out.append(arg)
    return out


def _dict_string_keys(d: ast.Dict) -> set[str]:
    return {
        k.value
        for k in d.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _chip_id(d: ast.Dict) -> str | None:
    """Return the chip's ``id`` field if present and a string literal.

    Handles both plain ``"id": "high_correction"`` and the
    ``security_critical`` shape where ``id`` is a constant string.
    Skips dicts without a literal ``id`` field — those are caught
    separately by the structural test below.
    """
    for k, v in zip(d.keys, d.values):
        if not isinstance(k, ast.Constant):
            continue
        if k.value != "id":
            continue
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
        return None
    return None


def test_every_chip_carries_explainer_or_opts_out_explicitly():
    """Every chip emitted in tile_metrics.py must carry ``why`` +
    ``impact``, or be listed in OPT_OUT with rationale. This is the
    forcing function for docs/principle-alerts-explain-and-remediate.md.
    """
    source = _TILE_METRICS.read_text()
    chip_dicts = _extract_chip_dicts(source)
    assert chip_dicts, (
        "no chip dicts found in tile_metrics.py — the test's AST "
        "matcher likely needs updating (chips.append(...) shape "
        "changed?). Otherwise this would silently allow chips to "
        "ship without explainers."
    )

    violations: list[str] = []
    for d in chip_dicts:
        chip_id = _chip_id(d)
        if chip_id is None:
            violations.append(
                f"chip dict at line {d.lineno} has no string-literal "
                "``id`` field"
            )
            continue
        if chip_id in OPT_OUT:
            continue
        keys = _dict_string_keys(d)
        missing = [f for f in REQUIRED_FIELDS if f not in keys]
        if missing:
            violations.append(
                f"chip {chip_id!r} at line {d.lineno} missing field(s): "
                f"{', '.join(missing)}"
            )

    if violations:
        raise AssertionError(
            "Chips missing explainer fields — per "
            "docs/principle-alerts-explain-and-remediate.md every "
            "chip must carry ``why`` and ``impact`` so the popover "
            "can render, or be listed in OPT_OUT below with rationale.\n\n"
            "Violations:\n  - "
            + "\n  - ".join(violations)
            + "\n\nTo fix: add the fields to the chip dict in "
            "tile_metrics.py (see ``high_correction`` for the worked "
            "example), or add the chip_id to OPT_OUT in "
            "tests/test_chip_explainers.py with a one-line rationale."
        )


def test_opt_out_entries_are_not_stale():
    """Every chip_id in OPT_OUT must actually appear as an emitted
    chip in tile_metrics.py. Catches the case where a chip was renamed
    or removed but its OPT_OUT entry was left behind.
    """
    if not OPT_OUT:
        return  # vacuously true
    source = _TILE_METRICS.read_text()
    chip_dicts = _extract_chip_dicts(source)
    emitted_ids = {_chip_id(d) for d in chip_dicts}
    stale = [chip_id for chip_id in OPT_OUT if chip_id not in emitted_ids]
    assert not stale, (
        "OPT_OUT entries that don't match any emitted chip "
        "(probably renamed or removed):\n  - "
        + "\n  - ".join(stale)
        + "\n\nRemove these from OPT_OUT in tests/test_chip_explainers.py."
    )


def test_opt_out_rationales_are_non_empty():
    """Every OPT_OUT entry must have a non-empty rationale string —
    the bar is explicit reasoning, not a blank exemption."""
    blanks = [k for k, v in OPT_OUT.items() if not (v or "").strip()]
    assert not blanks, (
        "OPT_OUT entries with empty rationale:\n  - "
        + "\n  - ".join(blanks)
        + "\n\nAdd a one-line reason explaining why this chip can't "
        "carry an explainer."
    )
