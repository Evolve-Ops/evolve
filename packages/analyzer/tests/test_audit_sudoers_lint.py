"""Tests for audit._lint_sudoers_content.

The existing audit_evolve_sudoers hash-checks the sudoers file against
a baseline so any drift fires critical. The new linter catches the
flip-side failure mode: a dangerous grant that's BEEN there from day 0
(or that ships with a deliberate baseline re-bless). Pure regex on the
live content; matches dangerous patterns regardless of baseline state.

Pinned behavior:
  - "evolve ALL=(ALL) NOPASSWD: ALL"  → critical + playbook
  - "evolve ALL=NOPASSWD: ALL"        → critical
  - "evolve ALL=(root) NOPASSWD: ALL" → critical
  - "evolve ALL=(ALL) NOPASSWD: *"    → critical (bare wildcard)
  - Commented-out dangerous patterns  → no finding (skip comments)
  - The real production sudoers       → no findings (legitimate narrow grants)
  - read failure                      → no findings (handled by hash-check path)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _set_sudoers_text(monkeypatch, text: str | None) -> None:
    """Monkey-patch _read_sudoers_text to return a fixed string."""
    monkeypatch.setattr(audit, "_read_sudoers_text", lambda _p: text)


SUDOERS_PATH = Path("/etc/sudoers.d/evolve")


# ── dangerous patterns ───────────────────────────────────────────────


def test_evolve_all_all_nopasswd_all_is_critical(monkeypatch):
    _set_sudoers_text(monkeypatch, "evolve ALL=(ALL) NOPASSWD: ALL\n")
    findings = audit._lint_sudoers_content(SUDOERS_PATH)
    assert len(findings) == 1
    f = findings[0]
    assert f.level == "critical"
    assert "evolve ALL=(ALL) NOPASSWD: ALL" in f.message
    assert "narrow-grants design" in f.what_it_means
    assert "refresh-sudoers" in f.fix_steps


def test_evolve_all_nopasswd_all_is_critical(monkeypatch):
    """Without the (ALL) runas spec — still unrestricted."""
    _set_sudoers_text(monkeypatch, "evolve ALL=NOPASSWD: ALL\n")
    findings = audit._lint_sudoers_content(SUDOERS_PATH)
    assert len(findings) == 1
    assert findings[0].level == "critical"


def test_evolve_root_nopasswd_all_is_critical(monkeypatch):
    _set_sudoers_text(monkeypatch, "evolve ALL=(root) NOPASSWD: ALL\n")
    findings = audit._lint_sudoers_content(SUDOERS_PATH)
    assert len(findings) == 1
    assert findings[0].level == "critical"


def test_bare_wildcard_command_is_critical(monkeypatch):
    _set_sudoers_text(monkeypatch, "evolve ALL=(ALL) NOPASSWD: *\n")
    findings = audit._lint_sudoers_content(SUDOERS_PATH)
    assert len(findings) == 1
    assert "NOPASSWD: *" in findings[0].message


def test_line_number_surfaced(monkeypatch):
    text = (
        "# /etc/sudoers.d/evolve\n"
        "Defaults:evolve !requiretty\n"
        "evolve ALL=(ALL) NOPASSWD: ALL\n"
    )
    _set_sudoers_text(monkeypatch, text)
    findings = audit._lint_sudoers_content(SUDOERS_PATH)
    assert "line 3" in findings[0].message


# ── safe patterns (the real production sudoers shouldn't fire) ────────


def test_narrow_command_grants_are_fine(monkeypatch):
    """The shape of real production grants — explicit command + path."""
    text = (
        "evolve ALL=(root) NOPASSWD: /bin/cat /Users/*/.openclaw/openclaw.json\n"
        "evolve ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/ai.evolve.*\n"
        "evolve ALL=(ALL) NOPASSWD: SETENV: /opt/homebrew/bin/openclaw\n"
    )
    _set_sudoers_text(monkeypatch, text)
    assert audit._lint_sudoers_content(SUDOERS_PATH) == []


def test_commented_dangerous_pattern_ignored(monkeypatch):
    """Doc blocks may discuss the bad pattern in comments — don't flag those."""
    text = (
        "# DON'T: evolve ALL=(ALL) NOPASSWD: ALL would grant root\n"
        "evolve ALL=(root) NOPASSWD: /bin/cat /Users/*/openclaw.json\n"
    )
    _set_sudoers_text(monkeypatch, text)
    assert audit._lint_sudoers_content(SUDOERS_PATH) == []


def test_inline_comment_after_grant_ignored(monkeypatch):
    """sudoers allows trailing # comments; the linter strips before matching."""
    text = "evolve ALL=(root) NOPASSWD: /bin/cat /Users/*/openclaw.json  # safe read\n"
    _set_sudoers_text(monkeypatch, text)
    assert audit._lint_sudoers_content(SUDOERS_PATH) == []


def test_blank_lines_dont_fire(monkeypatch):
    _set_sudoers_text(monkeypatch, "\n\n   \n\n")
    assert audit._lint_sudoers_content(SUDOERS_PATH) == []


# ── read failure passthrough ──────────────────────────────────────────


def test_read_failure_returns_empty(monkeypatch):
    """The skipped finding comes from the hash-check path; the linter
    silently returns nothing so we don't double-report 'can't read'."""
    _set_sudoers_text(monkeypatch, None)
    assert audit._lint_sudoers_content(SUDOERS_PATH) == []


# ── multiple offenses ────────────────────────────────────────────────


def test_multiple_dangerous_lines_each_fire(monkeypatch):
    text = (
        "evolve ALL=(ALL) NOPASSWD: ALL\n"
        "Defaults:evolve !requiretty\n"
        "evolve ALL=NOPASSWD: ALL\n"
    )
    _set_sudoers_text(monkeypatch, text)
    findings = audit._lint_sudoers_content(SUDOERS_PATH)
    assert len(findings) == 2
    assert all(f.level == "critical" for f in findings)


def test_integration_with_audit_evolve_sudoers(tmp_path: Path, monkeypatch):
    """The hash-check + lint compose: dangerous grant + matching baseline
    still fires critical from the linter, even when the hash check is
    satisfied. This is the WHOLE POINT of the linter — without it, an
    operator who re-blesses the baseline after a dangerous edit would
    silence the alert."""
    dangerous = "evolve ALL=(ALL) NOPASSWD: ALL\n"
    _set_sudoers_text(monkeypatch, dangerous)
    # Make sha256_sudo return a known hash and write a matching baseline
    monkeypatch.setattr(audit, "sha256_sudo", lambda _p: "abc123" * 10)
    baseline_path = tmp_path / "security" / "sudoers-evolve.sha256"
    baseline_path.parent.mkdir(parents=True)
    baseline_path.write_text("abc123" * 10)

    findings = audit.audit_evolve_sudoers(tmp_path, {})
    # Both the linter critical AND the hash-OK appear
    levels = [f.level for f in findings]
    assert "critical" in levels  # from the lint
    assert "ok" in levels         # from the hash match
    # The critical finding has the dangerous-grant message
    crit = next(f for f in findings if f.level == "critical")
    assert "dangerous grant" in crit.message
