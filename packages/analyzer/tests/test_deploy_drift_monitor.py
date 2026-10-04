"""tests/test_deploy_drift_monitor.py — deploy_drift_monitor producer tests."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import deploy_drift_monitor as ddm  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── detect_deploy_drift ─────────────────────────────────────────────────────


def _net(members=("team_bot_a", "team_bot_b", "admin_bot"), roles=None):
    roles = roles or {}
    return {
        "members": list(members),
        "bots": {m: {"role": roles.get(m, "member")} for m in members},
    }


def _install(versions):
    return {"bot_versions": {b: {"version": v} for b, v in versions.items()}}


def test_no_signal_when_current_version_unknown():
    """Without EVOLVE_VERSION we refuse to compare — fail-safe silent."""
    assert (
        ddm.detect_deploy_drift(_net(), _install({"team_bot_a": "v1"}), None) is None
    )


def test_no_signal_when_install_json_missing():
    """Fresh install — no install.json yet. Don't false-fire."""
    assert ddm.detect_deploy_drift(_net(), None, "v2") is None


def test_no_signal_when_all_bots_in_sync():
    install = _install({"team_bot_a": "v2", "team_bot_b": "v2", "admin_bot": "v2"})
    assert ddm.detect_deploy_drift(_net(), install, "v2") is None


def test_signal_lists_out_of_sync_bots():
    install = _install({"team_bot_a": "v1", "team_bot_b": "v2", "admin_bot": "v1"})
    spec = ddm.detect_deploy_drift(_net(), install, "v2")
    assert spec is not None
    assert spec["type"] == "deploy_drift"
    assert spec["scope"] == "pod"
    assert spec["severity"] == "warn"
    out = spec["details"]["out_of_sync"]
    out_ids = sorted(e["bot_id"] for e in out)
    assert out_ids == ["admin_bot", "team_bot_a"]
    assert spec["details"]["current_version"] == "v2"
    assert spec["details"]["affected_total"] == 2
    # Body should mention both bots and the upgrade command.
    assert "team_bot_a" in spec["body"] and "admin_bot" in spec["body"]
    assert "install-infra-jobs" in spec["body"]


def test_never_deployed_bots_are_called_out_separately():
    install = _install({"team_bot_a": "v2"})  # team_bot_b + admin_bot absent
    spec = ddm.detect_deploy_drift(_net(), install, "v2")
    assert spec is not None
    assert sorted(spec["details"]["never_deployed"]) == ["admin_bot", "team_bot_b"]
    assert spec["details"]["out_of_sync"] == []
    assert "never been deployed" in spec["title"].lower()


def test_mixed_drift_and_never_deployed_in_one_signal():
    install = _install({"team_bot_a": "v1"})  # team_bot_a behind, team_bot_b+admin_bot absent
    spec = ddm.detect_deploy_drift(_net(), install, "v2")
    assert spec is not None
    assert spec["details"]["affected_total"] == 3
    # Title surfaces both buckets when both are present.
    assert "behind" in spec["title"] and "never deployed" in spec["title"]


def test_primary_role_excluded():
    """Primary bots report local code version, not deploy state — exclude
    them so the primary's natural drift doesn't constantly fire."""
    install = _install({"team_bot_a": "v2", "evo": "v1"})  # evo is primary, drifted
    spec = ddm.detect_deploy_drift(
        _net(members=("team_bot_a", "evo"), roles={"evo": "primary"}),
        install,
        "v2",
    )
    assert spec is None  # evo excluded; team_bot_a is in sync


def test_signature_stable_across_calls():
    """Same condition → same signature so observe() dedupes."""
    install = _install({"team_bot_a": "v1", "team_bot_b": "v2"})
    spec_a = ddm.detect_deploy_drift(_net(), install, "v2")
    spec_b = ddm.detect_deploy_drift(_net(), install, "v2")
    assert spec_a is not None and spec_b is not None
    assert spec_a["signature"] == spec_b["signature"]


def test_signature_is_pod_scoped_not_per_bot():
    """One pod-wide signal regardless of how many bots drift."""
    a = ddm.detect_deploy_drift(_net(), _install({"team_bot_a": "v1"}), "v2")
    b = ddm.detect_deploy_drift(
        _net(members=("team_bot_c",)), _install({"team_bot_c": "v1"}), "v2"
    )
    assert a["signature"] == b["signature"]


# ── Direction-awareness (real YYYY.MMDD.PR version strings) ──────────────────
#
# Regression for the live fresh-pod alert that read "1 bot is running an older
# Evolve version ... deployed an older version: darwin: 2026.0623.3189" while
# the admin server was on 2026.0623.3187 — i.e. darwin was *newer*, not older.

_OLDER = "2026.0623.3187"
_NEWER = "2026.0623.3189"


