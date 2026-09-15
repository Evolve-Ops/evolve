"""Tests for audit._check_macos_updates and helpers.

Ports Security_bot's macOS-update check into Evolve. Severity policy:
  - Security Update pending          → critical
  - any recommended update pending   → warn
  - only optional/driver updates     → ok (noted)
  - no pending updates               → ok

Caching: a 12h JSON cache under ``{shared_dir}/security/`` keeps the
15-minute audit cycle from re-querying Apple's catalog on every run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import platform_profile  # noqa: E402


# Real output captured from a Mac mini on 2026-05-26 — exactly what the
# parser has to handle in production.
_MINI_REAL_OUTPUT = """Software Update Tool

Finding available software
Software Update found the following new or updated software:
* Label: Command Line Tools for Xcode 26.5-26.5
\tTitle: Command Line Tools for Xcode 26.5, Version: 26.5, Size: 920416KiB, Recommended: YES,
* Label: macOS Tahoe 26.5-25F71
\tTitle: macOS Tahoe 26.5, Version: 26.5, Size: 3690218KiB, Recommended: YES, Action: restart,
"""


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ── parser ────────────────────────────────────────────────────────────


def test_parse_real_mini_output_finds_two_updates():
    updates = audit._parse_softwareupdate_list(_MINI_REAL_OUTPUT)
    assert len(updates) == 2
    titles = {u["title"] for u in updates}
    assert "macOS Tahoe 26.5" in titles
    assert "Command Line Tools for Xcode 26.5" in titles
    assert all(u["recommended"] for u in updates)


def test_parse_empty_output_returns_no_updates():
    out = "Software Update Tool\n\nFinding available software\nNo new software available.\n"
    assert audit._parse_softwareupdate_list(out) == []


def test_parse_security_update_detected_by_title():
    out = (
        "Software Update Tool\n\n"
        "Software Update found the following new or updated software:\n"
        "* Label: SecurityUpdate2026-001-13.7.1\n"
        "\tTitle: Security Update 2026-001, Version: 13.7.1, Size: 999KiB, Recommended: YES,\n"
    )
    updates = audit._parse_softwareupdate_list(out)
    assert len(updates) == 1
    assert "security update" in updates[0]["title"].lower()


# ── severity routing ──────────────────────────────────────────────────


def test_no_updates_emits_ok(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "_query_macos_updates",
                        lambda: ("No new software available.", "2026-05-26T00:00:00+00:00"))
    findings = audit._check_macos_updates(tmp_path)
    assert findings[0].level == "ok"
    assert "none pending" in findings[0].message


def test_security_update_emits_critical(tmp_path: Path, monkeypatch):
    out = (
        "Software Update found the following new or updated software:\n"
        "* Label: SecurityUpdate2026-001\n"
        "\tTitle: Security Update 2026-001, Version: 1.0, Size: 1KiB, Recommended: YES,\n"
    )
    monkeypatch.setattr(audit, "_query_macos_updates",
                        lambda: (out, "2026-05-26T00:00:00+00:00"))
    findings = audit._check_macos_updates(tmp_path)
    assert findings[0].level == "critical"
    assert "Security Update" in findings[0].message
    assert "publicly-disclosed" in findings[0].what_it_means
    assert "Software Update" in findings[0].fix_steps


def test_recommended_only_emits_warn(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "_query_macos_updates",
                        lambda: (_MINI_REAL_OUTPUT, "2026-05-26T00:00:00+00:00"))
    findings = audit._check_macos_updates(tmp_path)
    assert findings[0].level == "warn"
    assert "2 recommended" in findings[0].message
    assert "Recommended" in findings[0].what_it_means


def test_only_optional_updates_emits_ok(tmp_path: Path, monkeypatch):
    out = (
        "Software Update found the following new or updated software:\n"
        "* Label: SomeOptional\n"
        "\tTitle: Some Optional Driver, Version: 1.0, Size: 1KiB,\n"
    )
    monkeypatch.setattr(audit, "_query_macos_updates",
                        lambda: (out, "2026-05-26T00:00:00+00:00"))
    findings = audit._check_macos_updates(tmp_path)
    assert findings[0].level == "ok"
    assert "1 optional pending" in findings[0].message


# ── caching ───────────────────────────────────────────────────────────


def test_fresh_cache_avoids_requery(tmp_path: Path, monkeypatch):
    cache_path = tmp_path / "security" / "macos-updates-cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "raw_output": _MINI_REAL_OUTPUT,
    }))

    call_count = {"n": 0}
    def _should_not_call():
        call_count["n"] += 1
        return ("", "")
    monkeypatch.setattr(audit, "_query_macos_updates", _should_not_call)

    findings = audit._check_macos_updates(tmp_path)
    assert call_count["n"] == 0
    assert findings[0].level == "warn"  # still emits derived finding
    assert "(cached)" in (findings[0].detail or "")


def test_stale_cache_triggers_requery(tmp_path: Path, monkeypatch):
    cache_path = tmp_path / "security" / "macos-updates-cache.json"
    cache_path.parent.mkdir(parents=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cache_path.write_text(json.dumps({
        "queried_at": old_ts,
        "raw_output": "stale data",
    }))

    new_ts = datetime.now(timezone.utc).isoformat()
    call_count = {"n": 0}
    def _new_query():
        call_count["n"] += 1
        return ("No new software available.", new_ts)
    monkeypatch.setattr(audit, "_query_macos_updates", _new_query)

    findings = audit._check_macos_updates(tmp_path)
    assert call_count["n"] == 1
    assert findings[0].level == "ok"
    # Cache was refreshed.
    fresh = json.loads(cache_path.read_text())
    assert fresh["queried_at"] == new_ts


def test_corrupt_cache_triggers_requery(tmp_path: Path, monkeypatch):
    cache_path = tmp_path / "security" / "macos-updates-cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json {{{")

    monkeypatch.setattr(audit, "_query_macos_updates",
                        lambda: ("No new software available.", "2026-05-26T00:00:00+00:00"))
    findings = audit._check_macos_updates(tmp_path)
    assert findings[0].level == "ok"


# ── infrastructure error paths ────────────────────────────────────────


def test_softwareupdate_unavailable_is_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "_query_macos_updates", lambda: (None, None))
    findings = audit._check_macos_updates(tmp_path)
    assert findings[0].level == "skipped"


def test_softwareupdate_timeout_treated_as_unavailable(monkeypatch):
    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="softwareupdate", timeout=60)
    monkeypatch.setattr(audit.subprocess, "run", _timeout)
    raw, ts = audit._query_macos_updates()
    assert raw is None
    assert ts is None


def test_softwareupdate_missing_treated_as_unavailable(monkeypatch):
    def _missing(*_a, **_k):
        raise FileNotFoundError("softwareupdate")
    monkeypatch.setattr(audit.subprocess, "run", _missing)
    raw, ts = audit._query_macos_updates()
    assert raw is None


# ── integration ───────────────────────────────────────────────────────


def test_wired_into_audit_machine(tmp_path: Path, monkeypatch):
    # macOS-update check is macOS-only; pin the macOS profile so the
    # platform-applicability gate in audit_machine runs it on Linux CI.
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.MACOS)
    monkeypatch.setattr(audit, "_check_firewall", lambda: [])
    monkeypatch.setattr(audit, "_check_filevault", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_ssh_config", lambda: [])
    monkeypatch.setattr(audit, "_check_user_accounts", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_listening_ports", lambda *_a, **_k: [])
    monkeypatch.setattr(audit, "_check_oc_binary_mtime", lambda *_a, **_k: [])
    monkeypatch.setattr(
        audit, "_query_macos_updates",
        lambda: (_MINI_REAL_OUTPUT, "2026-05-26T00:00:00+00:00"),
    )
    findings = audit.audit_machine(tmp_path, {})
    assert any(f.level == "warn" and "recommended" in f.message for f in findings)


def test_policy_acceptance_demotes_recommended_to_ok(tmp_path: Path, monkeypatch):
    """Operator can declare pending recommended updates accepted via
    network.json::policy_acceptances. Security Updates still fire (they
    aren't accepted via this mechanism — see _check_macos_updates docstring)."""
    monkeypatch.setattr(
        audit, "_query_macos_updates",
        lambda: (_MINI_REAL_OUTPUT, "2026-05-26T00:00:00+00:00"),
    )
    config = {
        "policy_acceptances": {
            "machine.macos_updates_pending": {
                "reason": "deferring during release freeze",
                "accepted_at": "2026-06-04",
                "accepted_by": "pod-admin",
            }
        }
    }
    findings = audit._check_macos_updates(tmp_path, config)
    assert all(f.level != "warn" for f in findings), (
        "policy_acceptance should demote the warn finding"
    )
    assert any(
        f.level == "ok" and "operator-accepted" in f.message for f in findings
    )
