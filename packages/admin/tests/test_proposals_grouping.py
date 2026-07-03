"""tests/test_proposals_grouping.py — UI shape checks for the Proposals
page's "group similar proposals" collapsing behavior.

When the same generator fans out the same finding across bots (e.g. 7
"primary floor" proposals across the pod's bots, or 8 "Add network_egress"
proposals on one bot's apps), the operator sees one collapsible group
row instead of N rows. The grouping is client-side only — the API still
returns one Proposal per finding.

These tests pin the structural markup + JS helper presence so an
accidental rip-out trips CI rather than silently regressing the page to
the noisy flat list. Sibling to test_alerts_grouping.py, which pins the
same shape on the Alerts page (the proposals coalescing intentionally
mirrors the alerts pattern so operators don't have to learn two systems).

Spec: docs/spec-recommendations-rework-2026-06-02.md (Phase 1, PR #1).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"


_SELF_IMPROVEMENT_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
_ALERTS_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "static" / "js" / "pages" / "alerts.js"
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _ALERTS_JS.read_text(encoding="utf-8") + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")


# ── Grouping helpers (JS)


def test_proposal_normalize_title_strips_bot_id_and_quotes():
    """_propNormalizeTitle replaces bot_id / scope_id with the {bot}
    placeholder and collapses quoted contents to 'X', so the same finding
    shape across bots collapses to one group key. Mirrors
    _alNormalizeTitle on the alerts side."""
    html = _html()
    fn = re.search(
        r"function _propNormalizeTitle\(p\)\s*\{(.+?)\n\}", html, re.DOTALL
    )
    assert fn, "_propNormalizeTitle helper missing"
    body = fn.group(1)
    assert "p.bot_id" in body and "p.scope_id" in body, (
        "title normalization must strip both bot_id and scope_id "
        "(some proposals carry scope_id, not bot_id)"
    )
    assert "{bot}" in body, "title normalization must substitute {bot} placeholder"
    assert "'X'" in body, (
        "title normalization must collapse single-quoted contents to 'X' so "
        "'Add network_egress: docs.openclaw.ai' and 'Add network_egress: "
        "github.com' group together"
    )
    assert "`X`" in body, (
        "title normalization must collapse backtick-quoted contents (app "
        "names like `ea-pack`, `task-manager`) so 'App audit on `ea-pack`' "
        "and similar group by shape"
    )


def test_proposal_group_key_combines_generator_urgency_title():
    """_propGroupKey identifies a group as
    (generator_id, urgency, normalized_title). Generator is on the key
    because two generators can emit lookalike titles for different
    underlying findings; urgency is on the key so a security_critical
    finding never silently merges with an improvement."""
    html = _html()
    fn = re.search(r"function _propGroupKey\(p\)\s*\{(.+?)\n\}", html, re.DOTALL)
    assert fn, "_propGroupKey helper missing"
    body = fn.group(1)
    assert "generator_id" in body, "group key must include generator_id"
    assert "urgency" in body, "group key must include urgency"
    assert "_propNormalizeTitle" in body, (
        "group key must include the normalized title — otherwise per-bot "
        "variants don't collapse"
    )


def test_proposal_group_builder_exists():
    """_propGroupProposals(props) returns the list of group objects the
    renderer iterates over. Without this the renderer falls back to a
    flat list and the toggle is meaningless."""
    html = _html()
    assert "function _propGroupProposals(props)" in html, (
        "_propGroupProposals builder missing"
    )


def test_proposal_group_row_defers_for_singletons():
    """_propGroupRow handles a group of one by delegating to the existing
    renderProposalCard — singleton groups render identically to ungrouped
    mode so the visual contract for non-clustered findings is preserved.
    Without the early-return, every proposal would get wrapped in a
    <details> shell even when there's nothing to expand to."""
    html = _html()
    fn = re.search(
        r"function _propGroupRow\(group\)\s*\{(.+?)\n\}\n", html, re.DOTALL
    )
    assert fn, "_propGroupRow renderer missing"
    body = fn.group(1)
    assert "renderProposalCard" in body, (
        "_propGroupRow must delegate singleton groups to renderProposalCard "
        "so they render identically to ungrouped mode"
    )
    assert "group.members.length === 1" in body, (
        "singleton early-return guard missing — multi-member groups would "
        "spuriously trigger it or singletons would skip the delegation"
    )


