"""Structural tests for the Customizations card in the admin UI.

Phase 4 of docs/spec-openclaw-json-derived-artifact-2026-05-24.md.

These tests don't open a browser — they check that the index.html
contains the expected DOM containers, JS functions, and that the
loadConfigBot orchestrator calls the new render function for both
the null and selected paths. A separate live-browser smoke would be
nice but is out of scope for this phase; structural tests catch the
"someone deleted the card by accident" regression class.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_WEB = Path(__file__).parent.parent / "evolve_admin" / "web"
INDEX_HTML = _WEB / "index.html"
_POD_CONFIG_JS = _WEB / "static" / "js" / "pages" / "pod-config.js"


def _load() -> str:
    # The customizations orchestrator (renderClassifierKeywords +
    # related Config → Bot helpers) moved to pages/pod-config.js
    # (Phase 3x). Concat so the existing string-shape assertions stay
    # valid.
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _POD_CONFIG_JS.read_text(encoding="utf-8")


def test_customizations_card_div_present():
    html = _load()
    assert 'id="botcfg-customizations"' in html, (
        "Customizations card container missing from Settings → Bot subtab. "
        "Spec §4 expects per-bot review surface here."
    )


def test_customizations_card_label_present():
    """The card's title is what operators look for."""
    html = _load()
    assert "Customizations" in html
    # Spec references were deliberately removed from operator-visible tooltips
    # (the spec corpus does not ship in the public repo) — assert the tooltip
    # itself still exists via its distinctive operator-facing copy instead.
    assert "Per-bot deviations from shipped defaults" in html


def test_render_function_defined():
    html = _load()
    # Definition site
    assert re.search(
        r"async function renderBotCustomizationsCard\(botId\)",
        html,
    ), "renderBotCustomizationsCard function missing"


def test_action_handlers_defined():
    """Private dispatch handlers (underscore-prefixed) are invoked by the
    delegated click listener. All four must be defined."""
    html = _load()
    for fn in (
        "_acceptCustomization",
        "_revertCustomization",
        "_saveAnnotation",
        "_toggleAnnotateEditor",
    ):
        assert re.search(rf"\b(async\s+)?function {fn}\(", html), (
            f"Customizations action handler {fn} not defined in index.html"
        )


def test_uses_data_attributes_not_string_interpolation():
    """Regression guard: the renderer must address rows by data-cust-idx
    + a module cache, not by string-interpolating schema keys or bot
    ids into onclick attributes (which is XSS-prone if a key contains
    a quote, and crashes the card if btoa() hits a non-ASCII char)."""
    html = _load()
    # Each action button carries data-cust-action + data-cust-idx.
    assert 'data-cust-action="accept"' in html
    assert 'data-cust-action="revert"' in html
    assert 'data-cust-action="annotate-save"' in html
    # And we no longer call the old public handler names with
    # interpolated keys.
    for forbidden in (
        "acceptCustomization('",
        "revertCustomization('",
        "saveAnnotation('",
    ):
        assert forbidden not in html, (
            f"Customizations card still string-interpolates key into "
            f"onclick: {forbidden!r}. Use data-cust-idx + delegated "
            f"click handler instead."
        )


def test_orchestrator_calls_render_on_bot_select_and_clear():
    """``loadConfigBot`` must call renderBotCustomizationsCard in both the
    null path (no bot selected, clear the card) and the selected path.

    Anchored to ``loadConfigBot`` specifically — there are other
    ``if (!botId)`` clauses in the file (e.g. toast-on-error guards)
    that match a generic regex.
    """
    html = _load()
    # Pull the loadConfigBot body specifically.
    fn_match = re.search(
        r"async function loadConfigBot\(\)\s*\{(.+?)\n\}\n",
        html, re.DOTALL,
    )
    assert fn_match, "loadConfigBot function not found"
    body = fn_match.group(1)
    # Within that body, the null-path returns early.
    null_branch = re.search(
        r"if \(!botId\) \{(.+?)return;\s*\}",
        body, re.DOTALL,
    )
    assert null_branch, "loadConfigBot null-path block not found"
    assert "renderBotCustomizationsCard(null)" in null_branch.group(1), (
        "loadConfigBot null path doesn't clear the Customizations card"
    )
    # And the selected-path (rest of the body after the null branch) calls it.
    after_null = body[null_branch.end():]
    assert "renderBotCustomizationsCard(botId)" in after_null, (
        "loadConfigBot selected path doesn't render the Customizations card"
    )


def test_endpoints_referenced_match_backend():
    """JS calls must target the endpoints the backend registers.

    Post-Phase-4b: the `/set` and `/revert` verbs previously called by the
    cacheRetention + sessionBudget pickers were removed alongside those
    pickers. The Customizations card now uses only accept/annotate/revert
    on existing rows (revert deletes an auto-promoted override). `/set`
    is still a valid backend endpoint — just not called from the UI today.
    """
    html = _load()
    for path in (
        "/api/customizations/",
    ):
        assert path in html, f"{path} URL not referenced in JS"
    # Action verbs each appear at least once in the JS for row-level actions.
    for verb in ("accept", "annotate", "revert"):
        assert re.search(
            rf"/api/customizations/\$\{{encodeURIComponent\(botId\)\}}/{verb}",
            html,
        ), f"JS missing call to /api/customizations/<bot>/{verb}"


# Cache-retention picker tests removed in Phase 4b — the picker JS was
# deleted alongside the TunableKey schema entry. Both knobs (cacheRetention
# + sessionBudget) now live in the canonical Cost & caps card on Settings
# -> Bots; Phase 7 tests will cover that surface.
