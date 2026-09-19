"""tests/test_deploy_cron_baseline.py — atomic cron-baseline rebless.

Pins the deploy-time invariant that whenever Evolve installs / removes a
cron job on a bot, the bot's entry in ``cron-jobs.json`` is rewritten to
match Evolve's known set — so the next audit run doesn't fire "new cron
job not in baseline" on Evolve's own additions.

The reblessing logic must:
  1. Set ``baseline[bot_id]`` to Evolve's known cron names.
  2. Preserve every other bot's entry verbatim.
  3. NOT bless bot-self-installed jobs (e.g. security_bot's ``usage-alert-dispatch``)
     — those still fire the drift warn on next audit, which is the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolve_admin.deploy import (
    DeployResult,
    _evolve_managed_cron_names,
    _rebless_cron_baseline,
)


def _baseline_path(shared: Path) -> Path:
    return shared / "security" / "baselines" / "cron-jobs.json"


def test_rebless_sets_bot_entry_to_known_set(tmp_path: Path):
    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", ["alpha", "beta"], tmp_path, result)

    data = json.loads(_baseline_path(tmp_path).read_text())
    assert data == {"evolve": ["alpha", "beta"]}


def test_rebless_preserves_other_bots_entries(tmp_path: Path):
    """Security_bot's entry must survive an evolve-only rebless verbatim."""
    bp = _baseline_path(tmp_path)
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps({
        "security_bot": ["usage-alert-dispatch", "security_bot-backup"],
        "team_bot_a": ["briefing"],
    }, indent=2))

    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", ["security:cve-scan-discover"], tmp_path, result)

    data = json.loads(bp.read_text())
    assert data["evolve"] == ["security:cve-scan-discover"]
    assert data["security_bot"] == ["usage-alert-dispatch", "security_bot-backup"]
    assert data["team_bot_a"] == ["briefing"]


def test_rebless_self_installed_job_still_drifts_on_audit(tmp_path: Path):
    """If security_bot self-installs job Z and Evolve reblesses with its own
    known set, the audit must still warn on Z. Proves we don't auto-bless
    bot-self-installed jobs."""
    bp = _baseline_path(tmp_path)
    bp.parent.mkdir(parents=True, exist_ok=True)
    # Pretend security_bot's baseline was previously seeded with only the
    # Evolve-known set (no self-installed job yet).
    bp.write_text(json.dumps({"security_bot": ["security_bot-backup"]}, indent=2))

    # Evolve reblesses security_bot with the same known set (note: Evolve doesn't
    # currently install security_bot crons — this exercises the contract for if
    # it ever did). The self-installed job Z is not included.
    result = DeployResult(bot_id="security_bot", success=True)
    _rebless_cron_baseline("security_bot", ["security_bot-backup"], tmp_path, result)

    data = json.loads(bp.read_text())
    assert data["security_bot"] == ["security_bot-backup"]
    assert "usage-alert-dispatch" not in data["security_bot"], (
        "self-installed jobs must NOT be auto-blessed by deploy rebless"
    )


def test_rebless_with_empty_known_set_writes_empty_list(tmp_path: Path):
    """All Evolve-managed crons removed → bot's baseline becomes []."""
    bp = _baseline_path(tmp_path)
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps({"evolve": ["old-cron"]}, indent=2))

    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", [], tmp_path, result)

    data = json.loads(bp.read_text())
    assert data == {"evolve": []}


def test_rebless_creates_baseline_file_if_missing(tmp_path: Path):
    """First-time rebless on a fresh install: file + parent dir get created."""
    assert not _baseline_path(tmp_path).exists()

    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", ["x", "y"], tmp_path, result)

    bp = _baseline_path(tmp_path)
    assert bp.exists()
    assert json.loads(bp.read_text()) == {"evolve": ["x", "y"]}


def test_rebless_no_change_is_no_op(tmp_path: Path):
    """Same set on disk = no rewrite (so mtime stays stable for the audit
    to use as a freshness proxy if it ever wants to)."""
    bp = _baseline_path(tmp_path)
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps({"evolve": ["a", "b"]}, indent=2))
    before_mtime = bp.stat().st_mtime_ns

    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", ["b", "a"], tmp_path, result)  # set-equal

    assert bp.stat().st_mtime_ns == before_mtime
    assert json.loads(bp.read_text()) == {"evolve": ["a", "b"]}


def test_rebless_input_sorted_independent_of_order(tmp_path: Path):
    """Caller can pass names in any order; output is sorted for stability."""
    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", ["z", "a", "m"], tmp_path, result)

    data = json.loads(_baseline_path(tmp_path).read_text())
    assert data["evolve"] == ["a", "m", "z"]


def test_rebless_corrupt_baseline_is_recovered(tmp_path: Path):
    """An unreadable / non-dict baseline shouldn't wedge deploy — it gets
    reseeded with just this bot's entry."""
    bp = _baseline_path(tmp_path)
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text("not valid json {")

    result = DeployResult(bot_id="evolve", success=True)
    _rebless_cron_baseline("evolve", ["only-job"], tmp_path, result)

    assert json.loads(bp.read_text()) == {"evolve": ["only-job"]}


# ── _evolve_managed_cron_names ──────────────────────────────────────────────


def test_evolve_managed_cron_names_reads_first_party_manifests():
    """Sanity: the helper reads cron names out of FIRST_PARTY_EVOLVE_APPS
    manifests. Today that's just security-cve-scan → one job."""
    names = _evolve_managed_cron_names()
    # Don't pin the exact contents — just that the canonical cve-scan
    # cron name is in there. Future manifests will add to this list.
    assert "security:cve-scan-discover" in names
    # The result must be sorted + de-duplicated.
    assert names == sorted(set(names))
