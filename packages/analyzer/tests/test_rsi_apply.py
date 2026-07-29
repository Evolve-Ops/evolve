"""tests/test_rsi_apply.py — Applier tests (capture/apply/revert round-trip)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter import apply as arbiter_apply  # noqa: E402
from arbiter.appliers import get_applier, known_action_kinds  # noqa: E402
from arbiter.state_machine import transition  # noqa: E402
from schema.proposal import (  # noqa: E402
    AddSignalCollection,
    Claim,
    ConfigPatch,
    Investigation,
    RevertPlan,
    WorkflowInstruction,
)
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
    make_workflow_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────


def test_known_action_kinds_includes_l1_set():
    known = set(known_action_kinds())
    assert {"Investigation", "ConfigPatch", "WorkflowInstruction"}.issubset(known)


def test_get_applier_raises_for_unknown():
    with pytest.raises(KeyError):
        get_applier("FakeKind")


# ─────────────────────────────────────────────────────────────────────────────
# InvestigationApplier
# ─────────────────────────────────────────────────────────────────────────────


def test_investigation_apply_is_noop():
    applier = get_applier("Investigation")
    action = Investigation(context="see logs")
    result = applier.apply(action, "team_bot_a")
    assert result.ok
    assert result.details["bot_id"] == "team_bot_a"


def test_investigation_revert_is_noop():
    applier = get_applier("Investigation")
    snapshot = applier.capture_snapshot(Investigation(context="x"), "team_bot_a")
    result = applier.revert(snapshot, "team_bot_a")
    assert result.ok


# ─────────────────────────────────────────────────────────────────────────────
# AddSignalCollectionApplier (Phase 5 — SignalGapProposal review path)
# ─────────────────────────────────────────────────────────────────────────────


def test_add_signal_collection_is_registered():
    assert "AddSignalCollection" in known_action_kinds()


def test_add_signal_collection_apply_is_noop_with_context():
    applier = get_applier("AddSignalCollection")
    action = AddSignalCollection(
        producer="cost_watchdog",
        signal_type="tool_use_pattern",
        description="Need to know which tools heartbeats invoke.",
        suggested_data_shape={"tools": "list of str"},
        motivating_candidate_ids=["c1", "c2"],
        estimated_impact="Would clarify 3 candidates.",
    )
    result = applier.apply(action, "<pod>")
    assert result.ok
    assert result.details["producer"] == "cost_watchdog"
    assert result.details["signal_type"] == "tool_use_pattern"
    assert "cost_watchdog.tool_use_pattern" in result.message


def test_add_signal_collection_revert_is_noop():
    applier = get_applier("AddSignalCollection")
    action = AddSignalCollection(
        producer="x",
        signal_type="y",
        description="z",
    )
    snap = applier.capture_snapshot(action, "<pod>")
    assert snap["action_kind"] == "AddSignalCollection"
    result = applier.revert(snap, "<pod>")
    assert result.ok


def test_add_signal_collection_is_manual_completion_kind():
    """Operator marks complete after the engineer writes the monitor."""
    assert arbiter_apply.is_manual_completion_kind("AddSignalCollection")
    assert arbiter_apply.is_deferred_completion_kind("AddSignalCollection")
    # NOT an external-completion kind (no forge-style sweep watches it).
    assert not arbiter_apply.is_external_completion_kind("AddSignalCollection")


# ─────────────────────────────────────────────────────────────────────────────
# ConfigPatchApplier
# ─────────────────────────────────────────────────────────────────────────────


def test_config_patch_set_on_new_file(tmp_path):
    target = tmp_path / "cfg.json"
    applier = get_applier("ConfigPatch")

    action = ConfigPatch(
        target_path=f"{target}::ui.theme",
        operation="set",
        value="dark",
    )
    snapshot = applier.capture_snapshot(action, "team_bot_a")
    assert not snapshot["file_existed_before"]

    result = applier.apply(action, "team_bot_a")
    assert result.ok

    data = json.loads(target.read_text())
    assert data == {"ui": {"theme": "dark"}}

    # Revert deletes the file since it didn't exist before
    revert_result = applier.revert(snapshot, "team_bot_a")
    assert revert_result.ok
    assert not target.exists()


def test_config_patch_set_on_existing_file(tmp_path):
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}, "keep": 1}))
    applier = get_applier("ConfigPatch")

    action = ConfigPatch(
        target_path=f"{target}::ui.theme", operation="set", value="dark"
    )
    snapshot = applier.capture_snapshot(action, "team_bot_a")
    assert snapshot["existed_before"]
    assert snapshot["prior_value"] == "light"

    result = applier.apply(action, "team_bot_a")
    assert result.ok
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "dark"

    # Revert restores prior
    revert = applier.revert(snapshot, "team_bot_a")
    assert revert.ok
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "light"
    assert data["keep"] == 1


def test_config_patch_unset(tmp_path):
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "dark"}, "keep": 1}))
    applier = get_applier("ConfigPatch")

    action = ConfigPatch(
        target_path=f"{target}::ui.theme", operation="unset"
    )
    snapshot = applier.capture_snapshot(action, "team_bot_a")

    result = applier.apply(action, "team_bot_a")
    assert result.ok
    data = json.loads(target.read_text())
    assert "theme" not in data["ui"]
    assert data["keep"] == 1

    revert = applier.revert(snapshot, "team_bot_a")
    assert revert.ok
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "dark"


def test_config_patch_merge(tmp_path):
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"settings": {"a": 1, "b": 2}}))
    applier = get_applier("ConfigPatch")

    action = ConfigPatch(
        target_path=f"{target}::settings",
        operation="merge",
        value={"b": 20, "c": 30},
    )
    snapshot = applier.capture_snapshot(action, "team_bot_a")

    result = applier.apply(action, "team_bot_a")
    assert result.ok
    data = json.loads(target.read_text())
    assert data["settings"] == {"a": 1, "b": 20, "c": 30}

    revert = applier.revert(snapshot, "team_bot_a")
    assert revert.ok
    data = json.loads(target.read_text())
    assert data["settings"] == {"a": 1, "b": 2}


def test_config_patch_merge_rejects_non_dict_value(tmp_path):
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({}))
    applier = get_applier("ConfigPatch")
    action = ConfigPatch(
        target_path=f"{target}::x", operation="merge", value="not a dict"
    )
    result = applier.apply(action, "team_bot_a")
    assert not result.ok


def test_config_patch_refuses_bot_openclaw_json_path():
    """Defensive guard: ConfigPatchApplier must NOT operate on bot
    openclaw.json paths — those route to UpdatePermissionConfigApplier
    (the canonical /tmp + sudo helper). The generic L1 patcher's
    mkstemp-in-place pattern fails on member bots whose .openclaw/
    is bot-owned + evolve-read-only. See
    docs/diagnosis-openclaw-json-write-regression-2026-05-21.md."""
    applier = get_applier("ConfigPatch")
    action = ConfigPatch(
        target_path="/Users/admin_bot/.openclaw/openclaw.json::tools.exec.security",
        operation="set",
        value="deny",
    )
    result = applier.apply(action, "admin_bot")
    assert not result.ok
    assert "UpdatePermissionConfig" in result.message


def test_config_patch_allows_non_openclaw_paths(tmp_path):
    """Negative case for the openclaw.json guard: any other path still
    works normally (this is what keeps the generic L1 patcher useful
    for its actual use cases)."""
    target = tmp_path / "general-config.json"
    applier = get_applier("ConfigPatch")
    action = ConfigPatch(
        target_path=f"{target}::ui.theme", operation="set", value="dark",
    )
    result = applier.apply(action, "team_bot_a")
    assert result.ok


# ─────────────────────────────────────────────────────────────────────────────
# WorkflowInstructionApplier
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_rejects_absolute_path():
    applier = get_applier("WorkflowInstruction")
    with pytest.raises(ValueError):
        applier.capture_snapshot(
            WorkflowInstruction(bot_id="team_bot_a", path="/etc/passwd", content="bad"),
            "team_bot_a",
        )


def test_workflow_rejects_escape_via_dotdot():
    applier = get_applier("WorkflowInstruction")
    # The harness WorkflowInstruction path is under the bot's workspace.
    # We can't easily write outside it without the applier raising.
    with pytest.raises(ValueError):
        applier.capture_snapshot(
            WorkflowInstruction(
                bot_id="team_bot_a", path="../../../etc/passwd", content="bad"
            ),
            "team_bot_a",
        )


# ─────────────────────────────────────────────────────────────────────────────
# arbiter.apply — end-to-end apply step
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_investigation_lands_in_applied_for_manual_completion(tmp_path):
    """Investigation is a manual-completion kind: applier no-ops and the
    proposal stays in ``applied`` (= In Process queue) awaiting an explicit
    Mark complete from the operator. NOT auto-promoted to succeeded."""
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_human", actor="user")

    outcome = arbiter_apply.apply(p)
    assert outcome.ok
    assert p.status == "applied"
    assert "succeeded" not in [h.to_status for h in p.history]


def test_apply_manual_completion_kinds_known():
    """Sanity: the manual-completion set covers Investigation and
    WorkflowInstruction — the kinds that route through the In Process queue
    rather than auto-succeeding."""
    from arbiter.apply import is_manual_completion_kind

    assert is_manual_completion_kind("Investigation")
    assert is_manual_completion_kind("WorkflowInstruction")
    assert not is_manual_completion_kind("ConfigPatch")
    assert not is_manual_completion_kind("TierAdjustment")
    assert not is_manual_completion_kind("ManifestUpdate")


def test_apply_claimless_non_manual_kind_auto_succeeds(tmp_path):
    """Edge case: a claim-less ConfigPatch (or any non-manual kind without
    a claim) still auto-promotes to succeeded — the applier did the work
    and there's nothing for the operator to do offline."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
    )
    p.claim = None  # strip the claim that the helper attaches
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_human", actor="user")

    outcome = arbiter_apply.apply(p)
    assert outcome.ok
    assert p.status == "succeeded"


