"""tests/test_primary_bot_setup_passes_monitors.py — primary-bot baseline gate.

Companion to test_member_bot_setup_passes_monitors. Until #1273 the
``deploy_bot`` workspace-docs install was gated on ``role != "primary"``,
so the primary bot (evo) shipped with only SOUL.md + AGENTS.md from
``packages/analyzer/evolve_bot/`` and was missing MEMORY.md + README.md —
``content_scan_file_disappeared`` fired on every monitor pass.

This test pins the post-fix shape so any regression in
``install_bot_docs`` or the ``role != "primary"`` guard re-introduction
fails CI rather than getting caught by the operator's Alerts page on the
next live deploy.

Three guarantees, each tested:
  1. The role-aware doc plan for "primary" sources SOUL/AGENTS from the
     evolve_bot/ verbatim dir and MEMORY/README from the templates dir
     with placeholder substitution.
  2. Each rendered primary doc is large enough to clear the
     content_scan structural floor (no stub-replacement regression).
  3. plugin_monitor's v2 baseline ships with an empty required_plugins
     list; the primary bot is not gated on any specific plugin being
     enabled (``evolve`` is tautological because its absence shows up as
     heartbeat loss; ``brave`` is one option of several search backends).
     See internal/spec-plugin-posture-rework-2026-06-06.md §2.6.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin.deploy import (  # noqa: E402
    _MEMBER_BOT_DOCS_TEMPLATE_DIR,
    _PRIMARY_BOT_VERBATIM_DOCS_DIR,
    _PRIMARY_BOT_VERBATIM_DOC_FILES,
    _PRIMARY_BOT_TEMPLATED_DOC_FILES,
    _doc_plan_for_role,
    _render_member_bot_doc,
)
from plugins import bootstrap as _plug_bootstrap  # noqa: E402
from content_scan.default_patterns import default_catalog  # noqa: E402
from content_scan import patterns as _scan_patterns  # noqa: E402


# ── 1. Doc plan ────────────────────────────────────────────────────────────────

def test_primary_doc_plan_covers_all_four_files():
    """Primary bots must receive SOUL/AGENTS/MEMORY/README — the same four
    files member bots receive — or content_scan fires
    ``content_scan_file_disappeared`` for the missing ones."""
    plan = _doc_plan_for_role("primary")
    dest_basenames = {fname for _, fname, _ in plan}
    assert dest_basenames == {"SOUL.md", "AGENTS.md", "MEMORY.md", "README.md"}, (
        f"primary plan missing files: {dest_basenames}"
    )


def test_primary_soul_and_agents_sourced_from_evolve_bot_dir():
    """Primary identity (evo) is hand-written and lives in
    packages/analyzer/evolve_bot/. Member templates would clobber it with a
    generic 'You are {bot_id}, a {role} bot' scaffold."""
    plan = {fname: (src, substitute) for src, fname, substitute in _doc_plan_for_role("primary")}
    for fname in _PRIMARY_BOT_VERBATIM_DOC_FILES:
        src, substitute = plan[fname]
        assert src == _PRIMARY_BOT_VERBATIM_DOCS_DIR / fname, (
            f"primary {fname} should source from {_PRIMARY_BOT_VERBATIM_DOCS_DIR}, got {src}"
        )
        assert substitute is False, (
            f"primary {fname} must NOT have placeholders substituted — "
            "evo identity is fixed, not templated"
        )


def test_primary_memory_and_readme_sourced_from_template_dir():
    """MEMORY.md and README.md fall back to the generic member templates —
    nothing primary-specific to write there, and placeholder substitution
    gives the operator a sensible starting point per-bot."""
    plan = {fname: (src, substitute) for src, fname, substitute in _doc_plan_for_role("primary")}
    for fname in _PRIMARY_BOT_TEMPLATED_DOC_FILES:
        src, substitute = plan[fname]
        assert src == _MEMBER_BOT_DOCS_TEMPLATE_DIR / fname, (
            f"primary {fname} should source from {_MEMBER_BOT_DOCS_TEMPLATE_DIR}, got {src}"
        )
        assert substitute is True, (
            f"primary {fname} must have placeholders substituted so {{bot_id}} "
            "/ {{role}} are filled in"
        )


def test_member_doc_plan_unchanged():
    """Defensive: confirm the refactor didn't shift member-bot sources.
    All four files still come from the templates dir with substitution."""
    plan = _doc_plan_for_role("member")
    assert len(plan) == 4
    for src, fname, substitute in plan:
        assert src == _MEMBER_BOT_DOCS_TEMPLATE_DIR / fname
        assert substitute is True


# ── 2. Structural floor ────────────────────────────────────────────────────────

def test_primary_verbatim_docs_clear_structural_floor():
    """SOUL.md and AGENTS.md must both be ≥1500 bytes after byte-for-byte
    copy from evolve_bot/. The structural_emptiness pattern fires below
    that — the April-2026 team_bot_a AGENTS.md truncation incident."""
    floor = 1500
    for fname in _PRIMARY_BOT_VERBATIM_DOC_FILES:
        src = _PRIMARY_BOT_VERBATIM_DOCS_DIR / fname
        assert src.exists(), f"primary doc source missing: {src}"
        size = len(src.read_text().encode("utf-8"))
        assert size >= floor, f"{fname} is {size} bytes (<{floor})"


def test_primary_verbatim_docs_pass_structural_pattern():
    """Run the actual content_scan structural pattern against the verbatim
    primary docs — guards against a future change to min_size_bytes."""
    cat = default_catalog()
    for fname in _PRIMARY_BOT_VERBATIM_DOC_FILES:
        text = (_PRIMARY_BOT_VERBATIM_DOCS_DIR / fname).read_text()
        matches = _scan_patterns.scan_file(
            text=text,
            filename=fname,
            patterns=cat.deny_patterns,
            evolve_markers_allowlist=cat.evolve_markers_allowlist,
            file_size_bytes=len(text.encode("utf-8")),
        )
        structural = [m for m in matches if m.to_dict().get("pattern_kind") == "structural"]
        assert not structural, (
            f"{fname} fires structural pattern: "
            f"{[m.to_dict() for m in structural]}"
        )


def test_primary_templated_docs_render_with_evolve_substitution():
    """MEMORY.md and README.md template render must substitute {bot_id}/{role}
    cleanly for the primary's (bot_id="evolve", role="primary") shape."""
    for fname in _PRIMARY_BOT_TEMPLATED_DOC_FILES:
        rendered = _render_member_bot_doc(
            _MEMBER_BOT_DOCS_TEMPLATE_DIR / fname,
            bot_id="evolve", role="primary",
        )
        assert "{bot_id}" not in rendered, f"{fname} left {{bot_id}} unsubstituted"
        assert "{role}" not in rendered, f"{fname} left {{role}} unsubstituted"
        assert "evolve" in rendered, f"{fname} dropped the bot_id substitution"


# ── 3. Plugin baseline for the primary ─────────────────────────────────────────

def test_primary_required_plugins_is_empty_in_v2():
    """The v2 baseline (internal/spec-plugin-posture-rework-2026-06-06.md
    §2.6) ships with an empty required_plugins list. The primary bot
    inherits the empty list — no plugin is gated as required pod-wide.
    Reasoning: ``evolve``'s absence shows up as heartbeat loss (caught
    by pod_health / watchdog) not as a file-on-disk diff, and ``brave``
    is one of several web-search backends rather than pod policy."""
    baseline = _plug_bootstrap.build_default_baseline()
    assert baseline.required_plugins == [], (
        f"v2 default required_plugins must be empty, got {baseline.required_plugins!r}"
    )
