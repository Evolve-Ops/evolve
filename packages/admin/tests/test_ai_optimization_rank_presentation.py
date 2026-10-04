"""tests/test_ai_optimization_rank_presentation.py — Phase 6b §C.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 3.C — rank
presentation: roles are ordered (Fast → Standard → Power → Max), metered, and
NEVER numbered. The legacy "Tier 0–3" numerals (lower = stronger, judge wedged
at 0) are retired from the AI-Optimization page.

These are source-level assertions on the rendered label maps + render helpers
in ai-optimization.js (the admin SPA has no JS test harness; the established
pattern across packages/admin/tests is to scan the web JS source). They pin:

  1. Zero user-facing "Tier N" / "TIERN" numerals survive in the label maps.
  2. The canonical ascending ladder order Fast → Standard → Power → Max.
  3. Max is a first-class ladder role (own row, in the meter ladder).
  4. Judge is rendered in its OWN section under a divider with NO meter, and
     carries the fixed diversity copy.
  5. The filled-steps rank meter exposes an accessible "rank N of M" label.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_JS = (
    REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
    / "static" / "js" / "pages" / "ai-optimization.js"
)
_INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return _AI_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


# ── 1. No user-facing tier numerals ──────────────────────────────────────────

# Display strings that render a numeral as a *user-facing* tier label. The
# lowercase storage keys (tier0..tier3) are legitimate internal identifiers and
# are NOT matched — only the numeral-bearing DISPLAY forms are retired
# (spec §Addendum3.C.1):
#   - "Tier 0".."Tier 3" (capital T, with or without a space): the old labels.
#   - "TIER0".."TIER3" (all-caps): the old uppercase headings (TIER2 (...)).
_NUMERAL_LABEL_RE = re.compile(
    r"""(?:\bTier\s*[0-3]\b|\bTIER[0-3]\b)""",
)


def _strip_comments(src: str) -> str:
    # Drop // line comments and /* */ blocks so a comment that *describes* the
    # retired numerals (e.g. the legacy "Tier 0–3" note) doesn't trip the scan.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def test_no_user_facing_tier_numerals_in_js(js: str):
    code = _strip_comments(js)
    hits = _NUMERAL_LABEL_RE.findall(code)
    assert hits == [], f"retired tier-numeral display labels still present: {hits}"


def test_no_user_facing_tier_numerals_in_index_html(html: str):
    # The AI-Optimization markup (freshness panel, engine-override picker copy)
    # also must not carry "Tier N" labels. The override picker labels moved to
    # role names in the JS, but the static copy lives here.
    code = _strip_comments(html)
    # Only scan the AI-Optimization region to avoid unrelated pages.
    start = code.find("Model Freshness")
    region = code[start : start + 4000] if start != -1 else code
    hits = _NUMERAL_LABEL_RE.findall(region)
    assert hits == [], f"tier numerals in AI-Optimization markup: {hits}"


def test_legacy_label_maps_retired(js: str):
    # The legacy _AI_TIER_LABELS map and the inline tierNames maps are gone.
    assert "_AI_TIER_LABELS" not in js
    # "Workhorse" / "Grunt" are legacy display names retired in favor of
    # Standard / Fast.
    code = _strip_comments(js)
    assert "Workhorse" not in code, "legacy 'Workhorse' label still rendered"
    assert "Grunt" not in code, "legacy 'Grunt' label still rendered"


# ── 2 & 3. Canonical ascending order + Max is a ladder role ───────────────────


def test_ladder_role_order_is_canonical_ascending(js: str):
    m = re.search(r"_AI_LADDER_ROLES\s*=\s*\[([^\]]*)\]", js)
    assert m, "_AI_LADDER_ROLES not found"
    roles = [r.strip().strip("'\"") for r in m.group(1).split(",") if r.strip()]
    assert roles == ["fast", "standard", "power", "max"], roles


def test_role_labels_cover_all_four_roles(js: str):
    m = re.search(r"_AI_ROLE_LABELS\s*=\s*\{(.*?)\}", js, re.DOTALL)
    assert m, "_AI_ROLE_LABELS not found"
    block = m.group(1)
    for role, label in [
        ("fast", "Fast"), ("standard", "Standard"), ("power", "Power"),
        ("max", "Max"),
    ]:
        assert re.search(rf"{role}\s*:\s*['\"]{label}['\"]", block), (role, label)
    assert "judge" not in block, "the retired judge role must not be labeled"


def test_max_is_a_ladder_role(js: str):
    # Max must be in the metered ladder.
    m = re.search(r"_AI_LADDER_ROLES\s*=\s*\[([^\]]*)\]", js)
    ladder = m.group(1)
    assert "'max'" in ladder or '"max"' in ladder


# ── 4. The judge role is GONE (judge-role collapse §5.4) ──────────────────────


def test_no_judge_render_machinery_remains(js: str):
    # The off-ladder judge card, its diversity copy, and the divider section
    # died with the judge role.
    assert "_aiRenderJudgeCard" not in js
    assert "Off the power ladder" not in js
    assert "judgeCard" not in js
    assert "judgeSection" not in js


# ── 5. Accessible rank meter ──────────────────────────────────────────────────


def test_rank_meter_has_accessible_label(js: str):
    m = re.search(r"function _aiRankMeter\([^)]*\)\s*\{(.*?)\n\}", js, re.DOTALL)
    assert m, "_aiRankMeter not found"
    body = m.group(1)
    assert "aria-label" in body
    assert "rank ${filled} of ${total}" in body
    # Meter colors come from tokens, not hardcoded hex (style-guide §2).
    assert "var(--accent)" in body and "var(--border)" in body
    assert not re.search(r"#[0-9a-fA-F]{6}", body), "meter must use token vars"