def test_apply_claimful_proposal_stays_in_applied(tmp_path):
    """ConfigPatch with a claim does NOT auto-promote to succeeded — the
    verify daemon owns that transition once the claim window expires."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
    )
    assert p.claim is not None  # sanity
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_human", actor="user")

    outcome = arbiter_apply.apply(p)
    assert outcome.ok
    assert p.status == "applied"
    # Daemon-owned transition, not apply.py's job.
    assert "succeeded" not in [h.to_status for h in p.history]


def test_apply_rejects_wrong_initial_state():
    p = make_investigation_proposal()
    # Still in draft
    outcome = arbiter_apply.apply(p)
    assert not outcome.ok
    assert "approved_auto" in outcome.message or "approved_human" in outcome.message


def test_apply_config_patch_populates_revert_plan(tmp_path):
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")

    outcome = arbiter_apply.apply(p)
    assert outcome.ok
    assert p.revert_on_failure is not None
    assert p.revert_on_failure.before_snapshot["prior_value"] == "light"

    # Verify the applier's revert restores
    applier = get_applier("ConfigPatch")
    revert = applier.revert(p.revert_on_failure.before_snapshot, "team_bot_a")
    assert revert.ok
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "light"


# ─────────────────────────────────────────────────────────────────────────────
# Breaker suppression — spec §5.5 "don't fight the breaker"
# ─────────────────────────────────────────────────────────────────────────────


def _trip(shared_dir, scope, breaker_type):
    """Trip an indefinite breaker for the suppression tests."""
    from breakers import store as _bstore
    return _bstore.trip(
        shared_dir=shared_dir, scope=scope, breaker_type=breaker_type,
        duration=None, initiated_by="test", reason="suppression test",
    )


def test_apply_deferred_when_target_bot_has_full_breaker(tmp_path):
    """A ConfigPatch against a bot with L2 (full) breaker tripped is
    deferred — the applier never runs, the proposal stays at
    approved_*, and the next sweep picks it up."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
        bot_id="team_bot_a",
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")

    _trip(tmp_path, "team_bot_a", "full")

    outcome = arbiter_apply.apply(p, shared_dir=tmp_path)
    assert not outcome.ok
    assert outcome.deferred is True
    assert "full" in outcome.deferred_reason
    # Proposal status untouched.
    assert p.status == "approved_auto"
    # And the underlying file was NOT modified.
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "light"


