"""Tests for the ``handrolled-collapse-caret`` rule in ``tools/ui-style-lint``.

Style-guide §9.13: an expand/collapse affordance must use the canonical
``.expand-icon`` SVG chevron, never a raw Unicode triangle. The block-tier
``unicode-expand-triangle`` rule had two blind spots that let a hand-rolled
caret on a ``<span>``/``<button>``/``<div>`` ship clean — the bot-tile caret
the operator caught in
[#3244](https://github.com/evolve-ops/evolve/pull/3244): its glyph set is only
``▸▾▼`` (so ``▴``/``▵``/``▶``/``▲`` are never matched) and it skips any line
containing the word ``caret``.

The ``handrolled-collapse-caret`` sibling rule closes that gap: it fires on a
raw triangle caret carried by an element whose CLASS names it a
caret/collapse/disclosure control, and only then — so the §9.13 KEEP
categories (menu carets, CTA go-arrows, trend badges) never match. These
tests pin both halves: the gap is caught, and the KEEP categories stay quiet.

See docs/ui-audit-expand-glyph-2026-06-24.md.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LINT_PATH = _REPO_ROOT / "tools" / "ui-style-lint"


def _load_lint():
    loader = importlib.machinery.SourceFileLoader("ui_style_lint", str(_LINT_PATH))
    spec = importlib.util.spec_from_loader("ui_style_lint", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_LINT = _load_lint()


# ── The gap: hand-rolled carets the block rule misses MUST be flagged ─────────
@pytest.mark.parametrize(
    "line",
    [
        # The exact #3244 bot-tile shape — a ▾ on a caret-classed span.
        '<span class="home-rail-tile-caret">▾</span>',
        # Up-caret ▴ — outside the block rule's ▸▾▼ glyph set entirely.
        '<button class="home-rail-tile-collapse">hide ▴</button>',
        # ▶ on a *-arrow disclosure span (the settings "Advanced" pre-fix shape).
        '<span class="module-advanced-arrow">▶</span> Advanced',
        # A generic disclosure chevron div.
        '<div class="card-collapse-chevron">▸</div>',
    ],
)
def test_handrolled_caret_flagged(line: str):
    assert _LINT._line_has_handrolled_caret(line), (
        "a raw caret glyph on a caret/collapse-classed element must be flagged "
        "(§9.13 .expand-icon)"
    )


# ── KEEP categories (§9.13 hard-exclusion list) MUST NOT be flagged ──────────
@pytest.mark.parametrize(
    "line",
    [
        # Already canonical.
        '<span class="expand-icon" aria-hidden="true"><svg>...</svg></span>',
        # Menu / dropdown caret — caret class but a haspopup trigger.
        '<span class="caret" aria-haspopup="true"> ▾</span>',
        # Menu caret with no caret class (Snooze ▾ bulk-action).
        '<button class="btn al-bulk-action">Snooze all ▾</button>',
        # CTA go-arrow.
        '<button class="btn btn-primary">Continue ▸</button>',
        # ▶ play/CTA action button.
        '<button class="btn">▶ Set up</button>',
        # Trend badge.
        '<span class="pod-trend-up">▲ new</span>',
        # Score-breakdown popover trigger (floating panel, not inline collapse).
        '<span class="caret">score ▾</span> <span class="score-popover">',
        # A caret-classed element with no glyph at all.
        '<span class="home-rail-tile-caret">x</span>',
    ],
)
def test_keep_categories_not_flagged(line: str):
    assert not _LINT._line_has_handrolled_caret(line), (
        "menu carets, CTA arrows, trend badges and canonical .expand-icon are "
        "§9.13 KEEP — they must not be flagged"
    )


# ── Live-tree invariant: no net-new hand-rolled carets above the baseline ────
def test_live_tree_has_no_baseline_violations():
    """The shrink-only gate the --strict CI run enforces: the SPA may not gain
    a hand-rolled caret site above tools/handrolled-caret-baseline.txt."""
    violations, _cur, _base = _LINT._check_handrolled_caret_baseline()
    assert not violations, (
        "new hand-rolled caret(s) above baseline — convert to .expand-icon "
        f"(§9.13) or ratchet the baseline: {violations}"
    )


# ── Regression guard: the converted settings "Advanced" disclosure ───────────
def test_settings_advanced_disclosure_is_canonical():
    """settings.js's Advanced <details> uses the .expand-icon chevron, not the
    pre-#3246-era raw ▶ in .module-advanced-arrow."""
    src = (
        _REPO_ROOT
        / "packages/admin/evolve_admin/web/static/js/pages/settings.js"
    ).read_text(encoding="utf-8")
    assert "module-advanced-summary" in src, "Advanced disclosure summary missing"
    # The summary line must carry the canonical chevron and no raw triangle.
    summary_line = next(
        l for l in src.splitlines() if "module-advanced-summary" in l
    )
    assert "expand-icon" in summary_line, (
        "Advanced disclosure must use .expand-icon (§9.13)"
    )
    assert not any(g in summary_line for g in "▴▵▾▿▸▹▲△▽▶◀"), (
        "no raw triangle glyph may remain on the Advanced disclosure summary"
    )
    assert "module-advanced-arrow" not in src, (
        "the dead .module-advanced-arrow hook should be gone"
    )
