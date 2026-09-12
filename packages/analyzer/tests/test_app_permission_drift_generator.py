"""Tests for generators.app_permission_drift.

Spec: internal/spec-app-permission-drift-2026-05-25.md (B.1).

Tests use plain-dict Signal fixtures (the make_proposals factory reads
via getattr-or-dict, so dicts work as drop-in stand-ins) and assert
expected Proposal shape per Signal kind.

The end-to-end observe(ctx) test injects a fake signal store via
monkeypatch so we don't depend on a real {shared_dir}/signals/ tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from generators.app_permission_drift.observe import (
    AppPermissionDriftContext,
    observe,
)
from generators.app_permission_drift.signal_proposals import (
    KIND_ALLOWED_NOT_DECLARED,
    KIND_DECLARED_MISSING_FILE,
    KIND_DECLARED_NOT_ALLOWED,
    KIND_WORKSPACE_ORPHAN_SCRIPT,
    KIND_WORKSPACE_WALK_TRUNCATED,
    make_proposals,
)
from schema.proposal import (
    Investigation,
    UpdateExecApproval,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _sig(
    kind: str,
    *,
    bot_id: str = "team_bot_a",
    pattern: str = "scripts/run.py",
    current_mode: str = "full",
    role: str = "member",
    app_id: str | None = "i-task",
    app_name: str | None = "Task App",
    sig_id: str = "sig-abc123",
    rationale: str = "test rationale",
) -> dict:
    return {
        "id": sig_id,
        "bot_id": bot_id,
        "type": "app_permission_drift",
        "details": {
            "kind": kind,
            "pattern": pattern,
            "current_mode": current_mode,
            "role": role,
            "app_id": app_id,
            "app_name": app_name,
            "rationale": rationale,
        },
    }


# ── declared_not_allowed ─────────────────────────────────────────────────────


def test_declared_not_allowed_emits_UpdateExecApproval_add_in_allowlist_mode():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED,
              current_mode="allowlist",
              pattern="ops/tools/unified_task_system.py")
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert isinstance(p.action, UpdateExecApproval)
    assert p.action.operation == "add"
    assert p.action.bot_id == "team_bot_a"
    assert p.action.pattern == "ops/tools/unified_task_system.py"
    assert p.action.agent_id == "main"
    assert p.action.scope == "agent"
    assert p.urgency == "critical"
    # Body mentions the allowlist-blocked situation
    assert "cannot run" in p.problem
    assert "allowlist mode" in p.problem


def test_declared_not_allowed_emits_UpdateExecApproval_add_in_full_mode_with_info_urgency():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED, current_mode="full")
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert isinstance(p.action, UpdateExecApproval)
    assert p.action.operation == "add"
    assert p.urgency == "improvement"
    # Body should note the preemptive framing
    assert "full mode" in p.problem
    assert "preemptively" in p.problem or "seed" in p.admin_surface_summary.lower()


def test_declared_not_allowed_now_emits_for_primary_bot():
    """Phase E.4 (2026-05-25) removed the primary-bot guard from
    ``_declared_not_allowed_proposal``. After evo cuts over to the
    unprivileged ``evo`` macOS user (Phase E.2.b), allowlist-mutation
    proposals against primary bots are no longer special-cased — the
    generator treats them like any member bot.
    """
    # Primary bot in full mode (post-E.4 default) gets a proposal.
    sig = _sig(KIND_DECLARED_NOT_ALLOWED,
              bot_id="evolve", role="primary",
              current_mode="full")
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    assert isinstance(proposals[0].action, UpdateExecApproval)
    assert proposals[0].action.operation == "add"

    # Same goes for a non-evo primary
    sig2 = _sig(KIND_DECLARED_NOT_ALLOWED,
               bot_id="team_bot_a", role="primary",
               current_mode="full")
    assert len(make_proposals(sig2)) == 1


def test_declared_not_allowed_carries_motivating_signal_id():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED, sig_id="sig-xyz999")
    proposals = make_proposals(sig)
    assert proposals[0].motivating_signals == ["sig-xyz999"]


def test_declared_not_allowed_provenance_and_risk_tag():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED)
    p = make_proposals(sig)[0]
    assert p.generator_id == "app_permission_drift"
    assert p.dimension == "safety"
    assert p.provenance.technique == "app_permission_drift.declared_not_allowed"
    assert p.provenance.signals["bot_id"] == "team_bot_a"
    assert p.provenance.signals["pattern"] == "scripts/run.py"
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "auto"
    assert "auth_config" in p.risk_tag.touches


# ── allowed_not_declared ─────────────────────────────────────────────────────


def test_allowed_not_declared_emits_UpdateExecApproval_revoke():
    sig = _sig(
        KIND_ALLOWED_NOT_DECLARED,
        current_mode="allowlist",
        pattern="scripts/orphan.py",
        app_id=None, app_name=None,
    )
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert isinstance(p.action, UpdateExecApproval)
    assert p.action.operation == "revoke"
    assert p.action.pattern == "scripts/orphan.py"
    assert p.urgency == "hygiene"
    # Body mentions the false-positive escape hatch
    assert "operator-set" in p.problem


def test_allowed_not_declared_now_emits_for_primary_bot():
    """Phase E.4 (2026-05-25) — symmetric with
    ``_declared_not_allowed_proposal``: primary-bot guard removed
    after the evo account separation closed the underlying risk.
    """
    sig = _sig(KIND_ALLOWED_NOT_DECLARED,
              bot_id="evolve", role="primary",
              current_mode="allowlist")
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    assert proposals[0].action.operation == "revoke"


# ── workspace_orphan_script ──────────────────────────────────────────────────


def test_workspace_orphan_script_emits_Investigation():
    sig = _sig(KIND_WORKSPACE_ORPHAN_SCRIPT,
              pattern="scripts/undeclared.py",
              app_id=None, app_name=None,
              current_mode="full")
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert isinstance(p.action, Investigation)
    assert "scripts/undeclared.py" in p.action.context
    assert p.urgency == "hygiene"


def test_workspace_orphan_script_emits_for_primary_bot_too():
    """Investigations are always emitted regardless of role — operator
    visibility is the point. Only allowlist-mutation kinds are skipped."""
    sig = _sig(KIND_WORKSPACE_ORPHAN_SCRIPT,
              bot_id="evolve", role="primary",
              pattern="scripts/x.py", app_id=None, app_name=None)
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    assert isinstance(proposals[0].action, Investigation)


def test_workspace_orphan_script_body_mode_aware():
    """Body differs in allowlist vs. full mode."""
    sig_allow = _sig(KIND_WORKSPACE_ORPHAN_SCRIPT, current_mode="allowlist",
                    pattern="x.py", app_id=None, app_name=None)
    sig_full = _sig(KIND_WORKSPACE_ORPHAN_SCRIPT, current_mode="full",
                   pattern="x.py", app_id=None, app_name=None)
    p_allow = make_proposals(sig_allow)[0]
    p_full = make_proposals(sig_full)[0]
    assert "allowlist mode" in p_allow.problem
    assert "full mode" in p_full.problem
    assert p_allow.problem != p_full.problem


# ── declared_missing_file ────────────────────────────────────────────────────


def test_declared_missing_file_emits_Investigation():
    sig = _sig(KIND_DECLARED_MISSING_FILE,
              pattern="ghost/missing.py")
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    p = proposals[0]
    assert isinstance(p.action, Investigation)
    assert "ghost/missing.py" in p.action.context
    assert "Stale declaration" in p.action.context


# ── workspace_walk_truncated ─────────────────────────────────────────────────


def test_workspace_walk_truncated_emits_Investigation():
    sig = _sig(KIND_WORKSPACE_WALK_TRUNCATED,
              pattern="<workspace>", app_id=None, app_name=None)
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    assert isinstance(proposals[0].action, Investigation)


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_unknown_kind_returns_empty():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED)
    sig["details"]["kind"] = "some-future-finding-kind"
    assert make_proposals(sig) == []


def test_missing_pattern_returns_empty():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED)
    sig["details"]["pattern"] = ""
    assert make_proposals(sig) == []


def test_missing_bot_id_returns_empty():
    sig = _sig(KIND_DECLARED_NOT_ALLOWED)
    sig["bot_id"] = ""
    assert make_proposals(sig) == []


def test_signal_as_object_not_dict_also_works():
    """make_proposals reads via getattr-or-dict so a real Signal dataclass
    instance works the same as a fixture dict. Mock with SimpleNamespace."""
    from types import SimpleNamespace
    sig = SimpleNamespace(
        id="sig-from-obj",
        bot_id="team_bot_a",
        type="app_permission_drift",
        details={
            "kind": KIND_DECLARED_NOT_ALLOWED,
            "pattern": "scripts/foo.py",
            "current_mode": "allowlist",
            "role": "member",
            "app_id": "i-x", "app_name": "X",
            "rationale": "test",
        },
    )
    proposals = make_proposals(sig)
    assert len(proposals) == 1
    assert proposals[0].motivating_signals == ["sig-from-obj"]


# ── observe(ctx) — end-to-end with real signal store ─────────────────────────
# Pattern borrowed from tests/test_auth_drift_filler.py: rather than mocking
# signals.store (which fragile-ly depends on import ordering across tests),
# write real Signals to a tmp_path shared_dir and let observe() iterate them.


def _write_signal(
    shared_dir: Path,
    *,
    kind: str,
    bot_id: str = "team_bot_a",
    pattern: str = "scripts/run.py",
    current_mode: str = "full",
    role: str = "member",
    app_id: str | None = "i-task",
    app_name: str | None = "Task App",
    producer: str = "app_manifest_monitor",
    sig_type: str = "app_permission_drift",
    signature_salt: str = "",
):
    """Write a real Signal to the tmp shared_dir for observe() to read."""
    from signals import store as signals_store
    from schema.signal import make_signature
    scope_key = f"{bot_id}:{kind}:{pattern}"
    if signature_salt:
        scope_key += f":{signature_salt}"
    return signals_store.observe(
        shared_dir,
        signature=make_signature(producer, sig_type, scope_key),
        producer=producer,
        type=sig_type,
        flavor="maintenance",
        severity="info",
        scope="bot",
        bot_id=bot_id,
        title=f"test signal for {kind}",
        details={
            "kind": kind,
            "pattern": pattern,
            "current_mode": current_mode,
            "role": role,
            "app_id": app_id,
            "app_name": app_name,
            "rationale": "test fixture",
        },
    )


def test_observe_filters_to_app_manifest_monitor_producer_and_correct_bot(tmp_path: Path):
    """observe() only picks up firing signals from our producer for our bot."""
    # Matching signal — should fire
    sig = _write_signal(tmp_path, kind=KIND_DECLARED_NOT_ALLOWED,
                       current_mode="allowlist",
                       pattern="ops/match.py", signature_salt="match")
    # Wrong producer — should be skipped
    _write_signal(tmp_path, kind=KIND_DECLARED_NOT_ALLOWED,
                 pattern="ops/wrong-prod.py",
                 producer="some_other_monitor", signature_salt="wp")
    # Wrong type — should be skipped
    _write_signal(tmp_path, kind=KIND_DECLARED_NOT_ALLOWED,
                 pattern="ops/wrong-type.py",
                 sig_type="some_other_signal", signature_salt="wt")
    # Wrong bot_id — should be skipped
    _write_signal(tmp_path, kind=KIND_DECLARED_NOT_ALLOWED,
                 bot_id="admin_bot", pattern="ops/wrong-bot.py", signature_salt="wb")

    proposals = observe(AppPermissionDriftContext(bot_id="team_bot_a", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].bot_id == "team_bot_a"
    assert proposals[0].motivating_signals == [sig.id]
    assert isinstance(proposals[0].action, UpdateExecApproval)
    assert proposals[0].action.pattern == "ops/match.py"


def test_observe_dispatches_each_kind_through_correct_factory(tmp_path: Path):
    """A mix of finding kinds → one proposal per signal, right action per kind."""
    _write_signal(tmp_path, kind=KIND_DECLARED_NOT_ALLOWED,
                 current_mode="allowlist", pattern="a.py", signature_salt="a")
    _write_signal(tmp_path, kind=KIND_ALLOWED_NOT_DECLARED,
                 current_mode="allowlist", pattern="b.py",
                 app_id=None, app_name=None, signature_salt="b")
    _write_signal(tmp_path, kind=KIND_WORKSPACE_ORPHAN_SCRIPT,
                 pattern="c.py", app_id=None, app_name=None, signature_salt="c")

    proposals = observe(AppPermissionDriftContext(bot_id="team_bot_a", shared_dir=tmp_path))
    by_pattern = {
        getattr(p.action, "pattern", None) or
        # Investigation has no pattern; key off the trigger observation
        p.trigger_observations[0]: p
        for p in proposals
    }
    assert len(proposals) == 3
    # Verify each kind got the right action
    kinds_to_action_type = {
        p.trigger_observations[0].split(":")[2]: type(p.action).__name__
        for p in proposals
    }
    assert kinds_to_action_type[KIND_DECLARED_NOT_ALLOWED] == "UpdateExecApproval"
    assert kinds_to_action_type[KIND_ALLOWED_NOT_DECLARED] == "UpdateExecApproval"
    assert kinds_to_action_type[KIND_WORKSPACE_ORPHAN_SCRIPT] == "Investigation"


# ── Charter invariants ───────────────────────────────────────────────────────


def test_emitted_actions_only_in_charter_allowlist():
    """The charter declares allowlist: [UpdateExecApproval, Investigation].
    Every kind we generate must only emit those two action types."""
    allowed = {"UpdateExecApproval", "Investigation"}
    for kind in (
        KIND_DECLARED_NOT_ALLOWED,
        KIND_ALLOWED_NOT_DECLARED,
        KIND_WORKSPACE_ORPHAN_SCRIPT,
        KIND_DECLARED_MISSING_FILE,
        KIND_WORKSPACE_WALK_TRUNCATED,
    ):
        sig = _sig(kind, current_mode="allowlist",
                  pattern="x.py" if kind != KIND_WORKSPACE_WALK_TRUNCATED else "<workspace>",
                  app_id=None, app_name=None)
        for p in make_proposals(sig):
            assert p.action.kind in allowed, (
                f"kind={kind} emitted disallowed action {p.action.kind}"
            )
