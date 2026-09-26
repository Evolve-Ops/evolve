"""tests/test_proposal_content_phase_a.py — Phase A schema + renderer
for the operator-first proposal content protocol.

Spec: internal/spec-proposal-drafting-protocol-2026-06-04.md.

Phase A ships the eight new optional fields on ``Proposal`` and the
five-section renderer on the Recommendations proposal-detail modal.
Pre-migration proposals (where ``summary`` is None) keep rendering
with the legacy layout — that's the safety valve that lets us migrate
one generator at a time without breaking the queue.

These tests pin:

  1. Schema — fields exist, default to None / "kind", round-trip
     cleanly through to_dict + from_dict (backward-compat: loading a
     proposal dict without the new keys produces None defaults).
  2. Renderer branch — ``renderProposalDetail`` calls the v2 layout
     iff ``summary`` is set; otherwise the legacy layout.
  3. Section presence — the v2 layout includes Summary, Proposed
     Action, Explanation, and Details (collapsed by default).
  4. Action fallback paths — ``cli_command`` renders a copy-button
     code block; ``manual_instruction`` renders a copy-button code
     block; ``manual_path`` renders a sentence.
  5. action_label override — when set on a non-dispatch proposal,
     the action button uses it. Dispatch proposals keep the
     auto-derived dispatch verb (per spec).

Sibling: a separate session is reworking the Reports-page proposal
rendering; this file only pins the Recommendations modal.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web" / "index.html"
_SELF_IMPROVEMENT_JS = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"/ "static" / "js" / "pages" / "self-improvement.js"
ANALYZER = REPO_ROOT / "packages" / "analyzer"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8") + "\n" + _SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Schema — fields, defaults, round-trip
# ─────────────────────────────────────────────────────────────────────────────


def _proposal_class():
    if str(ANALYZER) not in sys.path:
        sys.path.insert(0, str(ANALYZER))
    from schema.proposal import Proposal  # noqa: E402
    return Proposal


def _make_base_dict(**overrides):
    """Minimum-valid proposal dict for from_dict round-trips. Avoids
    importing the harness so this test doesn't transitively need
    the admin testing fixtures."""
    base = {
        "id": "test-1",
        "bot_id": "team-bot-a",
        "generator_id": "cache_ttl_tuner",
        "dimension": "cost",
        "trigger_observations": [],
        "provenance": {"technique": "x", "signals": {}, "confidence": 0.5},
        "problem": "legacy problem text",
        "action": {"kind": "Investigation", "context": "..."},
        "risk_tag": {
            "blast_radius": "bot",
            "reversibility": "manual",
            "touches": [],
        },
    }
    base.update(overrides)
    return base


def test_phase_a_fields_default_to_none_when_not_set():
    """Loading a pre-migration proposal (no Phase A fields in dict)
    leaves them None — backward-compat invariant."""
    Proposal = _proposal_class()
    p = Proposal.from_dict(_make_base_dict())
    for field in (
        "summary", "explanation", "action_label", "manual_path",
        "cli_command", "manual_instruction", "dismiss_signature",
    ):
        assert getattr(p, field) is None, (
            f"Phase A field {field!r} should default to None for "
            f"pre-migration proposals, got {getattr(p, field)!r}"
        )
    # dismiss_scope has a non-None default ("kind") so generators
    # don't need to set it explicitly — only the rare per-instance
    # generators set "instance".
    assert p.dismiss_scope == "kind"


def test_phase_a_fields_round_trip_through_to_dict_from_dict():
    """A proposal with all Phase A fields set survives to_dict +
    from_dict without losing any of them."""
    Proposal = _proposal_class()
    payload = _make_base_dict(
        summary="Your bot is paying to re-read the cache.",
        explanation="Concept: ... Diagnosis: ... Why: ... Trade-off: ...",
        action_label="Switch to long-window caching",
        manual_path="Cost Optimization → Team Bot A",
        cli_command="sudo evolve-admin set-agent-default team-bot-a cacheRetention long",
        manual_instruction="Hi bot, please switch your cache window to long.",
        dismiss_signature="cache_ttl_tuner:team-bot-a:cacheRetention_too_short",
        dismiss_scope="kind",
    )
    p = Proposal.from_dict(payload)
    d = p.to_dict()
    p2 = Proposal.from_dict(d)
    for field, expected in (
        ("summary", payload["summary"]),
        ("explanation", payload["explanation"]),
        ("action_label", payload["action_label"]),
        ("manual_path", payload["manual_path"]),
        ("cli_command", payload["cli_command"]),
        ("manual_instruction", payload["manual_instruction"]),
        ("dismiss_signature", payload["dismiss_signature"]),
        ("dismiss_scope", "kind"),
    ):
        assert getattr(p2, field) == expected, (
            f"Phase A field {field!r} didn't survive round-trip: "
            f"expected {expected!r}, got {getattr(p2, field)!r}"
        )


def test_dismiss_scope_accepts_instance_value():
    """Generators that can't compute a stable per-finding signature
    set dismiss_scope='instance' so the dismiss only suppresses this
    specific proposal id."""
    Proposal = _proposal_class()
    p = Proposal.from_dict(_make_base_dict(
        summary="x",
        dismiss_scope="instance",
    ))
    assert p.dismiss_scope == "instance"


def test_dismiss_scope_defaults_to_kind_when_dict_has_no_value():
    """An older serialization without dismiss_scope loads with the
    default 'kind' value, not None."""
    Proposal = _proposal_class()
    p = Proposal.from_dict(_make_base_dict())  # no dismiss_scope in dict
    assert p.dismiss_scope == "kind"


# ─────────────────────────────────────────────────────────────────────────────
# Renderer — branch on summary presence
# ─────────────────────────────────────────────────────────────────────────────


def test_renderer_branches_on_summary_presence():
    """``renderProposalDetail`` calls the v2 layout when ``summary``
    is set (Phase A migrated) and the legacy layout otherwise.
    Without this branch, the two layouts could clobber each other and
    pre-migration proposals would break."""
    html = _html()
    fn = re.search(
        r"function renderProposalDetail\(p\)\s*\{(.+?)\n}",
        html, re.DOTALL,
    )
    assert fn, "renderProposalDetail function not located"
    body = fn.group(1)
    assert "_isPhaseAContent" in body, (
        "renderProposalDetail must call _isPhaseAContent(p) to decide "
        "which layout to render"
    )
    assert "_renderProposalDetailV2" in body, (
        "renderProposalDetail must call the v2 layout when content "
        "is migrated"
    )
    assert "_renderProposalDetailLegacy" in body, (
        "renderProposalDetail must call the legacy layout as the "
        "fallback path (pre-migration proposals)"
    )


def test_is_phase_a_content_helper_checks_summary_non_empty():
    """The helper is the canonical 'is this migrated?' check. Must
    require a non-empty summary string — None or '' should NOT
    trigger the v2 layout."""
    html = _html()
    fn = re.search(
        r"function _isPhaseAContent\(p\)\s*\{(.+?)\n}",
        html, re.DOTALL,
    )
    assert fn, "_isPhaseAContent helper not located"
    body = fn.group(1)
    assert "p.summary" in body, "helper must check p.summary"
    assert "trim()" in body, (
        "helper must trim() before checking length so an all-whitespace "
        "summary doesn't accidentally trigger the v2 layout"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Renderer — v2 layout sections
# ─────────────────────────────────────────────────────────────────────────────


def test_v2_layout_renders_summary_block():
    """The v2 layout must call _renderProposalSummary so the
    operator-readable Summary appears above the action bar."""
    html = _html()
    fn = re.search(
        r"function _renderProposalDetailV2\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalDetailV2 not located"
    body = fn.group(1)
    assert "_renderProposalSummary(p.summary)" in body, (
        "v2 layout must render Summary via _renderProposalSummary"
    )


def test_v2_layout_renders_explanation_block():
    """The Explanation block is the educational content. Must call
    _renderProposalExplanation so generators that don't set
    explanation render nothing for that section instead of a stale
    heading."""
    html = _html()
    fn = re.search(
        r"function _renderProposalDetailV2\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalDetailV2 not located"
    body = fn.group(1)
    assert "_renderProposalExplanation(p.explanation)" in body, (
        "v2 layout must render Explanation via _renderProposalExplanation"
    )


def test_v2_layout_renders_fallback_paths_block():
    """Tier 2-5 supplemental actions (manual_path, cli_command,
    manual_instruction) render as separate alternatives below the
    primary action button. Must call _renderProposalFallbackPaths."""
    html = _html()
    fn = re.search(
        r"function _renderProposalDetailV2\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalDetailV2 not located"
    body = fn.group(1)
    assert "_renderProposalFallbackPaths(p)" in body, (
        "v2 layout must render fallback paths so cli_command + "
        "manual_instruction + manual_path appear when set"
    )


def test_v2_layout_collapses_technical_details():
    """The Details section must render inside a <details> element
    so it's collapsed by default. Daily operators don't open it;
    debuggers do."""
    html = _html()
    fn = re.search(
        r"function _renderProposalDetailV2\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalDetailV2 not located"
    body = fn.group(1)
    # Look for the <details> element wrapping the technical content +
    # the "Show technical details" summary label.
    assert "<details" in body, (
        "v2 layout must wrap technical details in a <details> element "
        "so it's collapsed by default"
    )
    assert "Show technical details" in body, (
        "<details> must have a 'Show technical details' summary so "
        "the operator knows what they're opening"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Renderer — action label override
# ─────────────────────────────────────────────────────────────────────────────


def test_v2_layout_uses_action_label_for_non_dispatch_proposals():
    """When the proposal has ``action_label`` set AND no
    dispatch_target, the action button uses the override label.
    Dispatch proposals keep the auto-derived verb per spec."""
    html = _html()
    fn = re.search(
        r"function _renderProposalDetailV2\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalDetailV2 not located"
    body = fn.group(1)
    assert "p.action_label" in body, (
        "v2 layout must reference p.action_label so the override "
        "actually applies"
    )
    # The override logic must be gated on isDispatchable being false.
    assert "isDispatchable" in body, (
        "v2 layout must check isDispatchable when picking the verb; "
        "dispatch_target proposals use the auto-derived verb"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Renderer — fallback path helper
# ─────────────────────────────────────────────────────────────────────────────


def test_fallback_paths_helper_renders_each_alternative_when_set():
    """The helper renders manual_path / cli_command /
    manual_instruction as separate blocks when set, and skips them
    cleanly when not."""
    html = _html()
    fn = re.search(
        r"function _renderProposalFallbackPaths\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalFallbackPaths not located"
    body = fn.group(1)
    assert "p.manual_path" in body
    assert "p.cli_command" in body
    assert "p.manual_instruction" in body


def test_fallback_paths_helper_uses_copy_buttons_for_cli_and_instruction():
    """``cli_command`` and ``manual_instruction`` must include copy
    buttons (via navigator.clipboard.writeText) so the operator can
    paste without re-typing. Without these, the fallback paths are
    operator-hostile."""
    html = _html()
    fn = re.search(
        r"function _renderProposalFallbackPaths\(p\)\s*\{(.+?)\n}\n",
        html, re.DOTALL,
    )
    assert fn, "_renderProposalFallbackPaths not located"
    body = fn.group(1)
    # Two copy buttons (one for CLI, one for instruction).
    copy_count = body.count("navigator.clipboard.writeText")
    assert copy_count >= 2, (
        f"expected at least 2 copy-to-clipboard handlers (CLI + "
        f"instruction), found {copy_count}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy renderer untouched — pre-migration proposals still work
# ─────────────────────────────────────────────────────────────────────────────


def test_legacy_renderer_exists_for_pre_migration_proposals():
    """``_renderProposalDetailLegacy`` is the path pre-migration
    proposals still take. Don't accidentally rip it out when
    refactoring v2."""
    html = _html()
    assert "function _renderProposalDetailLegacy(p)" in html, (
        "_renderProposalDetailLegacy must be present so pre-migration "
        "proposals continue to render correctly"
    )
