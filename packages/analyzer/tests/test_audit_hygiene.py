"""Tests for Phase 3 audit-hygiene changes:

  1. Capability-gap findings carry level="skipped" (not "warn"), so they
     don't generate alert-page noise when the audit can't run a check.
     The dispatch loop only buckets criticals + warns into the signal
     pipeline, so "skipped" naturally stays out — these tests pin the
     level so the suppression doesn't silently regress.

  2. Script-inventory drift is coalesced to one finding per bot (the
     prior shape was N findings per drifted file, which made a single
     security_bot redeploy look like five separate alerts).

  3. _check_oc_binary_mtime is version-aware: a brew upgrade that
     changes the mtime AND the version is a clean upgrade — auto-refresh
     the baseline and emit OK rather than warn.

  4. reset_baseline() drops a bot's entry from a per-bot baseline so
     deploy hooks (and "accept new baseline" affordances) can clear
     drift findings without operator file-editing on the mini.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


# ── Capability-gap findings → level="skipped" ────────────────────────────────


def test_evolve_sudoers_read_denied_is_skipped(tmp_path: Path):
    """When sudo /bin/cat can't read the file, finding is 'skipped', not 'warn'."""
    with patch.object(audit, "sha256_sudo", return_value=None):
        findings = audit.audit_evolve_sudoers(tmp_path, {})
    assert findings, "expected one capability-gap finding"
    assert findings[0].level == "skipped"
    assert findings[0].category == "config"
    assert findings[0].detail, "skipped findings should carry diagnostic detail"


def test_sshd_check_denied_is_skipped():
    """sshd -T failing emits a 'skipped' machine finding, not 'warn'."""
    class _R:
        returncode = 1
        stdout = ""
        stderr = "permission denied"

    with patch.object(audit.subprocess, "run", return_value=_R()):
        findings = audit._check_ssh_config()
    assert findings and findings[0].level == "skipped"
    assert "sshd" in findings[0].message.lower()


def test_listening_ports_denied_is_skipped(tmp_path: Path):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "permission denied"

    with patch.object(audit.subprocess, "run", return_value=_R()):
        findings = audit._check_listening_ports(tmp_path)
    assert findings and findings[0].level == "skipped"


def test_cron_jobs_missing_is_ok_not_denied(tmp_path: Path):
    """A MISSING cron/jobs.json is the benign no-cron-jobs case — it must NOT
    trip the sudo-cat fallback (FileNotFoundError is an OSError subclass; the
    old blanket except sudo-cat'ed a nonexistent path on every run — 116
    denials/day on the VPS) and must not read as a capability gap."""
    fake_home = tmp_path / "personal_bot"
    fake_home.mkdir()

    def _no_sudo(*_a, **_k):  # the fallback must not fire at all
        raise AssertionError("sudo cat fallback invoked for a missing jobs.json")

    with patch.object(audit, "_bot_home", lambda *_a, **_k: fake_home), \
         patch.object(audit.subprocess, "run", _no_sudo):
        findings = audit.audit_cron_health("personal_bot", tmp_path)
    assert findings, "expected at least one finding"
    assert findings[0].level == "ok"
    assert "no cron jobs" in findings[0].message


def test_cron_jobs_read_denied_is_skipped(tmp_path: Path):
    """personal_bot-style 'cannot read cron/jobs.json' (EACCES + failed sudo
    fallback) is a capability gap, not anomaly."""
    fake_home = tmp_path / "personal_bot"
    fake_home.mkdir()
    cron_dir = fake_home / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    jobs = cron_dir / "jobs.json"
    jobs.write_text("{}")
    jobs.chmod(0)  # direct read → PermissionError

    class _R:
        returncode = 1
        stdout = ""
        stderr = ""

    try:
        with patch.object(audit, "_bot_home", lambda *_a, **_k: fake_home), \
             patch.object(audit.subprocess, "run", return_value=_R()):
            findings = audit.audit_cron_health("personal_bot", tmp_path)
    finally:
        jobs.chmod(0o644)  # let tmp_path cleanup succeed
    # The first finding should be the capability-gap one.
    assert findings, "expected at least one finding"
    assert findings[0].level == "skipped"
    assert "cron/jobs.json" in findings[0].message


