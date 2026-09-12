"""tests/test_session_surface_primary_block.py — Primary-bot session-surface
blocks (Bundle 2B of the primary-bot-interface spec).

Covers:
  - load_primary_block: role-gated anti-hallucination scaffold (spec §7).
  - load_help_sidebar_block: dynamic TOC from the help index (spec §4.2).
  - build_session_prefix: ordering + omission semantics when the new
    blocks are present / empty.

Failure modes that must NOT crash session start:
  - role unset / member bot (most bots) — both blocks empty, no warning.
  - help_index.json missing (bot deployed before Bundle 2A) — sidebar
    empty, conduct + guide still surface.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def shared_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _write_help_index(shared_dir: Path, docs: list[dict]) -> Path:
    """Write a minimal help_index.json that ``help_index.build.load_index``
    accepts. Shape mirrors what ``evolve-admin help-index build`` emits."""
    payload = {
        "schema_version": 1,
        "built_at": "2026-05-14T18:00:00+00:00",
        "source_root": str(shared_dir),
        "docs": docs,
    }
    path = shared_dir / "help_index.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── load_primary_block ───────────────────────────────────────────────────────


class TestLoadPrimaryBlock:
    def test_primary_role_returns_scaffold(self):
        from session_surface import load_primary_block

        block = load_primary_block("primary")
        assert block
        # Header marker so the bot can distinguish this from POD_CONDUCT
        # and bot_guide blocks.
        assert "[PRIMARY BOT" in block
        # Mentions the three new tool names so the LLM knows what to call.
        assert "evolve_help_search" in block
        assert "submit_intake" in block

    def test_member_role_returns_empty(self):
        from session_surface import load_primary_block

        assert load_primary_block("member") == ""

    def test_unset_role_returns_empty(self):
        """Legacy pods (pre-wizard) have no role set; must not crash and
        must not inject primary-only scaffolding."""
        from session_surface import load_primary_block

        assert load_primary_block(None) == ""
        assert load_primary_block("") == ""

    def test_role_match_is_case_insensitive(self):
        """openclaw.json is hand-edited / wizard-generated and 'Primary'
        is a realistic typo. Without case-insensitive matching the
        primary bot would silently lose its scaffold while looking
        configured correctly to admin.

        Regression for a silent-failure path caught in second-pass
        review of Bundle 2B.
        """
        from session_surface import load_primary_block

        assert load_primary_block("Primary") != ""
        assert load_primary_block("PRIMARY") != ""
        assert load_primary_block("  primary  ") != ""


# ── load_member_block ────────────────────────────────────────────────────────


class TestLoadMemberBlock:
    """The member-bot standing instruction tells non-primary bots to stay
    out of `evo` keyword handling — the plugin owns it. Defense in depth
    for the case where the per-turn stay-quiet directive from
    before_prompt_build doesn't load (plugin missed the keyword, transport
    error, etc.). See the rationale in load_member_block's docstring.
    """

    def test_primary_role_returns_empty(self):
        """Primary bots have their own block (load_primary_block); they
        do not also get the member block."""
        from session_surface import load_member_block

        assert load_member_block("primary") == ""

    def test_member_role_returns_block(self):
        from session_surface import load_member_block

        block = load_member_block("member")
        assert block
        assert "[EVOLVE PLUGIN" in block
        # The two failure modes this block specifically guards against:
        # (1) shelling out to plugin infrastructure scripts, and
        # (2) offering to weaken a security policy to enable a command.
        assert "session_surface.py" in block
        assert "exec.security" in block

    def test_unset_role_returns_block(self):
        """Unset / None role falls into the member case — the standing
        advice is safe for legacy pods too, and missing role would
        otherwise leave them with no scaffold at all."""
        from session_surface import load_member_block

        assert load_member_block(None) != ""
        assert load_member_block("") != ""

    def test_unknown_role_returns_block(self):
        """Any non-primary value (typo, future role names) gets the
        member block. The block's advice is safe across all non-primary
        cases; failing closed (empty) would re-introduce the
        fall-through-to-LLM behavior the block exists to prevent."""
        from session_surface import load_member_block

        assert load_member_block("secondary") != ""
        assert load_member_block("guest") != ""

    def test_primary_match_is_case_insensitive(self):
        """Mirrors TestLoadPrimaryBlock — case-variant `primary` must
        still suppress the member block, otherwise a hand-edited
        openclaw.json with `"Primary"` would deliver both blocks."""
        from session_surface import load_member_block

        assert load_member_block("Primary") == ""
        assert load_member_block("PRIMARY") == ""
        assert load_member_block("  primary  ") == ""


# ── load_help_sidebar_block ──────────────────────────────────────────────────


class TestLoadHelpSidebarBlock:
    def test_member_role_returns_empty(self, shared_dir):
        from session_surface import load_help_sidebar_block

        _write_help_index(shared_dir, [
            {"doc_id": "help/cost", "title": "Cost", "summary": "s",
             "body": "b", "path": "docs/help/cost.md", "sha": "x",
             "size": 1, "category": "help"},
        ])
        assert load_help_sidebar_block("member", shared_dir) == ""

    def test_role_match_is_case_insensitive(self, shared_dir):
        """Same regression as TestLoadPrimaryBlock — case-variant role
        must still render the sidebar so the bot isn't half-configured."""
        from session_surface import load_help_sidebar_block

        _write_help_index(shared_dir, [
            {"doc_id": "help/cost", "title": "Cost",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "help"},
        ])
        assert load_help_sidebar_block("Primary", shared_dir) != ""
        assert load_help_sidebar_block("PRIMARY", shared_dir) != ""

    def test_missing_index_returns_empty(self, shared_dir):
        """Sidebar must silently no-op when the index hasn't been built
        yet (deploy hasn't run since Bundle 2A landed). The primary
        block tells the bot to fall back to `evo help` in this case."""
        from session_surface import load_help_sidebar_block

        assert load_help_sidebar_block("primary", shared_dir) == ""

    def test_empty_index_returns_empty(self, shared_dir):
        from session_surface import load_help_sidebar_block

        _write_help_index(shared_dir, [])
        assert load_help_sidebar_block("primary", shared_dir) == ""

    def test_renders_doc_ids_and_titles(self, shared_dir):
        from session_surface import load_help_sidebar_block

        _write_help_index(shared_dir, [
            {"doc_id": "help/cost", "title": "Cost",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "help"},
            {"doc_id": "operator/overview", "title": "Operator overview",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "operator"},
        ])

        block = load_help_sidebar_block("primary", shared_dir)
        assert "[EVOLVE HELP DOCS" in block
        # doc_id + title both present so the LLM has a stable handle
        # AND a human-readable label.
        assert "help/cost" in block
        assert "Cost" in block
        assert "operator/overview" in block
        # Grouped by category so the bot sees structure.
        assert "Operator docs" in block

    def test_groups_by_category_in_spec_order(self, shared_dir):
        """help → operator → applications, regardless of the index order."""
        from session_surface import load_help_sidebar_block

        _write_help_index(shared_dir, [
            {"doc_id": "applications/manifest-spec", "title": "Manifest",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "applications"},
            {"doc_id": "operator/getting-started", "title": "Getting started",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "operator"},
            {"doc_id": "help/security", "title": "Security",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "help"},
        ])

        block = load_help_sidebar_block("primary", shared_dir)
        help_pos = block.index("help/security")
        op_pos = block.index("operator/getting-started")
        app_pos = block.index("applications/manifest-spec")
        assert help_pos < op_pos < app_pos

    def test_caps_total_entries(self, shared_dir):
        """A blown-up index can't blow the system-prompt budget. Spec
        §4.2 caps the sidebar at ~30 lines / ~400 tokens; the
        implementation caps total docs surfaced."""
        from session_surface import load_help_sidebar_block

        many = [
            {"doc_id": f"help/topic-{i}", "title": f"T{i}",
             "summary": "s", "body": "b", "path": "p", "sha": "x",
             "size": 1, "category": "help"}
            for i in range(60)
        ]
        _write_help_index(shared_dir, many)

        block = load_help_sidebar_block("primary", shared_dir)
        # Block stays bounded; doesn't list all 60.
        assert "topic-59" not in block
        # Sanity: roughly the cap, never the full list.
        assert block.count("- help/") <= 30

    def test_overflow_message_is_per_category(self, shared_dir):
        """When the cap is hit mid-category, the trailing 'and N more'
        message must report the remainder of THIS category, not the
        running total across categories.

        Regression for a bug caught in self-review: the first version
        subtracted the total-shown counter from the current category's
        length, which gave wildly wrong numbers when an earlier
        category had already consumed some of the cap.
        """
        from session_surface import load_help_sidebar_block

        # 10 help docs (consume part of the cap) + 30 operator docs
        # (overflow the rest). With cap=24: help eats 10, operator
        # eats 14, remaining-in-operator = 16.
        docs = []
        for i in range(10):
            docs.append({
                "doc_id": f"help/h-{i}", "title": f"H{i}",
                "summary": "s", "body": "b", "path": "p", "sha": "x",
                "size": 1, "category": "help",
            })
        for i in range(30):
            docs.append({
                "doc_id": f"operator/op-{i}", "title": f"O{i}",
                "summary": "s", "body": "b", "path": "p", "sha": "x",
                "size": 1, "category": "operator",
            })
        _write_help_index(shared_dir, docs)

        block = load_help_sidebar_block("primary", shared_dir)
        # Operator category got truncated; the message reflects the
        # operator-only remainder (16), not the buggy running-total
        # derived value (6) that the prior code emitted.
        assert "…and 16 more" in block


