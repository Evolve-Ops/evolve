"""tests/test_alerts_grouping.py — UI shape checks for the Alerts page's
"group similar alerts" collapsing behavior.

When the same finding fans out across bots (e.g. "X: backup repo
visibility cannot be verified (no PAT)" on 8 bots), the operator sees
one collapsible group row instead of 8 individual rows. The grouping is
client-side only — the API still returns one Signal per bot.

These tests pin the structural markup + JS helper presence so an
accidental rip-out trips CI rather than silently regressing the page to
the noisy flat list.

Background: PR #1861 (severity recalibration) made a modest dent in noise
by demoting advisory producers to info-tier. The remaining noise on
the page is the per-bot fan-out shape — same-cause-different-bot — that
this grouping addresses.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_ALERTS_EXTENDED_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "alerts-extended.js"
_ALERTS_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _ALERTS_EXTENDED_JS.read_text(encoding="utf-8")


# ── Grouping helpers (JS)


def test_group_key_function_exists():
    """_alGroupKey computes the (producer, type, severity, normalized_title)
    tuple that defines a group. The normalized_title strips the signal's
    bot_id and quoted contents (cron names, plugin names) so the same
    finding shape across bots collapses."""
    html = _html()
    assert "function _alGroupKey(sig)" in html, (
        "_alGroupKey helper missing — grouping cannot compute group ids"
    )
    assert "function _alNormalizeTitle(sig)" in html, (
        "_alNormalizeTitle helper missing — group key needs title normalization"
    )
    # Both normalization rules must be present.
    fn = re.search(r"function _alNormalizeTitle\(sig\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alNormalizeTitle body not located"
    body = fn.group(1)
    assert "sig.bot_id" in body, "title normalization must strip bot_id"
    assert "{bot}" in body, "title normalization must substitute {bot} placeholder"
    assert "'X'" in body or '"X"' in body, (
        "title normalization must collapse quoted contents to 'X'"
    )


def test_group_signals_builder_exists():
    """_alGroupSignals(sigs) returns the list of group objects the
    renderer iterates over. Without this the renderer falls back to a
    flat list and the toggle is meaningless."""
    html = _html()
    assert "function _alGroupSignals(sigs)" in html, (
        "_alGroupSignals builder missing"
    )


def test_group_row_renderer_exists_and_defers_for_singletons():
    """_alGroupRow handles a group of one by delegating to the existing
    _alSignalRow — singleton groups render identically to ungrouped mode
    so the visual contract for non-clustered findings is preserved."""
    html = _html()
    fn = re.search(r"function _alGroupRow\(group\)\s*\{(.+?)\n\}\n",
                   html, re.DOTALL)
    assert fn, "_alGroupRow renderer missing"
    body = fn.group(1)
    assert "_alSignalRow" in body, (
        "_alGroupRow must delegate singleton groups to _alSignalRow so "
        "they render identically to ungrouped mode"
    )
    assert "group.members.length === 1" in body, (
        "singleton early-return guard missing — multi-member groups would "
        "spuriously trigger it or singletons would skip the delegation"
    )


# ── Group bulk actions wire to existing /api/signals/bulk-action


def test_group_bulk_action_uses_existing_endpoint():
    """Groups don't introduce a new server-side concept — the Snooze All /
    Resolve All / Dismiss All buttons fan out to the existing
    /api/signals/bulk-action endpoint with the member ids."""
    html = _html()
    fn = re.search(r"async function _alGroupBulk\(.+?\n\}\n",
                   html, re.DOTALL)
    assert fn, "_alGroupBulk helper missing"
    body = fn.group(0)
    assert "/api/signals/bulk-action" in body, (
        "Group bulk action must call /api/signals/bulk-action — that "
        "endpoint already has the bulk-action logic; the grouping layer "
        "should not duplicate it"
    )
    assert "signal_ids" in body, (
        "bulk-action payload must carry signal_ids — single key the server expects"
    )


def test_group_row_renders_three_bulk_actions():
    """Group header surfaces Snooze all / Dismiss all / Resolve all so
    the operator can dispose 8 fanned-out signals in one click."""
    html = _html()
    fn = re.search(r"function _alGroupRow\(group\)\s*\{(.+?)\n\}\n",
                   html, re.DOTALL)
    assert fn, "_alGroupRow not found"
    body = fn.group(1)
    assert "Snooze all" in body, "Snooze all button missing from group header"
    assert "Dismiss all" in body, "Dismiss all button missing from group header"
    assert "Resolve all" in body, "Resolve all button missing from group header"


# ── Toggle UI + default-on


def test_group_toggle_checkbox_present_on_reports_page():
    """A user-visible toggle lets the operator switch back to the flat
    view when needed. The default is ON because the noise reduction is
    the whole point of the feature."""
    html = _html()
    assert 'id="al-group-similar-reports"' in html, (
        "group-similar toggle checkbox missing from Reports → Alerts page"
    )
    # The checkbox should default to checked (grouping on).
    pattern = (
        r'<input\s+type="checkbox"\s+id="al-group-similar-reports"'
        r'[^>]*\bchecked\b'
    )
    assert re.search(pattern, html), (
        "group-similar toggle must default to checked — feature default is ON; "
        "operator opts out, not in"
    )
    assert "_alToggleGroupSimilar" in html, (
        "toggle handler missing — checkbox onchange would no-op"
    )


def test_group_default_is_on_in_js_state():
    """JS-side default mirrors the checkbox default — _alGroupSimilar
    starts at true so the very first lane render groups even if the
    toggle hasn't been touched."""
    html = _html()
    assert re.search(r"let _alGroupSimilar\s*=\s*true", html), (
        "_alGroupSimilar must default to true to match the checked checkbox"
    )


