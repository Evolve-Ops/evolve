"""Idempotency tests for the delimited-block AGENTS.md mutators.

Covers FIX 3 (handoff check, still injected) and the session-surface block,
which is now scrubbed rather than injected — see _build_surface_agents_md.
We test the pure builder functions (``_build_handoff_agents_md`` /
``_build_surface_agents_md``) so the tests run without a macOS bot
environment.

Run with: python -m pytest tests/test_inject_block_delimiters.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent / "packages" / "admin"
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.deploy import (  # noqa: E402
    EVOLVE_HANDOFF_BEGIN,
    EVOLVE_HANDOFF_END,
    EVOLVE_SURFACE_BEGIN,
    EVOLVE_SURFACE_END,
    _build_handoff_agents_md,
    _build_surface_agents_md,
)


# ── handoff ──────────────────────────────────────────────────────────────────

class TestHandoffInjectIdempotency:
    def test_empty_input_produces_block(self):
        out = _build_handoff_agents_md("")
        assert EVOLVE_HANDOFF_BEGIN in out
        assert EVOLVE_HANDOFF_END in out
        # Single occurrence
        assert out.count(EVOLVE_HANDOFF_BEGIN) == 1
        assert out.count(EVOLVE_HANDOFF_END) == 1

    def test_idempotent_on_empty(self):
        once = _build_handoff_agents_md("")
        twice = _build_handoff_agents_md(once)
        assert once == twice

    def test_idempotent_on_existing_content(self):
        existing = "# Bot\n\nSome unrelated docs.\n"
        once = _build_handoff_agents_md(existing)
        twice = _build_handoff_agents_md(once)
        assert once == twice
        # Original content preserved
        assert "# Bot" in once
        assert "Some unrelated docs." in once
        # Single block, not duplicated
        assert once.count(EVOLVE_HANDOFF_BEGIN) == 1

    def test_strips_legacy_section(self):
        legacy = (
            "# Bot\n\n"
            "## Session Start Checklist\n"
            "- Check `handoffs/` directory for any pending handoffs\n"
            "- Process each handoff in creation order\n"
        )
        out = _build_handoff_agents_md(legacy)
        # Legacy section header is gone (only the new delimited block has it).
        # Header appears once, inside the delimited block.
        assert out.count("## Session Start Checklist") == 1
        begin_idx = out.find(EVOLVE_HANDOFF_BEGIN)
        end_idx = out.find(EVOLVE_HANDOFF_END)
        header_idx = out.find("## Session Start Checklist")
        assert begin_idx < header_idx < end_idx
        # Original heading preserved
        assert "# Bot" in out

    def test_replaces_old_delimited_block(self):
        old = (
            "# Bot\n\n"
            f"{EVOLVE_HANDOFF_BEGIN}\n"
            "## Session Start Checklist\n"
            "- old instruction text\n"
            f"{EVOLVE_HANDOFF_END}\n"
        )
        out = _build_handoff_agents_md(old)
        assert "old instruction text" not in out
        assert out.count(EVOLVE_HANDOFF_BEGIN) == 1
        # Re-running is still a no-op
        assert _build_handoff_agents_md(out) == out

    def test_legacy_plus_delimited_collapses_to_one(self):
        # Tolerated edge case: both legacy and delimited present (mid-migration).
        mixed = (
            "# Bot\n\n"
            "## Session Start Checklist\n"
            "- legacy text\n\n"
            f"{EVOLVE_HANDOFF_BEGIN}\n"
            "## Session Start Checklist\n"
            "- delimited text\n"
            f"{EVOLVE_HANDOFF_END}\n"
        )
        out = _build_handoff_agents_md(mixed)
        assert "legacy text" not in out
        assert "delimited text" not in out  # both replaced by current instruction
        assert out.count(EVOLVE_HANDOFF_BEGIN) == 1


# ── session surface ──────────────────────────────────────────────────────────
#
# This block USED to inject a "Pending Approval Tasks" instruction whose closing
# line ("no pending tasks — proceed normally") primed the LLM to hallucinate that
# exact phrase back to users when the Evolve plugin handled 'evo' out of band.
# The builder is now a pure scrubber: it strips the block (delimited or legacy)
# and never re-injects. Pending tasks are surfaced via session_surface.py →
# session_start systemAppend (see TurnObserver.handleSessionStart).

class TestSurfaceScrubIdempotency:
    def test_empty_input_stays_empty(self):
        assert _build_surface_agents_md("") == ""

    def test_unrelated_content_preserved_no_block_added(self):
        existing = "# Bot\n\nSome unrelated docs.\n"
        out = _build_surface_agents_md(existing)
        assert EVOLVE_SURFACE_BEGIN not in out
        assert EVOLVE_SURFACE_END not in out
        assert "Pending Approval Tasks" not in out
        assert "# Bot" in out
        assert "Some unrelated docs." in out
        # Idempotent
        assert _build_surface_agents_md(out) == out

    def test_strips_legacy_section(self):
        legacy = (
            "# Bot\n\n"
            "## Pending Approval Tasks\n"
            "Old surface instruction text — proceed normally.\n"
        )
        out = _build_surface_agents_md(legacy)
        assert "Pending Approval Tasks" not in out
        assert "Old surface instruction text" not in out
        assert "proceed normally" not in out
        assert "# Bot" in out
        # Idempotent
        assert _build_surface_agents_md(out) == out

    def test_strips_delimited_block(self):
        old = (
            "# Bot\n\n"
            f"{EVOLVE_SURFACE_BEGIN}\n"
            "## Pending Approval Tasks\n"
            "old surface text — no pending tasks — proceed normally.\n"
            f"{EVOLVE_SURFACE_END}\n"
        )
        out = _build_surface_agents_md(old)
        assert EVOLVE_SURFACE_BEGIN not in out
        assert EVOLVE_SURFACE_END not in out
        assert "Pending Approval Tasks" not in out
        assert "old surface text" not in out
        assert "proceed normally" not in out
        assert "# Bot" in out
        # Idempotent
        assert _build_surface_agents_md(out) == out

    def test_strips_both_legacy_and_delimited(self):
        # Mid-migration edge case: both forms present.
        mixed = (
            "# Bot\n\n"
            "## Pending Approval Tasks\n"
            "legacy body line\n\n"
            f"{EVOLVE_SURFACE_BEGIN}\n"
            "## Pending Approval Tasks\n"
            "delimited body line — no pending tasks — proceed normally.\n"
            f"{EVOLVE_SURFACE_END}\n"
        )
        out = _build_surface_agents_md(mixed)
        assert "Pending Approval Tasks" not in out
        assert "legacy body line" not in out
        assert "delimited body line" not in out
        assert EVOLVE_SURFACE_BEGIN not in out
        assert "# Bot" in out


# ── cross-block coexistence ──────────────────────────────────────────────────

class TestBothBlocksCoexist:
    def test_handoff_kept_surface_scrubbed(self):
        """The handoff block is still injected; the surface block is scrubbed."""
        existing = "# Bot\n"
        with_handoff = _build_handoff_agents_md(existing)
        out = _build_surface_agents_md(with_handoff)
        # Handoff survives, surface is gone, original heading preserved.
        assert out.count(EVOLVE_HANDOFF_BEGIN) == 1
        assert EVOLVE_SURFACE_BEGIN not in out
        assert "# Bot" in out

        # Re-applying both is a fixed point.
        cur = out
        for _ in range(3):
            cur = _build_surface_agents_md(_build_handoff_agents_md(cur))
        next_round = _build_surface_agents_md(_build_handoff_agents_md(cur))
        assert cur == next_round
        assert cur.count(EVOLVE_HANDOFF_BEGIN) == 1
        assert EVOLVE_SURFACE_BEGIN not in cur

    def test_existing_surface_block_alongside_handoff_gets_scrubbed(self):
        """Bots with the legacy injected surface block lose it on re-deploy."""
        existing = (
            "# Bot\n\n"
            f"{EVOLVE_HANDOFF_BEGIN}\nhandoff body\n{EVOLVE_HANDOFF_END}\n\n"
            f"{EVOLVE_SURFACE_BEGIN}\n"
            "## Pending Approval Tasks\n"
            "no pending tasks — proceed normally.\n"
            f"{EVOLVE_SURFACE_END}\n"
        )
        # Apply both passes the way deploy_bot does.
        cur = _build_surface_agents_md(_build_handoff_agents_md(existing))
        assert EVOLVE_SURFACE_BEGIN not in cur
        assert "Pending Approval Tasks" not in cur
        assert "proceed normally" not in cur
        assert cur.count(EVOLVE_HANDOFF_BEGIN) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