def test_apply_NOT_deferred_when_cost_breaker_tripped(tmp_path):
    """L1 cost breaker leaves the gateway up; config writes still work
    and SHOULD proceed. Only L2 defers config_change."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
        bot_id="team_bot_a",
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")

    _trip(tmp_path, "team_bot_a", "cost")

    outcome = arbiter_apply.apply(p, shared_dir=tmp_path)
    assert outcome.ok
    assert outcome.deferred is False
    # Config write happened.
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "dark"


def test_apply_NOT_deferred_when_breaker_on_other_bot(tmp_path):
    """A breaker on security_bot must not defer an apply targeting team_bot_a."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
        bot_id="team_bot_a",
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")

    _trip(tmp_path, "security_bot", "full")

    outcome = arbiter_apply.apply(p, shared_dir=tmp_path)
    assert outcome.ok
    data = json.loads(target.read_text())
    assert data["ui"]["theme"] == "dark"


def test_apply_deferred_when_pod_full_breaker_tripped(tmp_path):
    """Pod-wide L2 defers config_change for every bot."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
        bot_id="team_bot_a",
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")

    _trip(tmp_path, "pod", "full")

    outcome = arbiter_apply.apply(p, shared_dir=tmp_path)
    assert not outcome.ok
    assert outcome.deferred is True


def test_apply_investigation_NOT_deferred_even_when_breaker_tripped(tmp_path):
    """Investigation is operator-facing — no config write. It should
    proceed even when the target bot has an L2 breaker tripped."""
    p = make_investigation_proposal()
    p.bot_id = "team_bot_a"   # ensure there IS a target bot for the lookup
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_human", actor="user")

    _trip(tmp_path, "team_bot_a", "full")

    outcome = arbiter_apply.apply(p, shared_dir=tmp_path)
    assert outcome.ok
    assert outcome.deferred is False


def test_apply_no_shared_dir_skips_suppression(tmp_path):
    """When the caller didn't pass shared_dir, we can't look up the
    breaker store — proceed normally (legacy callers that don't know
    about breakers shouldn't be silently regressed)."""
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps({"ui": {"theme": "light"}}))

    p = make_config_patch_proposal(
        target_path=f"{target}::ui.theme",
        value="dark",
        bot_id="team_bot_a",
    )
    transition(p, "pending", actor="arbiter")
    transition(p, "approved_auto", actor="arbiter")

    # No shared_dir passed → no suppression lookup.
    outcome = arbiter_apply.apply(p)
    assert outcome.ok
    assert outcome.deferred is False