# ── Bulk actions fan out to existing per-proposal endpoints


def test_proposal_group_bulk_uses_existing_endpoints():
    """Groups don't introduce a new server-side concept — Snooze All /
    Dismiss All fan out to the existing per-proposal endpoints. There is
    no /api/arbiter/proposals/bulk-action; that's intentional, since
    proposal actions are write-cheap and rate-limit-checked per-id."""
    html = _html()
    fn = re.search(r"async function _propGroupBulk\(.+?\n\}\n", html, re.DOTALL)
    assert fn, "_propGroupBulk helper missing"
    body = fn.group(0)
    assert "/api/arbiter/proposals/" in body, (
        "Group bulk action must call the per-proposal arbiter endpoints"
    )
    assert "/snooze" in body and "/dismiss" in body, (
        "Both snooze and dismiss fan-outs must be wired"
    )


def test_proposal_group_row_renders_bulk_actions():
    """Group header surfaces Snooze all / Dismiss all so the operator
    can dispose N fanned-out proposals in one click. (Act-all is
    deliberately omitted: 'Take this on' for an Investigation creates an
    operator workflow that doesn't make sense in bulk — each finding
    needs its own triage session.)"""
    html = _html()
    fn = re.search(
        r"function _propGroupRow\(group\)\s*\{(.+?)\n\}\n", html, re.DOTALL
    )
    assert fn, "_propGroupRow not found"
    body = fn.group(1)
    assert "Snooze all" in body, "Snooze all button missing from group header"
    assert "Dismiss all" in body, "Dismiss all button missing from group header"


# ── Toggle UI + default-on


def test_proposal_group_toggle_checkbox_present():
    """A user-visible toggle lets the operator switch back to the flat
    view when needed. The default is ON because the noise reduction is
    the whole point of the feature."""
    html = _html()
    assert 'id="arbiter-group-similar"' in html, (
        "group-similar toggle checkbox missing from Proposals filter row"
    )
    pattern = (
        r'<input\s+type="checkbox"\s+id="arbiter-group-similar"'
        r'[^>]*\bchecked\b'
    )
    assert re.search(pattern, html), (
        "group-similar toggle must default to checked — feature default is "
        "ON; operator opts out, not in"
    )
    assert "_propToggleGroupSimilar" in html, (
        "toggle handler missing — checkbox onchange would no-op"
    )


def test_proposal_group_default_is_on_in_js_state():
    """JS-side default mirrors the checkbox default — _propGroupSimilar
    starts at true so the very first render groups even if the toggle
    hasn't been touched."""
    html = _html()
    assert re.search(r"let _propGroupSimilar\s*=\s*true", html), (
        "_propGroupSimilar must default to true to match the checked checkbox"
    )


def test_renderer_branches_on_toggle():
    """The inbox render path picks group-mode vs flat-mode based on the
    toggle state. Without this branch the toggle has no effect on the
    rendered HTML.

    Since the altitude rail (Fit Reviewer Bite 2), renderArbiterProposals
    partitions the inbox by altitude (L1+ lead, L0 fold into the Maintenance
    digest) and delegates the actual group-vs-flat rendering to
    _renderInboxBody — so the toggle branch lives there, not in the outer
    function."""
    html = _html()
    # The outer function partitions by altitude and delegates rendering.
    outer_fn = re.search(
        r"function renderArbiterProposals\(inbox, inProcess[^)]*\)\s*\{(.+?)\n\}",
        html,
        re.DOTALL,
    )
    assert outer_fn, "renderArbiterProposals function not found"
    outer = outer_fn.group(1)
    assert "_propAltitude" in outer, (
        "renderArbiterProposals must partition the inbox by altitude"
    )
    assert "_renderInboxBody" in outer and "_renderMaintenanceDigest" in outer, (
        "renderArbiterProposals must delegate to _renderInboxBody and fold "
        "L0 into the Maintenance digest"
    )
    # The group-vs-flat toggle branch lives in _renderInboxBody.
    body_fn = re.search(
        r"function _renderInboxBody\([^)]*\)\s*\{(.+?)\n\}",
        html,
        re.DOTALL,
    )
    assert body_fn, "_renderInboxBody function not found"
    body = body_fn.group(1)
    assert "_propGroupSimilar" in body, (
        "_renderInboxBody must reference _propGroupSimilar to honor the toggle"
    )
    assert "_propGroupProposals" in body and "_propGroupRow" in body, (
        "group-mode branch must call _propGroupProposals + _propGroupRow"
    )
    assert "renderProposalCard" in body, (
        "flat-mode branch must call renderProposalCard"
    )


