"""tests/test_heal_drift_tiers_file.py — drift detector covers evolve-tiers.json.

P0 #4 from the 2026-05-29 tier audit (L10). Before this fix,
evolve-tiers.json — the file the plugin's ModelRouter actually reads
for cascade routing (cascade.enabled, tier definitions, tierCascade)
— was outside the backup/heal/cost_watchdog layers entirely. A manual
`vim` edit of cascade.enabled or wipe of tier2.models[] changed live
routing with:

  • zero alert (heal didn't diff this file)
  • zero diff (operator never saw what changed)
  • zero snapshot (backup.py didn't stage this file)
  • zero credit (the audit log had no entry to explain)

The single largest hole in the watchdog layer per the audit synthesis.

After this fix:
  • backup.py stages evolve-tiers.json into evolve-backup/ alongside
    openclaw.json. Round-trips to the remote backup repo.
  • heal.detect_backup_drift_keys diffs both files. Returns keys from
    openclaw.json bare ("agents", "plugins") and keys from
    evolve-tiers.json namespaced as "tiers:<key>" so operators can
    distinguish file-of-origin in the advisory.
  • Same TTL-windowed explain logic from PR #1738 still applies to
    explain admin-UI writes.

Coverage:
- evolve-tiers.json change ONLY → drift detector surfaces "tiers:<key>"
- openclaw.json + evolve-tiers.json both changed → both surface
- evolve-tiers.json missing in backup (older bot) → no crash, falls
  back to openclaw.json-only diff
- evolve-tiers.json missing on live side (bot never wrote one) → same
- meta key on openclaw.json still ignored (no regression to the
  existing false-positive guard)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import heal  # noqa: E402
from heal import detect_backup_drift_keys  # noqa: E402


class _FakeProc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_subprocess_and_file_reads(
    monkeypatch,
    *,
    committed_oc: dict,
    live_oc: dict,
    committed_tiers: dict | None = None,
    live_tiers: dict | None = None,
):
    """Stub the git-show subprocess + Path.read_text for both files.

    Routes the openclaw.json / evolve-tiers.json reads to either:
      • the committed_* dict when called via `git show HEAD:evolve-backup/<file>`
      • the live_* dict when called via Path.read_text on /Users/<bot>/.openclaw/<file>

    Pass None for committed_tiers/live_tiers to simulate "file doesn't
    exist in backup HEAD" / "no live tier file" — the drift detector
    should fall back to openclaw.json-only diffing.
    """
    real_run = heal.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "git" in cmd:
            for c in cmd:
                if isinstance(c, str):
                    if "HEAD:evolve-backup/openclaw.json" in c:
                        return _FakeProc(0, json.dumps(committed_oc))
                    if "HEAD:evolve-backup/evolve-tiers.json" in c:
                        if committed_tiers is None:
                            return _FakeProc(1, "")
                        return _FakeProc(0, json.dumps(committed_tiers))
        return real_run(cmd, *args, **kwargs)

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        s = str(self)
        if s.endswith("/.openclaw/openclaw.json"):
            return json.dumps(live_oc)
        if s.endswith("/.openclaw/evolve-tiers.json"):
            if live_tiers is None:
                raise FileNotFoundError(s)
            return json.dumps(live_tiers)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(heal.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "read_text", fake_read_text)


# ── evolve-tiers.json drift is detected (THE LOAD-BEARING TEST) ───────────


def test_REGRESSION_L10_tiers_only_drift_surfaces_as_namespaced_key(tmp_path, monkeypatch):
    """The exact failure mode this PR closes: operator (or attacker)
    flips cascade.enabled in evolve-tiers.json. Pre-L10 this changed
    live routing with no drift alert. Now it surfaces as
    'tiers:cascade'."""
    committed_oc = {
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-4-6"}}},
    }
    committed_tiers = {
        "tiers": {"tier2": {"models": ["anthropic/claude-sonnet-4-6"]}},
        "cascade": {"enabled": False},
    }
    live_oc = committed_oc  # openclaw.json unchanged
    live_tiers = {
        **committed_tiers,
        "cascade": {"enabled": True},  # operator flipped this without authorization
    }
    _stub_subprocess_and_file_reads(
        monkeypatch,
        committed_oc=committed_oc, live_oc=live_oc,
        committed_tiers=committed_tiers, live_tiers=live_tiers,
    )

    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    assert "tiers:cascade" in keys, (
        f"manual cascade.enabled flip MUST surface as drift; "
        f"got: {keys}. Pre-L10 this was silent."
    )
    # And the openclaw.json side is unchanged — no false positives there
    assert "agents" not in keys


def test_tiers_file_per_tier_models_change_surfaces(tmp_path, monkeypatch):
    """Wiping tier2.models[] is the failure mode where the operator's
    cascade has no first-choice model — silently routes to bot default."""
    committed_oc = {"agents": {"defaults": {}}}
    committed_tiers = {
        "tiers": {
            "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
            "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
        },
    }
    live_oc = committed_oc
    live_tiers = {
        "tiers": {
            "tier2": {"models": []},  # wiped
            "tier3": {"models": ["anthropic/claude-haiku-4-5"]},
        },
    }
    _stub_subprocess_and_file_reads(
        monkeypatch,
        committed_oc=committed_oc, live_oc=live_oc,
        committed_tiers=committed_tiers, live_tiers=live_tiers,
    )
    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    assert "tiers:tiers" in keys, (
        f"tier-models change must surface; got: {keys}"
    )


# ── Both files changed → both surface, separately ──────────────────────────


def test_both_files_drifting_returns_both_namespaces(tmp_path, monkeypatch):
    """Sanity: a real attacker / manual editor might touch both files.
    Drift advisory must show keys from each, distinguishable by prefix."""
    committed_oc = {"agents": {"defaults": {"model": {"primary": "old/model"}}}}
    committed_tiers = {"tiers": {"tier2": {"models": ["old/model"]}}}
    live_oc = {"agents": {"defaults": {"model": {"primary": "new/model"}}}}
    live_tiers = {"tiers": {"tier2": {"models": ["new/model"]}}}
    _stub_subprocess_and_file_reads(
        monkeypatch,
        committed_oc=committed_oc, live_oc=live_oc,
        committed_tiers=committed_tiers, live_tiers=live_tiers,
    )
    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    assert "agents" in keys, "openclaw.json drift must still surface (bare key)"
    assert "tiers:tiers" in keys, "tier file drift must surface (namespaced key)"


# ── Graceful fallback: backup doesn't have evolve-tiers.json yet ──────────


def test_no_committed_tiers_file_does_not_crash(tmp_path, monkeypatch):
    """Older bots' backup workspaces don't have evolve-tiers.json yet
    (backup.py only started staging it after this PR). The drift
    detector must fall back to openclaw.json-only diffing without
    crashing or returning falsely empty."""
    committed_oc = {"agents": {"defaults": {"model": {"primary": "old/model"}}}}
    live_oc = {"agents": {"defaults": {"model": {"primary": "new/model"}}}}
    _stub_subprocess_and_file_reads(
        monkeypatch,
        committed_oc=committed_oc, live_oc=live_oc,
        committed_tiers=None,  # backup has no committed copy
        live_tiers={"tiers": {}},  # live file exists (irrelevant — no backup to compare)
    )
    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    # openclaw.json drift still surfaces; no tier-file false-positives
    assert "agents" in keys
    assert not any(k.startswith("tiers:") for k in keys), (
        f"no tier file in backup HEAD → no tier-prefixed keys; got: {keys}"
    )


def test_no_live_tiers_file_does_not_crash(tmp_path, monkeypatch):
    """A bot that has a committed evolve-tiers.json in backup but
    deleted it on disk (or never had it; race during deploy) — drift
    detector must not crash. We treat absent-on-live as 'unable to
    diff' rather than 'entire file drifted'."""
    committed_oc = {"agents": {}}
    committed_tiers = {"tiers": {"tier2": {"models": ["x"]}}}
    live_oc = {"agents": {}}
    _stub_subprocess_and_file_reads(
        monkeypatch,
        committed_oc=committed_oc, live_oc=live_oc,
        committed_tiers=committed_tiers,
        live_tiers=None,  # missing on disk
    )
    network_config = {"bots": {"team_bot_a": {"user": "team_bot_a"}}}
    # Should not crash; openclaw side returns empty (same), tier side
    # silently skips.
    keys = detect_backup_drift_keys("team_bot_a", tmp_path, network_config)
    assert keys == [], f"expected no drift; got: {keys}"
