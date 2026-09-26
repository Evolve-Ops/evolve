"""tests/test_session_surface_app_posture.py — Session-surface injection of
the app_posture document.

Spec: internal/spec-manifest-reflex.md §"App posture review (PR4)". The
session_surface hook reads {shared_dir}/app_posture/<bot>.md and
injects it into the bot's systemAppend at session_start.

Failure modes that must NOT crash session start:
  - Missing doc (bot has never had a posture review run)
  - Empty doc (review ran but found nothing)
  - Unreadable file (permissions slip)
"""

from __future__ import annotations

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


def _write_doc(shared_dir: Path, bot_id: str, content: str) -> Path:
    apdir = shared_dir / "app_posture"
    apdir.mkdir(parents=True, exist_ok=True)
    p = apdir / f"{bot_id}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ── load_app_posture_block ───────────────────────────────────────────────────


class TestLoadAppPostureBlock:
    def test_missing_doc_returns_empty(self, shared_dir):
        from session_surface import load_app_posture_block
        assert load_app_posture_block("admin_bot", shared_dir) == ""

    def test_empty_doc_returns_empty(self, shared_dir):
        from session_surface import load_app_posture_block
        _write_doc(shared_dir, "admin_bot", "")
        assert load_app_posture_block("admin_bot", shared_dir) == ""

    def test_returns_labeled_block_when_doc_present(self, shared_dir):
        from session_surface import load_app_posture_block
        _write_doc(shared_dir, "admin_bot", "# App posture — admin_bot\n\nstuff")
        block = load_app_posture_block("admin_bot", shared_dir)
        # Has the labeled header so the bot can distinguish posture from
        # bot_guide and POD_CONDUCT.
        assert "[APP POSTURE" in block
        # Echoes the doc content.
        assert "# App posture — admin_bot" in block
        assert "stuff" in block

    def test_no_bot_id_returns_empty(self, shared_dir):
        from session_surface import load_app_posture_block
        assert load_app_posture_block(None, shared_dir) == ""
        assert load_app_posture_block("", shared_dir) == ""

    def test_truncates_oversized_docs(self, shared_dir):
        """Posture docs are capped at ~3KB injected so they don't blow
        the system-prompt budget."""
        from session_surface import load_app_posture_block
        # Build a doc that's well over 3KB.
        big = "# App posture — admin_bot\n\n"
        for i in range(200):
            big += f"## Section {i}\n\nlots of content here ............................\n\n"
        _write_doc(shared_dir, "admin_bot", big)

        block = load_app_posture_block("admin_bot", shared_dir)
        # Block has a header (~400 chars) plus capped doc (~3KB) plus
        # truncation note.
        assert len(block) < 5000
        assert "truncated for systemAppend" in block


# ── build_session_prefix ─────────────────────────────────────────────────────


class TestBuildSessionPrefix:
    def test_includes_app_posture_when_present(self):
        from session_surface import build_session_prefix
        prefix = build_session_prefix(
            guide_block="GUIDE",
            notifications_block="NOTIF",
            app_posture_block="POSTURE",
        )
        # Order: conduct → guide → posture → notifications.
        guide_pos = prefix.index("GUIDE")
        posture_pos = prefix.index("POSTURE")
        notif_pos = prefix.index("NOTIF")
        assert guide_pos < posture_pos < notif_pos

    def test_omits_app_posture_when_empty(self):
        from session_surface import build_session_prefix
        prefix = build_session_prefix(
            guide_block="GUIDE",
            notifications_block="NOTIF",
            app_posture_block="",
        )
        assert "POSTURE" not in prefix
        assert "GUIDE" in prefix
        assert "NOTIF" in prefix

    def test_works_without_optional_blocks(self):
        """All optional blocks empty — pod conduct still surfaces."""
        from session_surface import build_session_prefix, POD_CONDUCT_SUMMARY
        prefix = build_session_prefix()
        assert POD_CONDUCT_SUMMARY in prefix