def test_zshrc_unreadable_on_first_run_is_skipped(tmp_path: Path):
    """No baseline + unreadable .zshrc → 'skipped' (was 'warn' before Phase 3)."""
    fake_home = tmp_path / "admin_bot"
    fake_home.mkdir()
    # .zshrc exists but read denied — simulate by patching _hash_bot_zshrc to None.
    with patch.object(audit, "_hash_bot_zshrc", return_value=None), \
         patch.object(audit, "_bot_home", lambda *_a, **_k: fake_home):
        findings = audit.audit_shell_config(["admin_bot"], tmp_path)
    # Filter for the admin_bot-related finding (the function may emit others).
    admin_bot_findings = [f for f in findings if f.bot_id == "admin_bot"]
    assert admin_bot_findings and admin_bot_findings[0].level == "skipped"


def test_zshrc_unreadable_after_baseline_stays_warn(tmp_path: Path):
    """Pre-existing baseline + lost read access = real signal (warn).

    Capability gap on first-run is noise. Losing access after we'd
    established a baseline could mean someone removed the audit's read
    grant — that one is worth surfacing.
    """
    baseline = tmp_path / "security" / "baselines" / "shell-hashes.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"admin_bot": "a" * 64}))  # had a hash
    with patch.object(audit, "_hash_bot_zshrc", return_value=None), \
         patch.object(audit, "_bot_home", lambda *_a, **_k: tmp_path / "admin_bot"):
        findings = audit.audit_shell_config(["admin_bot"], tmp_path)
    admin_bot = [f for f in findings if f.bot_id == "admin_bot"]
    assert admin_bot and admin_bot[0].level == "warn"


# ── Script inventory drift coalesces to one finding per bot ──────────────────


def test_script_inventory_drift_emits_one_finding_per_bot(tmp_path: Path):
    """N new + M missing files → 1 finding (was 1 per file before Phase 3)."""
    workspace = tmp_path / "security_bot" / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "scripts").mkdir()
    (workspace / "tmp").mkdir()
    # Seed three "current" files in the workspace
    (workspace / "scripts" / "new-1.py").write_text("")
    (workspace / "scripts" / "new-2.py").write_text("")
    (workspace / "tmp" / "new-3.sh").write_text("")

    # Pre-write a baseline that has TWO different files (both will be missing)
    baseline_path = tmp_path / "security" / "baselines" / "scripts.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps({
        "security_bot": [
            str(workspace / "old-1.py"),
            str(workspace / "old-2.py"),
        ]
    }, indent=2))

    with patch.object(audit, "_bot_home", lambda *_a, **_k: tmp_path / "security_bot"):
        findings = audit.audit_script_inventory("security_bot", tmp_path)

    # Exactly one drift finding (was 5 before: 3 new + 2 missing).
    warn_findings = [f for f in findings if f.level == "warn"]
    assert len(warn_findings) == 1, [f.message for f in warn_findings]
    msg = warn_findings[0].message
    assert "drift" in msg
    assert "+3 new" in msg
    assert "-2 missing" in msg
    # Detail field carries the actual file lists.
    assert "new-1.py" in warn_findings[0].detail
    assert "old-1.py" in warn_findings[0].detail


# ── _check_oc_binary_mtime: version-aware auto-refresh ───────────────────────


def _seed_fake_binary(tmp_path: Path, mtime_secs: float, monkeypatch) -> Path:
    """Create a fake openclaw binary at a known mtime and steer the audit at
    it by stubbing the shared resolver.

    The check resolves the binary through ``platform_profile.find_openclaw_cli``
    at CALL time (it used to hold a module-level macOS-only Homebrew candidate
    list), so stubbing that one name is the whole seam."""
    fake = tmp_path / "openclaw"
    fake.write_text("binary")
    import os
    os.utime(fake, (mtime_secs, mtime_secs))
    monkeypatch.setattr(audit, "find_openclaw_cli", lambda: str(fake))
    return fake