def test_lane_renderer_branches_on_toggle():
    """The lane renderer picks group-mode vs flat-mode based on the
    toggle state. Without this branch the toggle has no effect on the
    rendered HTML."""
    html = _html()
    # The branch site is right around the existing 'sigs.map(_alSignalRow)' call.
    assert "_alGroupSimilar" in html, "_alGroupSimilar global not referenced"
    branch = re.search(
        r"_alGroupSimilar\s*\?\s*_alGroupSignals\(sigs\)\.map\(_alGroupRow\)",
        html,
    )
    assert branch, (
        "lane renderer must branch on _alGroupSimilar: when true → "
        "_alGroupSignals(sigs).map(_alGroupRow); else → flat _alSignalRow map"
    )


# ── {bot} placeholder stripping for rolled-up display titles


def test_present_template_title_helper_exists():
    """_alPresentTemplateTitle strips the {bot} grouping placeholder + its
    natural connective so rolled-up group headers read cleanly. Without
    this helper, group rows render literal "{bot}" in the title — the
    2026-06-03 Alerts review caught this on six producers (audit,
    security_warden, backup_signal, sysadmin_watchdog,
    install_integrity_monitor, primary_model_floor_advisor)."""
    html = _html()
    assert "function _alPresentTemplateTitle(template)" in html, (
        "_alPresentTemplateTitle helper missing — rolled-up group titles "
        "will leak the literal '{bot}' placeholder to the operator"
    )


def test_group_row_uses_present_template_title_for_display():
    """_alGroupRow must run the template through _alPresentTemplateTitle
    before rendering — otherwise the placeholder leaks even with the
    helper defined."""
    html = _html()
    fn = re.search(r"function _alGroupRow\(group\)\s*\{(.+?)\n\}\n",
                   html, re.DOTALL)
    assert fn, "_alGroupRow not found"
    body = fn.group(1)
    assert "_alPresentTemplateTitle(group.template_title)" in body, (
        "_alGroupRow must read displayTitle through _alPresentTemplateTitle "
        "(not raw group.template_title) so the {bot} placeholder is stripped "
        "for display"
    )


