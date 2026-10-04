"""Regression tests for audit.audit_workspace_secrets.

Background: issue 2026-04-27-003 (plaintext credentials in bot workspace
files) flagged 6 files across team_bot_a/team_bot_c/security_bot with real-shaped tokens.
While triaging, we found audit_workspace_secrets had two gaps:

  1. `.env` files were silently skipped (`Path('.env').suffix == ''`,
     so the `_SCAN_EXTENSIONS` check excluded them — even though .env
     is the canonical hiding spot for plaintext tokens).

  2. Slack tokens (xoxb-, xapp-) had no patterns at all — Team_bot_a's leaked
     `.env` had two Slack tokens that were invisible to the audit.

These tests pin both fixes plus the existing patterns (Anthropic,
GitHub, Telegram, OpenAI, xAI) against representative real-shaped
samples and confirmed false-positives.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────


def _run_against_workspace(workspace: Path, bot_id: str = "testbot") -> list:
    """Invoke audit_workspace_secrets against a tmpdir workspace.

    audit_workspace_secrets resolves the workspace via _bot_home(bot_id) /
    ".openclaw" / "workspace"; build a fake home tree whose .openclaw/workspace
    is a symlink to `workspace`, then patch _bot_home to return that home.
    """
    home = workspace.parent / "_fake_home"
    oc = home / ".openclaw"
    oc.mkdir(parents=True, exist_ok=True)
    target_ws = oc / "workspace"
    if target_ws.exists() or target_ws.is_symlink():
        target_ws.unlink()
    target_ws.symlink_to(workspace)
    with patch.object(audit, "_bot_home", lambda *_a, **_k: home):
        return audit.audit_workspace_secrets(bot_id, workspace.parent)


# ── .env scanning (the headline fix) ────────────────────────────────────


def test_env_file_with_anthropic_key_is_flagged(tmp_path: Path):
    """`.env` (no extension) must be scanned, not skipped."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        'ANTHROPIC_API_KEY=sk-ant-api03-' + 'A' * 95 + '\n'
    )
    findings = _run_against_workspace(workspace)
    crits = [f for f in findings if f.level == "critical"]
    assert len(crits) == 1
    assert "Anthropic API key" in crits[0].message
    assert ".env" in crits[0].message


def test_env_local_variant_is_flagged(tmp_path: Path):
    """`.env.local`, `.env.production`, etc. — same rule."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env.production").write_text(
        'TELEGRAM_TOKEN=1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n'
    )
    findings = _run_against_workspace(workspace)
    crits = [f for f in findings if f.level == "critical"]
    assert len(crits) == 1
    assert "Telegram bot token" in crits[0].message


def test_clean_workspace_returns_ok_finding(tmp_path: Path):
    """No-secrets workspaces must emit one OK finding (not silent)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Agent role description\n")
    (workspace / ".env").write_text("FOO=bar\n# nothing sensitive here\n")
    findings = _run_against_workspace(workspace)
    assert len(findings) == 1
    assert findings[0].level == "ok"
    assert "scan clean" in findings[0].message


# ── Pattern coverage ────────────────────────────────────────────────────


@pytest.mark.parametrize("token,label", [
    # Real-shaped samples (NOT live tokens — these are pattern-fitting
    # placeholders generated to match the audit regexes).
    ("ghp_" + "A" * 36,                                 "GitHub PAT (classic)"),
    ("github_pat_" + "A" * 82,                          "GitHub PAT (fine-grained)"),
    ("sk-ant-" + "A" * 95,                              "Anthropic API key"),
    ("sk-proj-" + "A" * 50,                             "OpenAI project key"),
    ("xai-" + "A" * 50,                                 "xAI API key"),
    ("1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  "Telegram bot token"),
    ("xoxb-1111111111111-1111111111111-" + "A" * 24,    "Slack bot token"),
    ("xapp-1-A0AKTF2406N-1068642859014-" + "f" * 64,    "Slack app token"),
])
def test_each_pattern_in_md_file(tmp_path: Path, token: str, label: str):
    """Every credential pattern fires when found in a workspace .md file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SETUP.md").write_text(f"Configured with token: {token}\n")
    findings = _run_against_workspace(workspace)
    crits = [f for f in findings if f.level == "critical"]
    assert len(crits) == 1, f"Expected 1 critical finding for {label}, got {len(crits)}"
    assert label in crits[0].message


# ── Allowlist still works ───────────────────────────────────────────────


def test_auth_profiles_json_is_allowlisted(tmp_path: Path):
    """auth-profiles.json is the canonical secret store and must NOT flag."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "auth-profiles.json").write_text(
        '{"profiles": {"anthropic": {"key": "sk-ant-' + 'A' * 95 + '"}}}'
    )
    findings = _run_against_workspace(workspace)
    crits = [f for f in findings if f.level == "critical"]
    assert crits == []


def test_openclaw_json_is_allowlisted(tmp_path: Path):
    """openclaw.json holds integration tokens by design."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "openclaw.json").write_text(
        '{"integrations": {"telegram": {"botToken": "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}}'
    )
    findings = _run_against_workspace(workspace)
    crits = [f for f in findings if f.level == "critical"]
    assert crits == []


# ── Ignored paths ───────────────────────────────────────────────────────


def test_git_directory_is_skipped(tmp_path: Path):
    """`.git/config` may contain tokens via git credential helpers;
    skipping it is intentional (the canonical workspace audit doesn't
    own .git hygiene)."""
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        "[remote]\nurl = https://x:ghp_" + "A" * 36 + "@github.com/foo.git\n"
    )
    findings = _run_against_workspace(workspace)
    crits = [f for f in findings if f.level == "critical"]
    assert crits == []