def test_oc_binary_mtime_clean_upgrade_refreshes_baseline(tmp_path: Path, monkeypatch):
    """Mtime + version both changed → it's a brew upgrade, not anomaly."""
    fake = _seed_fake_binary(tmp_path, mtime_secs=2000.0, monkeypatch=monkeypatch)
    monkeypatch.setattr(audit, "_read_openclaw_version",
                        lambda _p: "openclaw 2026.4.29")
    baseline = tmp_path / "security" / "oc-binary-mtime.baseline"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"mtime": "1000", "version": "openclaw 2026.4.10"}))

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "ok"
    assert "upgraded" in findings[0].message
    refreshed = json.loads(baseline.read_text())
    assert refreshed["version"] == "openclaw 2026.4.29"
    assert refreshed["mtime"] == "2000"


def test_oc_binary_mtime_change_without_version_bump_warns(tmp_path: Path, monkeypatch):
    """Mtime changed but version didn't → suspicious; still warn."""
    fake = _seed_fake_binary(tmp_path, mtime_secs=2000.0, monkeypatch=monkeypatch)
    monkeypatch.setattr(audit, "_read_openclaw_version",
                        lambda _p: "openclaw 2026.4.29")
    baseline = tmp_path / "security" / "oc-binary-mtime.baseline"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"mtime": "1000", "version": "openclaw 2026.4.29"}))

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "warn"
    assert "without version delta" in findings[0].message


def test_oc_binary_mtime_legacy_baseline_migrates(tmp_path: Path, monkeypatch):
    """Pre-Phase-3 baseline was a bare mtime string; first run migrates it."""
    fake = _seed_fake_binary(tmp_path, mtime_secs=2000.0, monkeypatch=monkeypatch)
    monkeypatch.setattr(audit, "_read_openclaw_version",
                        lambda _p: "openclaw 2026.4.29")
    baseline = tmp_path / "security" / "oc-binary-mtime.baseline"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text("1000")  # legacy: bare mtime, no JSON

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "ok"
    assert "migrated" in findings[0].message
    # Baseline is now JSON with both fields.
    migrated = json.loads(baseline.read_text())
    assert migrated["version"] == "openclaw 2026.4.29"


def test_oc_binary_mtime_unchanged_emits_ok(tmp_path: Path, monkeypatch):
    """Stable mtime → OK (no churn on every audit run)."""
    fake = _seed_fake_binary(tmp_path, mtime_secs=1000.0, monkeypatch=monkeypatch)
    monkeypatch.setattr(audit, "_read_openclaw_version",
                        lambda _p: "openclaw 2026.4.29")
    baseline = tmp_path / "security" / "oc-binary-mtime.baseline"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"mtime": "1000", "version": "openclaw 2026.4.29"}))

    findings = audit._check_oc_binary_mtime(tmp_path)

    assert findings and findings[0].level == "ok"
    assert "OK" in findings[0].message


# ── reset_baseline ───────────────────────────────────────────────────────────


def test_reset_baseline_drops_bot_entry(tmp_path: Path):
    """reset_baseline('security_bot', 'scripts', sd) removes security_bot's entry only."""
    baseline = tmp_path / "security" / "baselines" / "scripts.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({
        "security_bot": ["/Users/security_bot/.openclaw/workspace/old.py"],
        "admin_bot": ["/Users/admin_bot/.openclaw/workspace/keep.py"],
    }))

    assert audit.reset_baseline("security_bot", "scripts", tmp_path) is True
    data = json.loads(baseline.read_text())
    assert "security_bot" not in data
    assert "admin_bot" in data, "other bots' baselines must be preserved"


def test_reset_baseline_unknown_bot_returns_false(tmp_path: Path):
    baseline = tmp_path / "security" / "baselines" / "scripts.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"admin_bot": []}))
    assert audit.reset_baseline("security_bot", "scripts", tmp_path) is False


def test_reset_baseline_unknown_kind_returns_false(tmp_path: Path):
    assert audit.reset_baseline("security_bot", "nonsense", tmp_path) is False


def test_reset_baseline_supports_shell_and_cron_kinds(tmp_path: Path):
    """All three documented kinds resolve to their respective baseline files."""
    for kind, filename in [
        ("scripts", "scripts.json"),
        ("shell", "shell-hashes.json"),
        ("cron-jobs", "cron-jobs.json"),
    ]:
        baseline = tmp_path / "security" / "baselines" / filename
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps({"security_bot": "anything"}))
        assert audit.reset_baseline("security_bot", kind, tmp_path) is True
        assert "security_bot" not in json.loads(baseline.read_text())