# ── Category tabs (top-level domain filter)


def test_category_tab_strip_present_on_alerts_page():
    """The Alerts page renders a category tab strip so the operator can
    narrow 33+ producers into one of six domain buckets. Without it the
    Alerts page is a flat list dominated by whichever producer is
    noisiest that day."""
    html = _html()
    assert 'id="al-category-tabs"' in html, (
        "category tab strip container missing — operator can't filter "
        "by domain bucket"
    )


def test_category_tabs_canonical_six_keys():
    """The six categories must match the Python-side
    PRODUCER_CATEGORY_DEFAULT keys exactly. Drift here silently breaks
    the tab → server filter handshake."""
    html = _html()
    m = re.search(r"const _AL_CATEGORIES\s*=\s*\[(.+?)\];", html, re.DOTALL)
    assert m, "_AL_CATEGORIES array not found"
    body = m.group(1)
    for key in ("security", "cost", "platform", "integrations",
                "backup", "hygiene"):
        assert f"key: '{key}'" in body or f'key: "{key}"' in body, (
            f"category '{key}' missing from _AL_CATEGORIES — UI tabs out "
            f"of sync with schema.signal.PRODUCER_CATEGORY_DEFAULT"
        )


def test_category_set_handler_triggers_lane_reload():
    """_alSetCategory must trigger a lane re-render so the click feels
    instant. Without the reload, clicking a tab updates state but
    leaves the (now stale) signal list in the DOM."""
    html = _html()
    m = re.search(r"function _alSetCategory\(category\)\s*\{(.+?)\n\}", html, re.DOTALL)
    assert m, "_alSetCategory handler not found"
    body = m.group(1)
    assert "_alActiveCategory" in body, (
        "_alSetCategory must update _alActiveCategory state"
    )
    assert "_alLoadLane('reports')" in body, (
        "_alSetCategory must reload the reports lane so the rendered "
        "rows reflect the new tab selection"
    )


def test_lane_loader_renders_category_tabs_and_applies_filter():
    """_alLoadLane is the single integration site: it must (a) render
    the tabs from the post-severity signal list so counts are accurate,
    and (b) apply the category filter to the rows it renders."""
    html = _html()
    m = re.search(r"async function _alLoadLane\(flavor\)\s*\{(.+?)\n\}\n", html, re.DOTALL)
    assert m, "_alLoadLane not found"
    body = m.group(1)
    assert "_alRenderCategoryTabs(severityFiltered)" in body, (
        "_alLoadLane must render the category tab strip from the post-"
        "severity-filter list — without it, the (N) counts on each tab "
        "are wrong"
    )
    assert "_alFilterByCategory(severityFiltered)" in body, (
        "_alLoadLane must apply the category filter to the final "
        "rendered list — without it, the tab click changes state but "
        "doesn't actually narrow what shows in the DOM"
    )


def test_category_default_state_is_all_tab():
    """_alActiveCategory starts at null, which the renderer interprets
    as 'All'. Operator who never clicks a tab sees the same flat list
    as before the feature landed (no regression)."""
    html = _html()
    assert re.search(r"let _alActiveCategory\s*=\s*null", html), (
        "_alActiveCategory must default to null (no category filter); "
        "any other default would silently hide signals from operators "
        "who don't realize they're on a filtered view"
    )