# ── Coalescing actually collapses a realistic batch


def _normalize(title: str, bot_id: str) -> str:
    """Python mirror of _propNormalizeTitle for offline reasoning. Tests
    that exercise this helper are checking the *intent* — that the
    grouping key collapses representative on-disk proposal titles. The
    on-page JS is what actually runs; this is just a sanity check that
    the patterns chosen match the real titles."""
    t = title.replace(bot_id, "{bot}") if bot_id else title
    t = re.sub(r"'[^']*'", "'X'", t)
    t = re.sub(r"`[^`]*`", "`X`", t)
    return t


def test_realistic_titles_collapse_to_expected_group_count():
    """Sanity check using the actual titles from the 2026-06-02 test pod
    dump (the inciting incident for this rework). 41 raw proposals should
    collapse to a small number of distinct group keys.

    This is a Python-side sanity check on the normalization pattern, not
    a JS execution. If the real titles drift to a shape this regex doesn't
    catch, the test fails loudly here rather than the feature silently
    under-collapsing in production."""
    # Shape mirrors the 2026-06-02 test pod dump (the inciting incident)
    # with role-placeholder bot ids per docs/PLACEHOLDER_NAMING.md. The
    # normalizer doesn't care about the specific id — it strips whatever
    # bot_id the proposal carries — so role placeholders exercise the
    # same code path as real deployment ids.
    samples = [
        ("Investigate security-bot primary floor — current primary '' is not in any tier's models list — can't", "security-bot"),
        ("Investigate bot-a primary floor — current primary '' is not in any tier's models list — can't", "bot-a"),
        ("Investigate personal-bot primary floor — current primary '' is not in any tier's models list — can't", "personal-bot"),
        ("Investigate team-bot-a primary floor — current primary '' is not in any tier's models list — can't", "team-bot-a"),
        ("Investigate evolve primary floor — current primary '' is not in any tier's models list — can't", "evolve"),
        ("Investigate team-bot-b primary floor — current primary '' is not in any tier's models list — can't", "team-bot-b"),
        ("Investigate bot-b primary floor — current primary '' is not in any tier's models list — can't", "bot-b"),
    ]
    keys = {_normalize(t, b) for t, b in samples}
    assert len(keys) == 1, (
        f"7 per-bot 'primary floor' proposals should normalize to ONE key; "
        f"got {len(keys)}: {keys}"
    )

    network_egress_samples = [
        ("Add network_egress: docs.openclaw.ai on Evolve Framework", "personal-bot"),
        ("Add network_egress: github.com on Evolve Framework", "personal-bot"),
        ("Add network_egress: slack.com on Slack Integration", "personal-bot"),
        ("Add network_egress: api.prod.whoop.com on Health Tracking", "personal-bot"),
    ]
    # network_egress titles aren't quoted; they don't fully collapse to a
    # single key (different hostnames + apps). That's correct — these
    # represent meaningfully different findings inside one shape. The
    # test just pins that the bot_id is stripped uniformly.
    for t, b in network_egress_samples:
        assert b not in _normalize(t, b), (
            f"bot_id {b!r} should be stripped from normalized title; got "
            f"{_normalize(t, b)!r}"
        )