def test_bot_ahead_of_admin_reads_as_newer_and_is_informational():
    """admin=3187, bot=3189 → bot is AHEAD → 'newer than admin', info-tier."""
    spec = ddm.detect_deploy_drift(
        _net(members=("darwin",)),
        _install({"darwin": _NEWER}),
        _OLDER,
    )
    assert spec is not None
    # Informational, not a warn — a bot ahead of admin is benign/self-resolving.
    assert spec["severity"] == "info"
    assert spec["flavor"] == "activity"
    assert spec["details"]["direction"] == "ahead"
    # Direction-aware wording: "newer", never "older"/"out of sync" in the title.
    assert "newer" in spec["title"].lower()
    assert "older" not in spec["title"].lower()
    assert "out of sync" not in spec["title"].lower()
    # The ahead bot lands in `ahead`, not `out_of_sync`.
    assert [e["bot_id"] for e in spec["details"]["ahead"]] == ["darwin"]
    assert spec["details"]["out_of_sync"] == []
    # Body explains the self-resolving admin-not-reloaded condition.
    assert "darwin" in spec["body"]
    assert "newer" in spec["body"].lower()


def test_bot_behind_admin_reads_as_older_and_out_of_sync():
    """admin=3189, bot=3187 → bot is BEHIND → 'older / out of sync', warn."""
    spec = ddm.detect_deploy_drift(
        _net(members=("darwin",)),
        _install({"darwin": _OLDER}),
        _NEWER,
    )
    assert spec is not None
    assert spec["severity"] == "warn"
    assert spec["flavor"] == "maintenance"
    assert spec["details"]["direction"] == "behind"
    assert "older" in spec["title"].lower()
    assert [e["bot_id"] for e in spec["details"]["out_of_sync"]] == ["darwin"]
    assert spec["details"]["ahead"] == []
    assert "deployed an older version" in spec["body"]


def test_mixed_behind_and_ahead_warns_and_lists_both():
    """A behind bot forces warn-tier even when another bot is ahead; the
    ahead bot is still surfaced in the body (and the `ahead` detail)."""
    spec = ddm.detect_deploy_drift(
        _net(members=("behind_bot", "ahead_bot")),
        _install({"behind_bot": _OLDER, "ahead_bot": _NEWER}),
        "2026.0623.3188",
    )
    assert spec is not None
    assert spec["severity"] == "warn"  # action-needed condition dominates
    assert spec["details"]["direction"] == "behind"
    assert [e["bot_id"] for e in spec["details"]["out_of_sync"]] == ["behind_bot"]
    assert [e["bot_id"] for e in spec["details"]["ahead"]] == ["ahead_bot"]
    assert "older" in spec["title"].lower()
    assert "ahead_bot" in spec["body"] and "behind_bot" in spec["body"]


def test_unparseable_versions_fall_back_to_behind():
    """Non-YYYY.MMDD.PR versions can't prove direction → treated as behind so
    the operator still gets a 'sync the bot' nudge (preserves legacy behavior)."""
    spec = ddm.detect_deploy_drift(
        _net(members=("team_bot_a",)),
        _install({"team_bot_a": "weird-sha"}),
        "v2",
    )
    assert spec is not None
    assert spec["severity"] == "warn"
    assert [e["bot_id"] for e in spec["details"]["out_of_sync"]] == ["team_bot_a"]
    assert spec["details"]["ahead"] == []


# ── End-to-end run() ─────────────────────────────────────────────────────────


def test_run_writes_signal_and_returns_kept(tmp_path):
    network = _net()
    install = _install({"team_bot_a": "v1", "team_bot_b": "v2", "admin_bot": "v1"})
    kept, n_fired, n_resolved = ddm.run(
        tmp_path,
        network,
        install_loader=lambda _sd: install,
        version_loader=lambda: "v2",
    )
    assert n_fired == 1
    assert n_resolved == 0
    assert len(kept) == 1
    sigs = list(signals_store.iter_active(tmp_path, producer="deploy_drift_monitor"))
    assert len(sigs) == 1
    assert sigs[0].type == "deploy_drift"


def test_run_sweep_resolve_archives_when_pod_sync_recovers(tmp_path):
    network = _net()
    # Run 1: drift fires.
    install_drifted = _install({"team_bot_a": "v1", "team_bot_b": "v2", "admin_bot": "v1"})
    kept1, _, _ = ddm.run(
        tmp_path,
        network,
        install_loader=lambda _sd: install_drifted,
        version_loader=lambda: "v2",
    )
    assert len(kept1) == 1

    # Run 2: every bot now in sync.
    install_synced = _install({"team_bot_a": "v2", "team_bot_b": "v2", "admin_bot": "v2"})
    kept2, n_fired2, n_resolved2 = ddm.run(
        tmp_path,
        network,
        install_loader=lambda _sd: install_synced,
        version_loader=lambda: "v2",
    )
    assert kept2 == set()
    assert n_fired2 == 0
    assert n_resolved2 == 1
    assert (
        list(signals_store.iter_active(tmp_path, producer="deploy_drift_monitor"))
        == []
    )


def test_run_dry_run_does_not_write(tmp_path):
    install = _install({"team_bot_a": "v1"})
    kept, n_fired, _ = ddm.run(
        tmp_path,
        _net(members=("team_bot_a",)),
        install_loader=lambda _sd: install,
        version_loader=lambda: "v2",
        dry_run=True,
    )
    assert n_fired == 1
    assert (
        list(signals_store.iter_active(tmp_path, producer="deploy_drift_monitor"))
        == []
    )


def test_severity_default_is_warn():
    from signals.producer_severity import default_severity
    assert default_severity("deploy_drift_monitor") == "warn"