def test_present_template_title_strips_known_shapes():
    """Pin the strip behavior on the six concrete shapes the 2026-06-03
    review identified. The helper is plain JS; we drive it with a small
    quickjs/node smoke test if available, otherwise we parse the source
    body and assert the substitution rules are present.

    Pure-source assertion: enumerate the substitution rules so a future
    refactor can't silently drop one without surfacing here."""
    html = _html()
    fn = re.search(
        r"function _alPresentTemplateTitle\(template\)\s*\{(.+?)\n\}",
        html, re.DOTALL)
    assert fn, "_alPresentTemplateTitle body not located"
    body = fn.group(1)
    # Shapes the review surfaced + the catchalls. Raw strings carry the
    # literal JS source bytes (a single backslash in `\s`, `\{`, etc).
    required_rules = [
        # "{bot}: foo" → "foo" (security_warden, install_integrity_monitor)
        r"replace(/^\{bot\}:\s*/, '')",
        # "{bot} foo" → "foo" (backup_signal, primary_model_floor_advisor)
        r"replace(/^\{bot\}\s+/, '')",
        # "foo on {bot}" at end → "foo" (sysadmin_watchdog)
        r"replace(/\s+on\s+\{bot\}\s*$/, '')",
        # "X {bot} (rest)" → "X (rest)" — preserves preceding punctuation
        # like the colon in audit's "CRITICAL: {bot} (check_id)" shape
        r"replace(/\s*\{bot\}\s*(?=\()/g, ' ')",
        # catchall remaining {bot}
        r"replace(/\{bot\}/g, '')",
    ]
    # Compare normalized whitespace so multi-line formatting drift doesn't
    # break the test.
    norm_body = re.sub(r"\s+", " ", body)
    for rule in required_rules:
        norm_rule = re.sub(r"\s+", " ", rule)
        assert norm_rule in norm_body, (
            f"_alPresentTemplateTitle is missing the substitution rule:\n"
            f"  {rule}\n"
            f"Dropping this rule re-introduces the {{bot}} leak in group titles."
        )


# ── Incident-key coalescing (producer-declared groups) ──────────────────────


def test_group_key_prefers_incident_key():
    """When a producer declares ``incident_key`` (e.g. the structural
    verifier writes ``app_structural:{bot}:{app}`` so four assertions
    against one manifest collapse), grouping MUST honor it instead of
    falling back to the (producer, type, severity, normalized_title)
    heuristic — otherwise distinct assertion ids on the same manifest
    end up in separate groups.
    """
    html = _html()
    fn = re.search(r"function _alGroupKey\(sig\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alGroupKey body not located"
    body = fn.group(1)
    assert "sig.incident_key" in body, (
        "_alGroupKey must check sig.incident_key first — producers use it "
        "to declare 'these signals are one root cause'. Falling through to "
        "the title heuristic re-splits coalesced findings."
    )
    # The incident-key path must short-circuit the other rules — otherwise
    # two signals with the same incident_key but different titles would
    # land in different groups.
    assert "return `incident:" in body or 'return "incident:' in body or "return 'incident:" in body, (
        "_alGroupKey must RETURN on the incident_key branch, not fall "
        "through into the title-tuple key"
    )


def test_group_row_uses_display_name_for_app_structural_coalesce():
    """When the coalesced group is the app_structural family, the group
    header must read like ``"Health Tracking: 3 structural
    issues"`` — manifest display name + count — not the first
    member's cryptic ``"i-0b78ebf9: app_no_producer_surface"``.

    The display name lives in ``sig.details.display_name`` (the
    audit_poller writes it when ingesting the outbox record).
    """
    html = _html()
    fn = re.search(r"function _alGroupRow\(group\)\s*\{(.+?)\n\}",
                   html, re.DOTALL)
    assert fn, "_alGroupRow body not located"
    body = fn.group(1)
    assert "app_structural:" in body, (
        "_alGroupRow must detect the app_structural incident_key prefix "
        "so per-manifest coalesce groups get a readable headline"
    )
    assert "display_name" in body, (
        "_alGroupRow must read details.display_name for the coalesced "
        "headline; otherwise the cryptic 'i-XXXXXXXX' app_id leaks through"
    )
    assert "structural issue" in body, (
        "_alGroupRow must label the count as 'structural issue(s)' so "
        "the operator reads '3 structural issues' not just '3'"
    )
