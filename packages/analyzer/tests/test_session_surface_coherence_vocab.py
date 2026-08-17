"""Tests for COHERENCE_VOCAB marker-block extraction + footer injection.

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

The vocab doc and its marker block are bot-side scaffolding for in-situ
app-repair conversations. These tests are the equivalent of
test_runtime_notes_clean_scan.py — they guarantee the marker block
loads, the deep-dive footer renders alongside findings, and the
content-scan allowlist accepts our own marker pair.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest


_ANALYZER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


REPO_ROOT = Path(__file__).resolve().parents[3]
COHERENCE_VOCAB_PATH = REPO_ROOT / "docs" / "system" / "COHERENCE_VOCAB.md"


# ── Marker-block extraction ─────────────────────────────────────────────────


def test_coherence_vocab_file_exists():
    assert COHERENCE_VOCAB_PATH.exists(), (
        f"missing source file: {COHERENCE_VOCAB_PATH}"
    )


def test_coherence_vocab_block_loads_with_known_content():
    """Stable substrings from the marker block must reach the loaded constant.

    Catches the regression where the file exists but markers drifted —
    the loader returns "" silently and the bot loses the vocab block.
    """
    from session_surface import COHERENCE_VOCAB as LOADED
    assert LOADED, "COHERENCE_VOCAB loaded empty — markers may have drifted"
    # Stable substrings from the curated vocabulary. If these disappear
    # the doc has been rewritten in a way the bot's prompt scaffolding
    # would also need to know about.
    assert "C-A1" in LOADED
    assert "C1-1" in LOADED
    assert "critical" in LOADED
    assert "observational" in LOADED


def test_coherence_vocab_markers_are_in_allowlist():
    """The session_surface markers must match the content-scan allowlist."""
    from session_surface import _COHERENCE_VOCAB_BEGIN, _COHERENCE_VOCAB_END
    from content_scan.default_patterns import default_catalog
    catalog = default_catalog()
    allowlist = catalog.evolve_markers_allowlist
    assert _COHERENCE_VOCAB_BEGIN in allowlist, (
        f"session_surface marker {_COHERENCE_VOCAB_BEGIN!r} not in "
        "content-scan allowlist; COHERENCE_VOCAB.md would warn on every scan"
    )
    assert _COHERENCE_VOCAB_END in allowlist


def test_coherence_vocab_lands_in_session_prefix():
    """build_session_prefix must surface COHERENCE_VOCAB above the
    app-findings block. The bot needs the vocab in context BEFORE the
    findings reference assertion ids by name."""
    from session_surface import COHERENCE_VOCAB, build_session_prefix
    prefix = build_session_prefix()
    if COHERENCE_VOCAB:
        # The vocab block is conditionally emitted based on file load,
        # so we only assert presence when the constant is non-empty.
        assert "C-A1" in prefix


def test_coherence_vocab_block_is_under_5kb():
    """Spec cap: marker block stays under ~5KB so it doesn't blow the
    session-prefix budget. Operators reading the full doc go to the
    on-disk file."""
    from session_surface import COHERENCE_VOCAB
    assert len(COHERENCE_VOCAB.encode("utf-8")) <= 5120, (
        "COHERENCE_VOCAB marker block grew past 5KB — trim or split."
    )


# ── Footer on load_app_findings_block ──────────────────────────────────────


@pytest.fixture()
def fake_manifests_dir(monkeypatch, tmp_path):
    """Redirect ~/.openclaw/workspace/manifests to a tmp dir.

    load_app_findings_block reads from Path.home() / ".openclaw" /
    "workspace" / "manifests". Patching Path.home is the cleanest stub.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    manifests = fake_home / ".openclaw" / "workspace" / "manifests"
    manifests.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    yield manifests


def _write_manifest(manifests_dir: Path, app_id: str, payload: dict) -> None:
    payload = {"id": app_id, **payload}
    (manifests_dir / f"{app_id}.json").write_text(json.dumps(payload))


def test_app_findings_block_includes_deep_dive_footer(fake_manifests_dir):
    """When there ARE findings, the deep-dive footer is appended.

    The footer is the bot's reference for where the per-finding detail
    lives on disk — without it the bot only sees the truncated summary
    line and can't drill in.
    """
    from session_surface import load_app_findings_block
    _write_manifest(fake_manifests_dir, "journal", {
        "coherence": {
            "findings": [{
                "id": "C-A1", "severity": "critical",
                "assertion": "recurring_behavior_without_trigger",
                "description": "no triggers found for the 6pm summary",
                "evidence": [], "signature": "abc123",
            }],
        },
    })
    block = load_app_findings_block(bot_id="any-bot",
                                     shared_dir=Path("/tmp/unused"))
    assert "journal" in block
    # Footer markers.
    assert "~/.openclaw/workspace/manifests/<app>.json" in block
    assert ".coherence.findings[]" in block
    assert ".reconciliation" in block
    assert ".coherence.last_capability_check" in block
    assert "[COHERENCE VOCAB]" in block


def test_app_findings_block_empty_when_no_findings(fake_manifests_dir):
    """No findings → no block (no footer either, per session-prefix
    rule 6 in §10.9.6)."""
    from session_surface import load_app_findings_block
    # No manifests = no findings.
    block = load_app_findings_block(bot_id="any-bot",
                                     shared_dir=Path("/tmp/unused"))
    assert block == ""


def test_app_findings_block_drops_info_severity(fake_manifests_dir):
    """info-severity findings are noise at session_start; the existing
    filter drops them. Footer therefore not emitted either."""
    from session_surface import load_app_findings_block
    _write_manifest(fake_manifests_dir, "j", {
        "coherence": {
            "findings": [{
                "id": "C-A1", "severity": "info",
                "description": "minor observation", "evidence": [],
            }],
        },
    })
    block = load_app_findings_block(bot_id="any-bot",
                                     shared_dir=Path("/tmp/unused"))
    assert block == ""
