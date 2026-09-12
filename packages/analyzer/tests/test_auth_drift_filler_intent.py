"""Integration tests for the auth_drift_filler intent-aware path.

Phase 2 of internal/spec-config-intent-system-2026-05-21.md.

The bug this prevents is exactly the silent-failure mode that motivates
the spec: the generator emits one revert proposal per deliberate deviation,
every sweep, forever — because its rule is "if drifted, propose revert"
with no consideration of intent. These tests run the generator end-to-end
against a Signal store, with intents written via the real ``set_intent``
helper, and confirm the proposals stop.

Behavioral check, not just unit — per
[feedback_two_pass_review_workflow](../../.claude/projects/-Users-pod_admin-GitHub-evolve/memory/feedback_two_pass_review_workflow.md):
the silent-failure mode this guards against is exactly the kind that needs
a behavioral verification, not just helper-level tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin.config_intent import set_intent  # noqa: E402
from generators.auth_drift_filler.observe import (  # noqa: E402
    AuthDriftFillerContext,
    observe,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


def _write_drift_signal(
    shared_dir: Path, *, bot_id: str, diffs: dict[str, dict],
    salt: str = "",
) -> str:
    scope = f"{bot_id}:{salt}" if salt else bot_id
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("permission_monitor",
                                  "perm_config_drift", scope),
        producer="permission_monitor",
        type="perm_config_drift",
        flavor="security",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: permission config drifted",
        details={"bot_id": bot_id, "diffs": diffs},
    )
    return sig.id


# ── Primary behavioral guard ────────────────────────────────────────────────


def test_observe_suppresses_proposal_when_intent_matches_observed(
        tmp_path: Path):
    """The MVP behavioral guarantee: an operator records intent →
    generator stops proposing the revert.

    This is the test that pins the 2026-05-24 triage fix. Pre-Phase-2 the
    generator would emit one proposal here. Post-Phase-2 it emits zero
    (the deviation matches a recorded intent)."""
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
        },
    )
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        depends_on={"plugin": "codex"},
        shared_dir=tmp_path,
    )
    proposals = observe(AuthDriftFillerContext(
        bot_id="team_bot_a", shared_dir=tmp_path,
    ))
    assert proposals == [], (
        "An active intent matching the observed value must suppress the "
        "revert proposal. Got: "
        + str([p.action.fields for p in proposals])
    )


def test_observe_still_emits_when_no_intent_recorded(tmp_path: Path):
    """Pre-existing legacy behavior: no intent → emit revert. Confirms
    Phase 2 doesn't accidentally suppress legitimate drift."""
    _write_drift_signal(
        tmp_path, bot_id="admin_bot",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
        },
    )
    proposals = observe(AuthDriftFillerContext(
        bot_id="admin_bot", shared_dir=tmp_path,
    ))
    assert len(proposals) == 1
    assert proposals[0].action.fields == {"tools.exec.security": "deny"}


def test_observe_emits_when_intent_value_does_not_match_observed(
        tmp_path: Path):
    """Intent records ``allowlist`` as intentional, but the bot's current
    value is ``full`` — a different deviation. The intent does NOT cover
    this case; the generator should still propose the revert."""
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
        },
    )
    set_intent(
        "team_bot_a", "tools.exec.security", "allowlist",
        reason="restricted exec for occasional shell ops",
        set_by="pod_admin (admin UI)",
        shared_dir=tmp_path,
    )
    proposals = observe(AuthDriftFillerContext(
        bot_id="team_bot_a", shared_dir=tmp_path,
    ))
    assert len(proposals) == 1
    assert proposals[0].action.fields == {"tools.exec.security": "deny"}


def test_observe_emits_partial_when_one_field_intentful_one_drift(
        tmp_path: Path):
    """Two-field drift signal: one field has an intent (suppress), the
    other doesn't (emit). Phase 2's per-field check is what makes this
    work — a per-signal "skip all if any intent matches" would be wrong."""
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
            "tools.fs.workspaceOnly": {"expected": True, "observed": False},
        },
    )
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        shared_dir=tmp_path,
    )
    proposals = observe(AuthDriftFillerContext(
        bot_id="team_bot_a", shared_dir=tmp_path,
    ))
    assert len(proposals) == 1
    assert proposals[0].action.fields == {"tools.fs.workspaceOnly": True}


# ── Audit-only signal emission + dedup ──────────────────────────────────────


def test_intent_match_emits_one_audit_signal(tmp_path: Path):
    """Spec §4.3: suppressed proposals get a once-per-intent audit-only
    Signal so the deliberate deviation has somewhere to surface."""
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
        },
    )
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        shared_dir=tmp_path,
    )
    observe(AuthDriftFillerContext(bot_id="team_bot_a", shared_dir=tmp_path))

    audit_signals = [
        s for s in signals_store.iter_active(
            tmp_path, producer="auth_drift_filler", state="firing",
        )
    ]
    assert len(audit_signals) == 1
    audit = audit_signals[0]
    assert audit.type == "perm_config_intent_audit"
    assert audit.bot_id == "team_bot_a"
    assert audit.details["field"] == "tools.exec.security"
    assert audit.details["intent_reason"] == "codex plugin requires exec"


def test_second_sweep_does_not_re_emit_audit_signal(tmp_path: Path):
    """Dedup memory prevents the audit-only signal from re-firing every
    sweep. ``visibility is good, pestering is not`` (spec §4.3)."""
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
        },
    )
    set_intent(
        "team_bot_a", "tools.exec.security", "full",
        reason="codex plugin requires exec",
        set_by="plugin_side_effect:codex",
        shared_dir=tmp_path,
    )
    observe(AuthDriftFillerContext(bot_id="team_bot_a", shared_dir=tmp_path))
    observe(AuthDriftFillerContext(bot_id="team_bot_a", shared_dir=tmp_path))
    observe(AuthDriftFillerContext(bot_id="team_bot_a", shared_dir=tmp_path))

    # The signal store dedups by signature too (find-or-create), so even
    # without the generator-memory log there's only one audit signal.
    # The dedup log additionally avoids the bump-of-existing churn —
    # observation_count stays at 1.
    audit_signals = [
        s for s in signals_store.iter_active(
            tmp_path, producer="auth_drift_filler", state="firing",
        )
    ]
    assert len(audit_signals) == 1
    assert audit_signals[0].observation_count == 1, (
        "Dedup memory should keep the generator from re-observing the "
        "audit signal on subsequent sweeps; observation_count >1 means "
        "the generator memory log isn't being consulted."
    )


# ── Fail-open guards ────────────────────────────────────────────────────────


def test_malformed_sidecar_falls_back_to_legacy_propose(tmp_path: Path):
    """A corrupted sidecar must not suppress a legitimate proposal. The
    fail-open direction: over-recommend rather than under-protect."""
    _write_drift_signal(
        tmp_path, bot_id="team_bot_a",
        diffs={
            "tools.exec.security": {"expected": "deny", "observed": "full"},
        },
    )
    sidecar = tmp_path / "config_intents" / "team_bot_a.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("not json {{{")
    proposals = observe(AuthDriftFillerContext(
        bot_id="team_bot_a", shared_dir=tmp_path,
    ))
    assert len(proposals) == 1
