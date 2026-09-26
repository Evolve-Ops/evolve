"""Tests for audit.audit_policy_file_permissions.

Policy files (EMAIL_WHITELIST.md, EMAIL_POLICY.md) define what the bot
is allowed to do. They must be mode 0444 — read-only for everyone
including the bot's own UNIX user — so the bot cannot rewrite its own
policy to grant itself permissions the operator never approved.

Security_bot's per-instance audit checked EMAIL_WHITELIST.md was chmod 444;
porting that into Evolve was gap #5 of the Security_bot retirement coverage
map. Confirmed on the live mini on 2026-05-26: admin_bot had both files
at 0644 (owner-writable) — exactly the failure mode this check now
catches.

Pinned behavior:
  - file absent              → no finding (no policy to enforce)
  - mode 0444 (or stricter)  → ok
  - any write bit set        → critical with chmod 0444 playbook
  - stat raises permission   → skipped
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _patch_bot_home(home: Path):
    return patch.object(audit, "_bot_home", lambda *_a, **_k: home)


def _make_workspace(tmp_path: Path, bot_id: str = "admin_bot") -> Path:
    home = tmp_path / bot_id
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    return home


def test_no_policy_files_yields_no_findings(tmp_path: Path):
    home = _make_workspace(tmp_path)
    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert findings == []


def test_locked_0444_file_emits_ok(tmp_path: Path):
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_WHITELIST.md"
    f.write_text("allow: ops@example.com\n")
    os.chmod(f, 0o444)

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert len(findings) == 1
    assert findings[0].level == "ok"
    assert "EMAIL_WHITELIST.md permission OK (0444)" in findings[0].message


def test_owner_writable_0644_emits_critical(tmp_path: Path):
    """This is the live-mini admin_bot case — file at 0644, must page."""
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_WHITELIST.md"
    f.write_text("allow: ops@example.com\n")
    os.chmod(f, 0o644)

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert len(findings) == 1
    f0 = findings[0]
    assert f0.level == "critical"
    assert f0.category == "identity"
    assert f0.bot_id == "admin_bot"
    assert "EMAIL_WHITELIST.md is writable" in f0.message
    assert "0644" in f0.message
    assert "0444" in f0.fix_steps
    assert "policy" in f0.what_it_means.lower()


def test_group_writable_also_critical(tmp_path: Path):
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_WHITELIST.md"
    f.write_text("allow: ops@example.com\n")
    os.chmod(f, 0o464)  # group-writable

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert findings[0].level == "critical"


def test_world_writable_also_critical(tmp_path: Path):
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_WHITELIST.md"
    f.write_text("allow: ops@example.com\n")
    os.chmod(f, 0o446)  # world-writable

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert findings[0].level == "critical"


def test_email_policy_md_also_covered(tmp_path: Path):
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_POLICY.md"
    f.write_text("policy\n")
    os.chmod(f, 0o644)

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert len(findings) == 1
    assert findings[0].level == "critical"
    assert "EMAIL_POLICY.md" in findings[0].message


def test_both_files_present_emits_one_finding_each(tmp_path: Path):
    home = _make_workspace(tmp_path)
    ws = home / ".openclaw" / "workspace"
    (ws / "EMAIL_WHITELIST.md").write_text("a")
    os.chmod(ws / "EMAIL_WHITELIST.md", 0o444)
    (ws / "EMAIL_POLICY.md").write_text("p")
    os.chmod(ws / "EMAIL_POLICY.md", 0o644)

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert len(findings) == 2
    by_level = {f.message.split(":")[1].strip().split()[0]: f for f in findings}
    # EMAIL_WHITELIST.md → ok; EMAIL_POLICY.md → critical
    assert any(f.level == "ok" for f in findings)
    assert any(f.level == "critical" for f in findings)


def test_mode_0400_also_ok(tmp_path: Path):
    """Stricter than 0444 (e.g. 0400 = owner read only) is also acceptable."""
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_WHITELIST.md"
    f.write_text("a")
    os.chmod(f, 0o400)

    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert findings[0].level == "ok"


def test_stat_permission_error_emits_skipped(tmp_path: Path, monkeypatch):
    home = _make_workspace(tmp_path)
    f = home / ".openclaw" / "workspace" / "EMAIL_WHITELIST.md"
    f.write_text("a")

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if self == f:
            raise PermissionError("simulated locked-down workspace")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)
    with _patch_bot_home(home):
        findings = audit.audit_policy_file_permissions("admin_bot")
    assert findings[0].level == "skipped"
