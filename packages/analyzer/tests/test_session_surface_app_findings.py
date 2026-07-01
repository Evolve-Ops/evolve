"""tests/test_session_surface_app_findings.py — session-surface injection
of the app-findings block (coherence + reconciliation drift).

Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

The bot reads its own manifests at
``~/.openclaw/workspace/manifests/<app>.json`` (where the Tier 2 runner
writes ``coherence.findings[]`` + ``reconciliation``) and builds a
short block surfaced in the session_start systemAppend.

Failure modes that must NOT crash session start:
  - Missing manifests dir
  - Unreadable manifest file
  - Malformed JSON
  - admin-tree unavailable (FindingForBlock import fails)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ANALYZER_DIR))


@pytest.fixture()
def fake_home(tmp_path):
    """Stub Path.home() so the test doesn't read the real user's
    manifests."""
    fake = tmp_path / "fake-home"
    fake.mkdir()
    with patch.object(Path, "home", return_value=fake):
        yield fake


def _write_manifest(home: Path, manifest_id: str, body: dict) -> Path:
    mdir = home / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    body.setdefault("id", manifest_id)
    p = mdir / f"{manifest_id}.json"
    p.write_text(json.dumps(body))
    return p


# ── No manifests dir → empty block ───────────────────────────────────────

def test_no_manifests_dir_returns_empty(fake_home, tmp_path):
    from session_surface import load_app_findings_block
    out = load_app_findings_block("any-bot", tmp_path)
    assert out == ""


# ── Empty manifests → empty block ────────────────────────────────────────

def test_no_findings_returns_empty(fake_home, tmp_path):
    from session_surface import load_app_findings_block
    _write_manifest(fake_home, "app1", {"coherence": {"findings": []}})
    out = load_app_findings_block("any-bot", tmp_path)
    assert out == ""


# ── Pass A finding surfaces ──────────────────────────────────────────────

def test_pass_a_critical_finding_surfaces(fake_home, tmp_path):
    from session_surface import load_app_findings_block
    _write_manifest(fake_home, "briefing", {
        "id": "briefing",
        "coherence": {"findings": [{
            "id": "C-A1", "severity": "critical",
            "assertion": "recurring_behavior_without_trigger",
            "description": "manifest claims daily but no triggers",
            "evidence": [],
            "signature": "abc123",
        }]},
    })
    out = load_app_findings_block("any-bot", tmp_path)
    assert "briefing" in out
    assert "CRITICAL" in out
    assert "claims daily but no triggers" in out
    # Repair hint included for critical findings.
    assert "evo app-repair briefing" in out


# ── Pass C1 finding surfaces (uses check_id+message) ─────────────────────

def test_pass_c1_finding_surfaces(fake_home, tmp_path):
    from session_surface import load_app_findings_block
    _write_manifest(fake_home, "x", {
        "coherence": {"findings": [{
            "check_id": "C1-1", "severity": "major",
            "message": "action declares output via 'telegram' but the script...",
            "action_id": "send",
        }]},
    })
    out = load_app_findings_block("any-bot", tmp_path)
    assert "telegram" in out


# ── Info severity is filtered out (noise) ────────────────────────────────

def test_info_severity_filtered(fake_home, tmp_path):
    from session_surface import load_app_findings_block
    _write_manifest(fake_home, "x", {
        "coherence": {"findings": [{
            "id": "C-A6", "severity": "info",
            "description": "x", "evidence": [],
        }]},
    })
    out = load_app_findings_block("any-bot", tmp_path)
    assert out == ""


# ── Orphan-confirmed manifest surfaces ──────────────────────────────────

def test_orphan_confirmed_surfaces(fake_home, tmp_path):
    from session_surface import load_app_findings_block
    _write_manifest(fake_home, "old-app", {
        "id": "old-app",
        "reconciliation": {"status": "orphan"},
    })
    out = load_app_findings_block("any-bot", tmp_path)
    assert "old-app" in out
    assert "infrastructure files" in out
    # Reinstall hint included.
    assert "evo install old-app" in out


# ── Malformed JSON → empty (silent) ──────────────────────────────────────

def test_malformed_manifest_is_silent(fake_home, tmp_path):
    mdir = fake_home / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "bad.json").write_text("not json {{{")
    from session_surface import load_app_findings_block
    # Should not raise.
    out = load_app_findings_block("any-bot", tmp_path)
    assert out == ""


# ── .scan-status.json is skipped ─────────────────────────────────────────

def test_scan_status_file_skipped(fake_home, tmp_path):
    mdir = fake_home / ".openclaw" / "workspace" / "manifests"
    mdir.mkdir(parents=True)
    (mdir / ".scan-status.json").write_text("{}")
    _write_manifest(fake_home, "app1", {
        "coherence": {"findings": [{
            "id": "C-A1", "severity": "critical",
            "description": "real finding", "evidence": [],
        }]},
    })
    from session_surface import load_app_findings_block
    out = load_app_findings_block("any-bot", tmp_path)
    assert "real finding" in out


# ── Long messages truncated ──────────────────────────────────────────────

def test_long_message_is_truncated(fake_home, tmp_path):
    very_long = "x" * 500
    from session_surface import load_app_findings_block
    _write_manifest(fake_home, "app1", {
        "coherence": {"findings": [{
            "id": "C-A1", "severity": "critical",
            "description": very_long, "evidence": [],
        }]},
    })
    out = load_app_findings_block("any-bot", tmp_path)
    assert "..." in out
    # Block shouldn't contain the full 500-char string.
    assert "x" * 500 not in out