# ── build_session_prefix ─────────────────────────────────────────────────────


class TestBuildSessionPrefixPrimaryOrdering:
    def test_includes_primary_and_sidebar_when_present(self):
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            app_posture_block="POSTURE",
            primary_block="PRIMARY",
            help_sidebar_block="SIDEBAR",
            notifications_block="NOTIF",
        )
        # Order: conduct → guide → posture → primary → sidebar → notif.
        guide_pos = prefix.index("GUIDE")
        posture_pos = prefix.index("POSTURE")
        primary_pos = prefix.index("PRIMARY")
        sidebar_pos = prefix.index("SIDEBAR")
        notif_pos = prefix.index("NOTIF")
        assert guide_pos < posture_pos < primary_pos < sidebar_pos < notif_pos

    def test_omits_primary_when_empty(self):
        """Member bots: primary + sidebar are empty; existing blocks
        still surface in their existing order."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            notifications_block="NOTIF",
            primary_block="",
            help_sidebar_block="",
        )
        assert "GUIDE" in prefix
        assert "NOTIF" in prefix
        # Bot guide still ordered before notifications.
        assert prefix.index("GUIDE") < prefix.index("NOTIF")

    def test_includes_member_block_in_order(self):
        """Member-bot case: the member block lands in the role-scaffold
        slot (after posture, before notifications) — same position the
        primary block occupies for primaries."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            app_posture_block="POSTURE",
            primary_block="",
            help_sidebar_block="",
            member_block="MEMBER",
            notifications_block="NOTIF",
        )
        guide_pos = prefix.index("GUIDE")
        posture_pos = prefix.index("POSTURE")
        member_pos = prefix.index("MEMBER")
        notif_pos = prefix.index("NOTIF")
        assert guide_pos < posture_pos < member_pos < notif_pos

    def test_omits_member_when_empty(self):
        """Primary bot case: member is empty, primary + sidebar are set;
        member's absence must not disturb the other blocks."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix(
            guide_block="GUIDE",
            primary_block="PRIMARY",
            help_sidebar_block="SIDEBAR",
            member_block="",
            notifications_block="NOTIF",
        )
        # Member block must not appear when caller passed empty.
        assert "MEMBER" not in prefix
        assert prefix.index("PRIMARY") < prefix.index("SIDEBAR") < prefix.index("NOTIF")

    def test_backwards_compatible_positional_call(self):
        """Existing callers used positional args (guide, notif, posture).
        Keep them working — the new params are keyword-only in practice
        but we don't want to break the analyzer's old call sites."""
        from session_surface import build_session_prefix

        prefix = build_session_prefix("GUIDE", "NOTIF", "POSTURE")
        assert "GUIDE" in prefix
        assert "POSTURE" in prefix
        assert "NOTIF" in prefix
