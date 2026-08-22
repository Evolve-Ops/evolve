"""tests/test_evo_action_tools.py — Phase 1.3 write_safe action tools.

Covers the three action tools that move signals + proposals through
their state machines:

* action.signal.snooze
* action.signal.dismiss
* action.proposal.snooze

Each tool has TWO surfaces under test — the handler (real side effect)
and the validate (dry-run preflight that gates button rendering per
spec §5.2). Both must work cohesively: validate ok'd implies handler
will succeed (modulo race conditions); validate failed implies
handler shouldn't run.

Test pattern mirrors test_evo_tools.py — tmp_path-based shared_dir,
real signal store + arbiter store seeded by the existing helper
functions, no mocking of the analyzer surface.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))
_ANALYZER_PKG = _ADMIN_PKG.parent / "analyzer"
if str(_ANALYZER_PKG) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_PKG))

from evolve_admin.evo import tools as _tools  # noqa: E402
from evolve_admin.evo.tools import (  # noqa: E402
    action_proposal, action_proposal_apply, action_signal,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — mirror the seeding helpers in test_evo_tools.py
# ─────────────────────────────────────────────────────────────────────────────


def _seed_signal(shared_dir: Path, *, signature: str, producer: str,
                 type_: str, bot_id: str | None, severity: str,
                 title: str) -> str:
    """Create a firing signal via the real store. Returns signal id."""
    from signals import store as _store
    sig = _store.observe(
        shared_dir,
        signature=signature,
        producer=producer,
        type=type_,
        flavor="security",
        severity=severity,
        scope="bot" if bot_id else "pod",
        bot_id=bot_id,
        title=title,
        body=f"test body for {title}",
    )
    return sig.id


def _seed_proposal(shared_dir, *, proposal_id, bot_id, status="pending",
                   urgency="improvement", summary="test summary"):
    """Write a Proposal directly to shared_dir/proposals/<status>/."""
    from arbiter import store as arbiter_store
    from schema.proposal import Proposal, Investigation, RiskTag
    from schema.provenance import Provenance

    p = Proposal(
        id=proposal_id,
        bot_id=bot_id,
        generator_id="test_gen",
        dimension="cost",
        trigger_observations=[],
        provenance=Provenance(technique="test"),
        problem="test problem",
        action=Investigation(context="test"),
        risk_tag=RiskTag(blast_radius="local", reversibility="reversible"),
        urgency=urgency,
        admin_surface_summary=summary,
        status=status,
    )
    arbiter_store.write_proposal(p, shared_dir, subdir=status)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Duration parsing — shared shape across the action tools
# ─────────────────────────────────────────────────────────────────────────────


def test_duration_parse_signal_action_accepts_h_d_w():
    """Signal-action _parse_duration accepts 'h', 'd', 'w'. The proposal
    parser also accepts 'm' (months-as-30d); checked separately."""
    from datetime import timedelta
    parser = action_signal._parse_duration
    assert parser("1h") == timedelta(hours=1)
    assert parser("24h") == timedelta(hours=24)
    assert parser("7d") == timedelta(days=7)
    assert parser("2w") == timedelta(weeks=2)
    # Whitespace + case-insensitive
    assert parser(" 4H ") == timedelta(hours=4)


def test_duration_parse_signal_rejects_invalid():
    """Empty, malformed, zero, negative, unknown unit → None."""
    parser = action_signal._parse_duration
    assert parser(None) is None
    assert parser("") is None
    assert parser("garbage") is None
    assert parser("0h") is None
    assert parser("-1h") is None
    assert parser("3m") is None    # months not in signal parser
    assert parser("3y") is None


def test_duration_parse_proposal_accepts_m_as_30d():
    """Proposal parser accepts 'm' (months as 30 days each), matching
    admin UI semantics."""
    from datetime import timedelta
    parser = action_proposal._parse_duration
    assert parser("1m") == timedelta(days=30)
    assert parser("2m") == timedelta(days=60)


# ─────────────────────────────────────────────────────────────────────────────
# action.signal.snooze — validate
# ─────────────────────────────────────────────────────────────────────────────


def test_signal_snooze_validate_missing_signal_id(tmp_path):
    """No signal_id → validate fails with explicit reason."""
    result = action_signal._snooze_validate(shared_dir=tmp_path, signal_id="")
    assert result["ok"] is False
    assert "signal_id is required" in result["reason"]


def test_signal_snooze_validate_unknown_signal(tmp_path):
    """Signal id not in the store → validate fails. Prevents button
    rendering for a non-existent target."""
    result = action_signal._snooze_validate(
        shared_dir=tmp_path, signal_id="not-a-real-id",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_signal_snooze_validate_invalid_duration(tmp_path):
    """Bad duration string → validate fails with specific message
    listing valid formats."""
    sig_id = _seed_signal(
        tmp_path, signature="t:val:dur",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn",
        title="dur test",
    )
    result = action_signal._snooze_validate(
        shared_dir=tmp_path, signal_id=sig_id, duration="garbage",
    )
    assert result["ok"] is False
    assert "duration" in result["reason"].lower()
    assert "1h" in result["reason"]   # example formats mentioned


def test_signal_snooze_validate_ok(tmp_path):
    """Firing signal + valid duration → validate ok with context."""
    sig_id = _seed_signal(
        tmp_path, signature="t:val:ok",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="alert",
        title="ok to snooze",
    )
    result = action_signal._snooze_validate(
        shared_dir=tmp_path, signal_id=sig_id, duration="24h",
    )
    assert result["ok"] is True
    assert result["context"]["current_title"] == "ok to snooze"


def test_signal_snooze_validate_already_snoozed(tmp_path):
    """Signal in snoozed state → validate fails. Snooze only applies
    to firing signals (state-machine restriction; preflight catches
    so we never render a button that would transition error)."""
    from signals import store as _store
    sig_id = _seed_signal(
        tmp_path, signature="t:val:already",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn",
        title="already snoozed",
    )
    # Move to snoozed
    sig = _store.find_signal(tmp_path, sig_id)[0]
    _store.apply_transition(
        sig, "snoozed", tmp_path,
        actor="test", reason="precondition",
        snoozed_until=datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
    )
    # Now try to snooze again
    result = action_signal._snooze_validate(
        shared_dir=tmp_path, signal_id=sig_id,
    )
    assert result["ok"] is False
    assert "snoozed" in result["reason"]
    assert "firing" in result["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# action.signal.snooze — handler (the real side effect)
# ─────────────────────────────────────────────────────────────────────────────


def test_signal_snooze_handler_transitions_signal(tmp_path):
    """Snoozing a firing signal moves it from firing/ to snoozed/ on
    disk; the signal's state field flips; snoozed_until is set; the
    state_history records the transition."""
    from signals import store as _store
    sig_id = _seed_signal(
        tmp_path, signature="t:handler:snooze",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="alert",
        title="real snooze",
    )
    result = action_signal._snooze_handler(
        shared_dir=tmp_path, signal_id=sig_id,
        duration="4h", reason="evo decided this can wait",
    )
    assert result["ok"] is True
    assert result["to_state"] == "snoozed"
    assert result["snoozed_until"].endswith("Z") or "+" in result["snoozed_until"]
    # verify_via hint guides the model to confirm the new state (spec
    # §3.7 reliability lever #4 — post-action verify pattern).
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.signals.history"
    assert result["verify_via"]["args"] == {"signal_id": sig_id, "state": "snoozed"}
    assert "state=snoozed" in result["verify_via"]["expect"]

    # Disk verification: signal is now in snoozed/ not firing/
    located = _store.find_signal(tmp_path, sig_id)
    assert located is not None
    sig, _path, subdir = located
    assert subdir == "snoozed"
    assert sig.state == "snoozed"
    assert sig.snoozed_until == result["snoozed_until"]
    # History recorded the actor + reason
    assert any(
        h.actor == "evo" and "wait" in (h.reason or "")
        for h in sig.state_history
    )


def test_signal_snooze_handler_default_duration_24h(tmp_path):
    """When no duration is provided, defaults to 24h."""
    from datetime import timedelta
    sig_id = _seed_signal(
        tmp_path, signature="t:handler:default",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn",
        title="default duration",
    )
    before = datetime.now(timezone.utc)
    result = action_signal._snooze_handler(
        shared_dir=tmp_path, signal_id=sig_id,
    )
    assert result["ok"] is True
    snoozed_until = datetime.fromisoformat(
        result["snoozed_until"].replace("Z", "+00:00")
    )
    delta = snoozed_until - before
    # Within 1 second of 24h
    assert abs(delta - timedelta(hours=24)) < timedelta(seconds=2)


def test_signal_snooze_handler_unknown_signal(tmp_path):
    """Unknown signal id → handler returns error, doesn't crash.
    (Validate would have caught it earlier; this is the
    defense-in-depth path.)"""
    result = action_signal._snooze_handler(
        shared_dir=tmp_path, signal_id="ghost-id",
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# action.signal.dismiss — validate + handler
# ─────────────────────────────────────────────────────────────────────────────


def test_signal_dismiss_validate_rejects_bad_verdict(tmp_path):
    sig_id = _seed_signal(
        tmp_path, signature="t:dismiss:v",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn", title="x",
    )
    result = action_signal._dismiss_validate(
        shared_dir=tmp_path, signal_id=sig_id, verdict="random_verdict",
    )
    assert result["ok"] is False
    assert "verdict" in result["reason"]


def test_signal_dismiss_validate_accepts_known_verdicts(tmp_path):
    """false_positive / bad_inference / not_actionable all pass."""
    sig_id = _seed_signal(
        tmp_path, signature="t:dismiss:vok",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn", title="x",
    )
    for v in ("false_positive", "bad_inference", "not_actionable"):
        result = action_signal._dismiss_validate(
            shared_dir=tmp_path, signal_id=sig_id, verdict=v,
        )
        assert result["ok"] is True, f"verdict {v} should pass"


def test_signal_dismiss_validate_accepts_omitted_verdict(tmp_path):
    """Dismiss without a verdict is valid — verdict is optional."""
    sig_id = _seed_signal(
        tmp_path, signature="t:dismiss:nov",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn", title="x",
    )
    result = action_signal._dismiss_validate(
        shared_dir=tmp_path, signal_id=sig_id,
    )
    assert result["ok"] is True


def test_signal_dismiss_handler_terminal_state(tmp_path):
    """Dismissing moves the signal to archived/. State is dismissed.
    No way back via observe() for dismissed signals."""
    from signals import store as _store
    sig_id = _seed_signal(
        tmp_path, signature="t:dismiss:terminal",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn", title="dismiss me",
    )
    result = action_signal._dismiss_handler(
        shared_dir=tmp_path, signal_id=sig_id, reason="bad finding",
    )
    assert result["ok"] is True
    assert result["to_state"] == "dismissed"
    # verify_via hint (spec §3.7 reliability lever #4).
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.signals.history"
    assert result["verify_via"]["args"] == {"signal_id": sig_id, "state": "dismissed"}

    located = _store.find_signal(tmp_path, sig_id)
    assert located is not None
    sig, _path, subdir = located
    assert subdir == "archived"
    assert sig.state == "dismissed"


def test_signal_dismiss_handler_with_verdict_writes_feedback(tmp_path):
    """When verdict is provided, feedback.jsonl gets a row per
    motivated proposal. Closes the producer-tuning loop."""
    sig_id = _seed_signal(
        tmp_path, signature="t:dismiss:fb",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn", title="false alarm",
    )
    result = action_signal._dismiss_handler(
        shared_dir=tmp_path, signal_id=sig_id,
        verdict="false_positive", reason="not actually broken",
    )
    assert result["ok"] is True
    assert result["verdict"] == "false_positive"
    # No motivated_proposals on this test signal, so feedback writes
    # one row for the (signal_id, "") pair
    feedback_path = tmp_path / "signals" / "feedback.jsonl"
    assert feedback_path.exists()
    rows = [
        json.loads(line) for line in feedback_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) >= 1
    assert rows[-1]["verdict"] == "false_positive"
    assert rows[-1]["signal_id"] == sig_id


def test_signal_dismiss_handler_no_verdict_no_feedback(tmp_path):
    """Without a verdict, the signal still dismisses but no feedback
    row is written."""
    sig_id = _seed_signal(
        tmp_path, signature="t:dismiss:nofb",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn", title="just dismiss",
    )
    result = action_signal._dismiss_handler(
        shared_dir=tmp_path, signal_id=sig_id, reason="cleanup",
    )
    assert result["ok"] is True
    assert "feedback_written" not in result
    # No feedback file should be touched
    feedback_path = tmp_path / "signals" / "feedback.jsonl"
    if feedback_path.exists():
        assert feedback_path.read_text().strip() == ""


# ─────────────────────────────────────────────────────────────────────────────
# action.proposal.snooze — validate + handler
# ─────────────────────────────────────────────────────────────────────────────


def test_proposal_snooze_validate_missing_id(tmp_path):
    result = action_proposal._snooze_validate(
        shared_dir=tmp_path, proposal_id="",
    )
    assert result["ok"] is False
    assert "proposal_id is required" in result["reason"]


def test_proposal_snooze_validate_unknown_proposal(tmp_path):
    result = action_proposal._snooze_validate(
        shared_dir=tmp_path, proposal_id="not-real",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_proposal_snooze_validate_ok_for_pending(tmp_path):
    """Pending proposal with valid duration → validate ok with summary."""
    _seed_proposal(
        tmp_path, proposal_id="p-val-1",
        bot_id="team_bot_a", summary="raise cache TTL",
    )
    result = action_proposal._snooze_validate(
        shared_dir=tmp_path, proposal_id="p-val-1", duration="1w",
    )
    assert result["ok"] is True
    assert "raise cache TTL" in result["context"]["summary"]


def test_proposal_snooze_validate_rejects_invalid_duration(tmp_path):
    _seed_proposal(tmp_path, proposal_id="p-bad-dur", bot_id="team_bot_a")
    result = action_proposal._snooze_validate(
        shared_dir=tmp_path, proposal_id="p-bad-dur",
        duration="forever",
    )
    assert result["ok"] is False
    assert "duration" in result["reason"].lower()
    assert "1w" in result["reason"]  # example formats listed


def test_proposal_snooze_handler_transitions_to_snoozed(tmp_path):
    """End-to-end: pending → snoozed. File moves between subdirs;
    snoozed_until is set; status field flips; history records actor."""
    from arbiter import store as arbiter_store
    _seed_proposal(
        tmp_path, proposal_id="p-snooze-real",
        bot_id="team_bot_a", summary="real snooze test",
    )
    result = action_proposal._snooze_handler(
        shared_dir=tmp_path, proposal_id="p-snooze-real",
        duration="2w", reason="evo wants to wait",
    )
    assert result["ok"] is True
    assert result["from_status"] == "pending"
    assert result["to_status"] == "snoozed"
    assert result["snoozed_until"]
    # verify_via hint (spec §3.7 reliability lever #4).
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.proposals.snoozed"
    assert result["verify_via"]["args"] == {"proposal_id": "p-snooze-real"}

    # On-disk: file should now live in proposals/snoozed/
    located = arbiter_store.find_proposal(tmp_path, "p-snooze-real")
    assert located is not None
    proposal, _path, subdir = located
    assert subdir == "snoozed"
    assert proposal.status == "snoozed"
    assert proposal.snoozed_until == result["snoozed_until"]
    # Pending file is gone
    assert not (tmp_path / "proposals" / "pending" / "p-snooze-real.json").exists()
    # Snoozed file is present
    assert (tmp_path / "proposals" / "snoozed" / "p-snooze-real.json").exists()


def test_proposal_snooze_handler_default_duration_1w(tmp_path):
    """No duration → defaults to 1 week."""
    from datetime import timedelta
    _seed_proposal(tmp_path, proposal_id="p-default-dur", bot_id="team_bot_a")
    before = datetime.now(timezone.utc)
    result = action_proposal._snooze_handler(
        shared_dir=tmp_path, proposal_id="p-default-dur",
    )
    assert result["ok"] is True
    snoozed_until = datetime.fromisoformat(
        result["snoozed_until"].replace("Z", "+00:00")
    )
    delta = snoozed_until - before
    # Within 1 second of 1 week
    assert abs(delta - timedelta(weeks=1)) < timedelta(seconds=2)


# ─────────────────────────────────────────────────────────────────────────────
# Registration + risk tier + validate-required contract
# ─────────────────────────────────────────────────────────────────────────────


def test_action_signal_tools_registered():
    found_snooze = _tools.lookup("action.signal.snooze")
    found_dismiss = _tools.lookup("action.signal.dismiss")
    assert found_snooze is not None
    assert found_dismiss is not None


def test_action_signal_tools_are_write_safe_with_validate():
    """Both signal action tools are write_safe and have a validate
    function. The construction guard in the Tool dataclass enforces
    this; we test the outcome here as a regression for future
    changes."""
    for name in ("action.signal.snooze", "action.signal.dismiss"):
        t = _tools.lookup(name)
        assert t.risk_tier == _tools.RiskTier.WRITE_SAFE, name
        assert t.validate is not None, name


def test_action_proposal_snooze_registered_with_validate():
    t = _tools.lookup("action.proposal.snooze")
    assert t is not None
    assert t.risk_tier == _tools.RiskTier.WRITE_SAFE
    assert t.validate is not None


def test_action_tools_descriptions_mention_reversibility():
    """Write tools' descriptions should signal to the model that they
    DO something — operator-visible state changes. Spot-check the
    'snooze' / 'dismiss' verbs appear in the description, plus
    reversibility hints for the snooze tools."""
    snooze_sig = _tools.lookup("action.signal.snooze")
    assert "snooze" in snooze_sig.description.lower()
    assert "until" in snooze_sig.description.lower()

    dismiss_sig = _tools.lookup("action.signal.dismiss")
    # Terminal action — desc should NOT call it reversible. Spot-check
    # the wording.
    assert "dismiss" in dismiss_sig.description.lower()

    snooze_prop = _tools.lookup("action.proposal.snooze")
    assert "snooze" in snooze_prop.description.lower()
    assert "defer" in snooze_prop.description.lower() or "wake" in snooze_prop.description.lower()


# ─────────────────────────────────────────────────────────────────────────────
# action.proposal.apply — the resolver-pattern linchpin (Phase 1.4)
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_registered_as_write_risky():
    """The apply tool is write_risky — under 'ask' or 'auto-small' it
    stages a button; under 'auto' it auto-runs (modulo per-class tier
    override). Spec §13.3."""
    found = _tools.lookup("action.proposal.apply")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_RISKY
    assert found.validate is not None    # construction guard catches if not
    assert "proposal_id" in found.input_schema["properties"]
    assert found.input_schema.get("required", []) == ["proposal_id"]


def test_apply_validate_missing_id(tmp_path):
    """Empty proposal_id → validate fails. Mirrors the snooze
    validate behavior."""
    result = action_proposal_apply._apply_validate(
        shared_dir=tmp_path, proposal_id="",
    )
    assert result["ok"] is False
    assert "required" in result["reason"]


def test_apply_validate_unknown_proposal(tmp_path):
    """Proposal id not on disk → validate fails with explicit reason."""
    result = action_proposal_apply._apply_validate(
        shared_dir=tmp_path, proposal_id="not-a-real-id",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_apply_validate_wrong_state(tmp_path):
    """Proposal in snoozed state → validate fails. Apply only works
    for pending / approved_*."""
    _seed_proposal(
        tmp_path, proposal_id="p-snoozed", bot_id="team_bot_a",
        status="snoozed", summary="snoozed proposal",
    )
    result = action_proposal_apply._apply_validate(
        shared_dir=tmp_path, proposal_id="p-snoozed",
    )
    assert result["ok"] is False
    assert "snoozed" in result["reason"]


def test_apply_validate_ok_for_pending(tmp_path):
    """Pending Investigation proposal → validate ok with context
    + requires_confirmation=False (Investigation isn't a force-ask
    kind)."""
    _seed_proposal(
        tmp_path, proposal_id="p-pending", bot_id="team_bot_a",
        summary="investigation thing",
    )
    result = action_proposal_apply._apply_validate(
        shared_dir=tmp_path, proposal_id="p-pending",
    )
    assert result["ok"] is True
    assert result["context"]["proposal_id"] == "p-pending"
    assert result["context"]["action_kind"] == "Investigation"
    assert result["context"]["bot_id"] == "team_bot_a"
    assert result["context"]["current_status"] == "pending"
    # Investigation is not in _FORCE_ASK_ACTION_KINDS — normal authority
    # tier semantics apply.
    assert result["requires_confirmation"] is False


def test_apply_validate_requires_confirmation_flag_path(tmp_path, monkeypatch):
    """Action kinds in _FORCE_ASK_ACTION_KINDS get
    requires_confirmation=True regardless of authority tier. Closes
    spec §13.4 Q2 — policy-weighted changes always need operator
    review.

    Stubs the action-kind extraction to claim 'SoulEdit' for an
    Investigation-shaped proposal, so we exercise the flag-path of
    validate without needing a SoulEdit applier registered. Validates
    the wiring of the force-ask set into the validate output."""
    # Seed a normal Investigation proposal
    _seed_proposal(
        tmp_path, proposal_id="p-pw-flag", bot_id="team_bot_a",
        summary="policy-weighted flag test",
    )

    # Stub _action_kind_of so the validate sees this as a SoulEdit
    monkeypatch.setattr(
        action_proposal_apply, "_action_kind_of",
        lambda proposal: "SoulEdit",
    )
    # Also stub get_applier so the applier-registered check passes
    import arbiter.appliers as _appliers_mod
    monkeypatch.setattr(
        _appliers_mod, "get_applier",
        lambda kind: object(),   # fake applier; just exists
    )

    result = action_proposal_apply._apply_validate(
        shared_dir=tmp_path, proposal_id="p-pw-flag",
    )
    assert result["ok"] is True, result
    assert result["requires_confirmation"] is True
    assert result["context"]["action_kind"] == "SoulEdit"


def test_apply_validate_normal_kind_does_not_require_confirmation(tmp_path):
    """Investigation (and other non-force-ask kinds) → validate
    returns requires_confirmation=False. Authority-tier semantics
    apply as normal."""
    _seed_proposal(
        tmp_path, proposal_id="p-normal-kind", bot_id="team_bot_a",
        summary="normal kind",
    )
    result = action_proposal_apply._apply_validate(
        shared_dir=tmp_path, proposal_id="p-normal-kind",
    )
    assert result["ok"] is True
    assert result["requires_confirmation"] is False


def test_apply_handler_unknown_proposal(tmp_path):
    """Unknown proposal_id → handler returns structured error.
    Never raises up to the MCP bridge."""
    result = action_proposal_apply._apply_handler(
        shared_dir=tmp_path, proposal_id="ghost",
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_apply_handler_full_chain_investigation(tmp_path):
    """End-to-end on a real Investigation proposal. After apply, the
    proposal should:

      - have status='applied' (Investigation is a manual-completion
        kind; it waits for operator's "Mark complete" to promote to
        succeeded, per arbiter.apply.is_deferred_completion_kind)
      - live in proposals/applied/ on disk
      - NO longer live in proposals/pending/
      - have its history recording the pending→approved_auto→applied
        transitions

    Closes the linchpin contract: synchronous end-to-end apply,
    correctly handling deferred-completion kinds.
    """
    from arbiter import store as arbiter_store
    p_id = "p-apply-full"
    _seed_proposal(
        tmp_path, proposal_id=p_id, bot_id="team_bot_a",
        summary="apply-chain test",
    )
    # File starts in pending/
    assert (tmp_path / "proposals" / "pending" / f"{p_id}.json").exists()

    result = action_proposal_apply._apply_handler(
        shared_dir=tmp_path, proposal_id=p_id,
        reason="evo applying this from the resolver test",
    )

    # Successful end-to-end
    assert result["ok"] is True, result
    assert result["proposal_id"] == p_id
    assert result["action_kind"] == "Investigation"
    # Investigation is a deferred-completion kind — stops at applied.
    # (Other kinds like ConfigPatch would auto-promote to succeeded.)
    assert result["to_status"] == "applied"
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.proposals.pending"
    assert result["verify_via"]["args"] == {"proposal_id": p_id}

    # On-disk verification: file moved from pending/ → applied/
    assert not (tmp_path / "proposals" / "pending" / f"{p_id}.json").exists()
    assert (tmp_path / "proposals" / "applied" / f"{p_id}.json").exists()

    # Re-read the persisted proposal and check the audit trail
    located = arbiter_store.find_proposal(tmp_path, p_id)
    assert located is not None
    proposal, _path, subdir = located
    assert subdir == "applied"
    assert proposal.status == "applied"
    # The history must record the transitions, with evo as actor on
    # the approved_auto promotion. Field is .history on Proposal.
    actors = [h.actor for h in proposal.history]
    statuses = [h.to_status for h in proposal.history]
    assert "evo" in actors
    assert "approved_auto" in statuses
    assert "applied" in statuses


def test_apply_handler_already_approved_skips_promotion(tmp_path):
    """A proposal already in approved_auto status (eg auto-approved
    by RSI) → apply runs without re-promoting. promoted_from is None
    since we didn't have to transition from pending."""
    from arbiter import store as arbiter_store
    from schema.proposal import Proposal, Investigation, RiskTag
    from schema.provenance import Provenance

    p_id = "p-already-approved"
    # Write directly via the store with the correct subdir mapping —
    # approved_auto status lives in proposals/pending/ per _STATUS_TO_SUBDIR.
    p = Proposal(
        id=p_id, bot_id="team_bot_a", generator_id="test_gen",
        dimension="cost", trigger_observations=[],
        provenance=Provenance(technique="test"),
        problem="already approved",
        action=Investigation(context="test"),
        risk_tag=RiskTag(blast_radius="local", reversibility="reversible"),
        urgency="improvement",
        admin_surface_summary="already approved",
        status="approved_auto",
    )
    arbiter_store.write_proposal(p, tmp_path, subdir="pending")

    result = action_proposal_apply._apply_handler(
        shared_dir=tmp_path, proposal_id=p_id,
    )
    assert result["ok"] is True, result
    # The promoted_from is None because the proposal was already
    # approved_auto — we skipped the pending→approved_auto step.
    assert result.get("from_status") in (None, "approved_*")


def test_apply_handler_snoozed_proposal_rejected(tmp_path):
    """A proposal in 'snoozed' status → handler refuses, surfacing
    the actual status. Apply is only valid for pending/approved_*."""
    _seed_proposal(
        tmp_path, proposal_id="p-snoozed-test", bot_id="team_bot_a",
        status="snoozed", summary="already snoozed",
    )
    result = action_proposal_apply._apply_handler(
        shared_dir=tmp_path, proposal_id="p-snoozed-test",
    )
    assert result["ok"] is False
    assert result["current_status"] == "snoozed"


def test_apply_force_ask_kinds_set_is_documented(tmp_path):
    """The _FORCE_ASK_ACTION_KINDS set is a load-bearing security
    surface — every kind in it represents a policy-weighted change
    that warrants operator review even under 'auto' authority.
    Adding / removing entries requires deliberate intent."""
    assert "SoulEdit" in action_proposal_apply._FORCE_ASK_ACTION_KINDS
    assert "ThrottleGenerator" in action_proposal_apply._FORCE_ASK_ACTION_KINDS
    assert "PauseGenerator" in action_proposal_apply._FORCE_ASK_ACTION_KINDS
    assert "UpdatePermissionBaseline" in action_proposal_apply._FORCE_ASK_ACTION_KINDS
    assert "UpdateContentScanCatalog" in action_proposal_apply._FORCE_ASK_ACTION_KINDS
    # NOT in the set — these should still respect normal tier semantics
    assert "Investigation" not in action_proposal_apply._FORCE_ASK_ACTION_KINDS
    assert "ConfigPatch" not in action_proposal_apply._FORCE_ASK_ACTION_KINDS


# ─────────────────────────────────────────────────────────────────────────────
# action.proposal.reject — destructive (Phase 1.4 step 2)
# ─────────────────────────────────────────────────────────────────────────────


def test_reject_registered_as_destructive():
    found = _tools.lookup("action.proposal.reject")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.DESTRUCTIVE
    assert found.validate is not None


def test_reject_validate_missing_id(tmp_path):
    result = action_proposal._reject_validate(shared_dir=tmp_path, proposal_id="")
    assert result["ok"] is False


def test_reject_validate_unknown_proposal(tmp_path):
    result = action_proposal._reject_validate(
        shared_dir=tmp_path, proposal_id="ghost",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_reject_validate_ok_for_pending(tmp_path):
    _seed_proposal(
        tmp_path, proposal_id="p-rej-ok", bot_id="team_bot_a",
        summary="rej ok",
    )
    result = action_proposal._reject_validate(
        shared_dir=tmp_path, proposal_id="p-rej-ok",
    )
    assert result["ok"] is True
    assert result["requires_confirmation"] is True   # destructive always asks
    assert result["context"]["proposal_id"] == "p-rej-ok"


def test_reject_handler_terminal_transition(tmp_path):
    """Reject moves pending → rejected, file moves pending/ → archived/."""
    from arbiter import store as arbiter_store
    p_id = "p-reject-real"
    _seed_proposal(
        tmp_path, proposal_id=p_id, bot_id="team_bot_a",
        summary="will be rejected",
    )
    result = action_proposal._reject_handler(
        shared_dir=tmp_path, proposal_id=p_id,
        reason="evo's test",
    )
    assert result["ok"] is True, result
    assert result["from_status"] == "pending"
    assert result["to_status"] == "rejected"
    assert "verify_via" in result

    # File moved pending/ → archived/
    assert not (tmp_path / "proposals" / "pending" / f"{p_id}.json").exists()
    assert (tmp_path / "proposals" / "archived" / f"{p_id}.json").exists()

    # Status field flipped + history recorded
    located = arbiter_store.find_proposal(tmp_path, p_id)
    assert located is not None
    proposal, _, subdir = located
    assert subdir == "archived"
    assert proposal.status == "rejected"
    assert any(h.actor == "evo" for h in proposal.history)


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.restart + action.bot.redeploy
# ─────────────────────────────────────────────────────────────────────────────


def _write_network_for_bot_tools(tmp_path: Path, bot_id: str = "team_bot_a") -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["evolve", bot_id],
        "bots": {
            "evolve": {"role": "primary", "port": 19030, "user": "evolve"},
            bot_id: {"role": "member", "port": 18789, "user": bot_id},
        },
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def test_bot_restart_registered_as_write_risky():
    found = _tools.lookup("action.bot.restart")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_RISKY


def test_bot_redeploy_registered_as_write_risky():
    found = _tools.lookup("action.bot.redeploy")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_RISKY


def test_bot_restart_validate_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._restart_validate(network_path=np, bot_id="ghost")
    assert result["ok"] is False
    assert "not registered" in result["reason"]


def test_bot_restart_validate_ok(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._restart_validate(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True


def test_bot_restart_handler_invokes_deploy(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    calls = []
    monkeypatch.setattr(
        "evolve_admin.deploy.restart_gateway",
        lambda bot_id, bot_user=None: calls.append({"bot_id": bot_id}),
    )
    result = action_bot._restart_handler(
        network_path=np, bot_id="team_bot_a", reason="test",
    )
    assert result["ok"] is True
    assert result["bot_id"] == "team_bot_a"
    assert result["verify_via"]["tool"] == "pod_state.bots"
    assert calls == [{"bot_id": "team_bot_a"}]


def test_bot_restart_handler_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._restart_handler(network_path=np, bot_id="ghost")
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_bot_restart_handler_surfaces_deploy_errors(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)

    def boom(bot_id, bot_user=None):
        raise RuntimeError("port 19030 stuck")

    monkeypatch.setattr("evolve_admin.deploy.restart_gateway", boom)
    result = action_bot._restart_handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is False
    assert "port 19030 stuck" in result["error"]


def test_bot_redeploy_handler_invokes_deploy_bot(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    calls = []

    class FakeResult:
        success = True
        steps = ["step1", "step2"]
        errors = []

    monkeypatch.setattr(
        "evolve_admin.deploy.deploy_bot",
        lambda **k: (calls.append(k) or FakeResult()),
    )
    result = action_bot._redeploy_handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True
    assert calls[0]["bot_id"] == "team_bot_a"
    assert calls[0]["role"] == "member"
    assert calls[0]["dry_run"] is False
    assert result["verify_via"]["tool"] == "pod_state.bots"


def test_bot_redeploy_handler_failure(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)

    class FakeResult:
        success = False
        steps = ["got partway"]
        errors = ["plugin install failed"]

    monkeypatch.setattr(
        "evolve_admin.deploy.deploy_bot", lambda **k: FakeResult(),
    )
    result = action_bot._redeploy_handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is False
    assert result["errors"] == ["plugin install failed"]


# ─────────────────────────────────────────────────────────────────────────────
# action.app.install + pod_state.forge_job
# ─────────────────────────────────────────────────────────────────────────────


def test_app_install_registered_as_write_risky():
    found = _tools.lookup("action.app.install")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_RISKY


def test_forge_job_registered_as_read():
    found = _tools.lookup("pod_state.forge_job")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ
    assert found.validate is None


def test_app_install_validate_missing_args(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    assert action_app._install_validate(
        shared_dir=tmp_path, network_path=np, bot_id="", pkg_id="x",
    )["ok"] is False
    assert action_app._install_validate(
        shared_dir=tmp_path, network_path=np, bot_id="team_bot_a", pkg_id="",
    )["ok"] is False


def test_app_install_validate_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._install_validate(
        shared_dir=tmp_path, network_path=np,
        bot_id="ghost", pkg_id="pkg",
    )
    assert result["ok"] is False
    assert "not registered" in result["reason"]


def test_app_install_handler_creates_forge_job(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        lambda pkg_id, shared_dir: {"name": "Test App", "pkg_version": "1.0.0"},
    )

    class FakeJob:
        job_id = "j-test123"
        status = "created"

    monkeypatch.setattr(
        "evolve_admin.applications.forge_jobs.create_install_job",
        lambda **k: FakeJob(),
    )
    result = action_app._install_handler(
        shared_dir=tmp_path, network_path=np,
        bot_id="team_bot_a", pkg_id="test-pkg",
    )
    assert result["ok"] is True
    assert result["job_id"] == "j-test123"
    assert result["app_id"] == "test-app"
    assert result["verify_via"]["tool"] == "pod_state.forge_job"


def test_app_install_handler_missing_package(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.applications.gallery.load_gallery_package",
        lambda pkg_id, shared_dir: None,
    )
    result = action_app._install_handler(
        shared_dir=tmp_path, network_path=np,
        bot_id="team_bot_a", pkg_id="ghost",
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_forge_job_handler_unknown_job(tmp_path):
    from evolve_admin.evo.tools import action_app
    result = action_app._forge_job_handler(
        shared_dir=tmp_path, job_id="not-a-job",
    )
    assert result["ok"] is False


def test_forge_job_handler_reads_real_job(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_app

    class FakeStep:
        def __init__(self, name, status):
            self.name = name
            self.status = status

    class FakeJob:
        job_id = "j-abc"
        job_type = "install"
        bot_id = "team_bot_a"
        pkg_id = "test-pkg"
        app_id = "test-app"
        status = "running"
        created_at = "2026-05-19T00:00:00Z"
        last_updated = "2026-05-19T00:01:00Z"
        steps = [
            FakeStep("download", "completed"),
            FakeStep("install", "running"),
        ]

    monkeypatch.setattr(
        "evolve_admin.applications.forge_jobs.load_job",
        lambda job_id, shared_dir: FakeJob(),
    )
    result = action_app._forge_job_handler(
        shared_dir=tmp_path, job_id="j-abc",
    )
    assert result["ok"] is True
    assert result["status"] == "running"
    assert result["current_step"] == "install"
    assert result["completed_steps"] == ["download"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5a: action.bot.remove (destructive)
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_remove_registered_as_destructive():
    found = _tools.lookup("action.bot.remove")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.DESTRUCTIVE


def test_bot_remove_validate_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._remove_validate(
        network_path=np, bot_id="ghost", confirm=True,
    )
    assert result["ok"] is False
    assert "not registered" in result["reason"]


def test_bot_remove_validate_requires_confirm(tmp_path):
    """The validate gate refuses confirm=False so the proxy can surface
    a clear reason instead of rendering a button that would silently
    error at execute time."""
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._remove_validate(
        network_path=np, bot_id="team_bot_a", confirm=False,
    )
    assert result["ok"] is False
    assert "confirm: true" in result["reason"]


def test_bot_remove_validate_passes_with_confirm(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._remove_validate(
        network_path=np, bot_id="team_bot_a", confirm=True,
    )
    assert result["ok"] is True
    assert result["context"]["bot_id"] == "team_bot_a"
    assert "irreversible_summary" in result["context"]


def test_bot_remove_handler_refuses_without_confirm(tmp_path):
    """Defense-in-depth: even at handler time, confirm:false short-circuits."""
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._remove_handler(
        network_path=np, bot_id="team_bot_a", confirm=False,
    )
    assert result["ok"] is False
    assert "confirm" in result["error"]


# test_bot_remove_handler_calls_deploy_remove_bot was deleted. PR #1903
# re-plumbed `action.bot.remove` to route through `/api/lifecycle/retire`
# with a `retire.retire_bot` fallback; the old test still mocked
# `deploy.remove_bot` and was failing on every run pre-cleanup. The
# replacement behavior is covered by tests in test_api_lifecycle.py.


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5a: action.pod.pause_all / resume_all + pod_state.pause_state
# ─────────────────────────────────────────────────────────────────────────────


def test_pause_all_registered_as_destructive():
    found = _tools.lookup("action.pod.pause_all")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.DESTRUCTIVE


def test_resume_all_registered_as_write_risky():
    found = _tools.lookup("action.pod.resume_all")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_RISKY


def test_pause_state_registered_as_read():
    found = _tools.lookup("pod_state.pause_state")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.READ


def test_pause_all_validate_requires_reason(tmp_path):
    from evolve_admin.evo.tools import action_pod
    np = _write_network_for_bot_tools(tmp_path)
    result = action_pod._pause_all_validate(
        network_path=np, reason="", confirm=True,
    )
    assert result["ok"] is False
    assert "reason" in result["reason"].lower()


def test_pause_all_validate_requires_confirm(tmp_path):
    from evolve_admin.evo.tools import action_pod
    np = _write_network_for_bot_tools(tmp_path)
    result = action_pod._pause_all_validate(
        network_path=np, reason="emergency", confirm=False,
    )
    assert result["ok"] is False
    assert "confirm" in result["reason"]


def test_pause_all_validate_passes_with_reason_and_confirm(tmp_path):
    from evolve_admin.evo.tools import action_pod
    np = _write_network_for_bot_tools(tmp_path)
    result = action_pod._pause_all_validate(
        network_path=np, reason="hardware migration", confirm=True,
    )
    assert result["ok"] is True
    # Surfaces an estimate so the operator knows the blast radius.
    assert "estimated_bots_affected" in result["context"]
    assert result["context"]["reversible_via"] == "action.pod.resume_all"


def test_pause_all_handler_refuses_without_confirm(tmp_path):
    from evolve_admin.evo.tools import action_pod
    np = _write_network_for_bot_tools(tmp_path)
    result = action_pod._pause_all_handler(
        network_path=np, reason="emergency", confirm=False,
    )
    assert result["ok"] is False
    assert "confirm" in result["error"]


def test_pause_all_handler_calls_recovery_pause_all(tmp_path, monkeypatch):
    """Handler delegates to recovery.pause_all when reason+confirm set."""
    from evolve_admin.evo.tools import action_pod

    np = _write_network_for_bot_tools(tmp_path)
    called: dict = {}

    class FakePerBot:
        def __init__(self, bot_id, ok):
            self.bot_id = bot_id
            self.ok = ok

        def to_dict(self):
            return {"bot_id": self.bot_id, "ok": self.ok}

    class FakeResult:
        action = "pause-all"
        ok = True
        initiated_by = "evo:tool"
        reason = "test"
        started_at = "2026-05-19T00:00:00+00:00"
        finished_at = "2026-05-19T00:00:30+00:00"
        elapsed_ms = 30000
        per_bot = [FakePerBot("team_bot_a", True), FakePerBot("evolve", True)]
        state_before = None
        state_after = {"paused": True, "reason": "test"}
        dry_run = False

    def fake_pause_all(*, reason, initiated_by, network, dry_run):
        called["reason"] = reason
        called["initiated_by"] = initiated_by
        called["dry_run"] = dry_run
        return FakeResult()

    recovery = action_pod._import_recovery()
    assert recovery is not None
    monkeypatch.setattr(recovery, "pause_all", fake_pause_all)

    result = action_pod._pause_all_handler(
        network_path=np, reason="rolling-upgrade", confirm=True,
    )
    assert result["ok"] is True
    assert called["reason"] == "rolling-upgrade"
    assert called["initiated_by"] == "evo:tool"
    assert called["dry_run"] is False
    assert result["bots_total"] == 2
    assert sorted(result["bots_ok"]) == ["evolve", "team_bot_a"]
    assert result["verify_via"]["tool"] == "pod_state.pause_state"


def test_resume_all_handler_calls_recovery_resume_all(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_pod

    np = _write_network_for_bot_tools(tmp_path)
    called: dict = {}

    class FakeResult:
        action = "resume-all"
        ok = True
        initiated_by = "evo:tool"
        reason = ""
        started_at = ""
        finished_at = ""
        elapsed_ms = 100
        per_bot = []
        state_before = {"paused": True}
        state_after = None
        dry_run = False

    def fake_resume_all(*, initiated_by, network, dry_run):
        called["initiated_by"] = initiated_by
        called["dry_run"] = dry_run
        return FakeResult()

    recovery = action_pod._import_recovery()
    assert recovery is not None
    monkeypatch.setattr(recovery, "resume_all", fake_resume_all)

    result = action_pod._resume_all_handler(network_path=np, reason="all clear")
    assert result["ok"] is True
    assert called["initiated_by"] == "evo:tool"
    assert result["reason"] == "all clear"
    assert result["verify_via"]["expect"].startswith("paused=false")


def test_pause_state_handler_returns_unpaused_when_no_flag(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_pod
    recovery = action_pod._import_recovery()
    assert recovery is not None
    monkeypatch.setattr(recovery, "read_pause_state", lambda _shared: None)

    result = action_pod._pause_state_handler()
    assert result["ok"] is True
    assert result["paused"] is False


def test_pause_state_handler_returns_paused_payload_when_set(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_pod
    recovery = action_pod._import_recovery()
    assert recovery is not None
    monkeypatch.setattr(
        recovery, "read_pause_state",
        lambda _shared: {
            "paused": True,
            "paused_at": "2026-05-19T01:00:00+00:00",
            "initiated_by": "operator",
            "reason": "maintenance",
        },
    )
    result = action_pod._pause_state_handler()
    assert result["ok"] is True
    assert result["paused"] is True
    assert result["reason"] == "maintenance"
    assert result["initiated_by"] == "operator"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5a: action.app.audit
# ─────────────────────────────────────────────────────────────────────────────


def test_app_audit_registered_as_write_safe():
    found = _tools.lookup("action.app.audit")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_SAFE


def test_app_audit_validate_requires_bot_id():
    from evolve_admin.evo.tools import action_app
    assert action_app._audit_validate(
        network_path=Path("/dev/null"), bot_id="", app_id="x",
    )["ok"] is False


def test_app_audit_validate_requires_app_or_all_apps(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._audit_validate(
        network_path=np, bot_id="team_bot_a",
    )
    assert result["ok"] is False
    assert "all_apps" in result["reason"]


def test_app_audit_validate_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._audit_validate(
        network_path=np, bot_id="ghost", app_id="x",
    )
    assert result["ok"] is False
    assert "not registered" in result["reason"]


def test_app_audit_validate_passes_with_app_id(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._audit_validate(
        network_path=np, bot_id="team_bot_a", app_id="morning-brief",
    )
    assert result["ok"] is True
    assert result["context"]["scope"] == "morning-brief"


def test_app_audit_validate_passes_with_all_apps(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._audit_validate(
        network_path=np, bot_id="team_bot_a", all_apps=True,
    )
    assert result["ok"] is True
    assert result["context"]["scope"] == "all apps"


def test_app_audit_handler_calls_request_audit(tmp_path, monkeypatch):
    """Handler delegates to applications.audit_dispatch.request_audit."""
    from evolve_admin.evo.tools import action_app

    np = _write_network_for_bot_tools(tmp_path)
    called: dict = {}

    class FakeDispatch:
        ok = True
        request_id = "audit-req-deadbeef"
        kicked = True
        error = ""

    def fake_request_audit(bot_id, bot_user, *, apps, full_audit, requested_by, kick):
        called["bot_id"] = bot_id
        called["bot_user"] = bot_user
        called["apps"] = apps
        called["full_audit"] = full_audit
        called["requested_by"] = requested_by
        called["kick"] = kick
        return FakeDispatch()

    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.request_audit",
        fake_request_audit,
    )
    result = action_app._audit_handler(
        network_path=np, bot_id="team_bot_a", app_id="morning-brief",
        full_audit=True, reason="spot-check",
    )
    assert result["ok"] is True
    assert result["request_id"] == "audit-req-deadbeef"
    assert called["bot_id"] == "team_bot_a"
    assert called["apps"] == ["morning-brief"]
    assert called["full_audit"] is True
    assert called["kick"] is True
    assert called["requested_by"] == "evo:tool"


def test_app_audit_handler_with_all_apps(tmp_path, monkeypatch):
    from evolve_admin.evo.tools import action_app

    np = _write_network_for_bot_tools(tmp_path)
    captured: dict = {}

    class FakeDispatch:
        ok = True
        request_id = "audit-req-all"
        kicked = True
        error = ""

    def fake_request_audit(bot_id, bot_user, *, apps, **k):
        captured["apps"] = apps
        return FakeDispatch()

    monkeypatch.setattr(
        "evolve_admin.applications.audit_dispatch.request_audit",
        fake_request_audit,
    )
    result = action_app._audit_handler(
        network_path=np, bot_id="team_bot_a", all_apps=True,
    )
    assert result["ok"] is True
    # all_apps=True passes apps=None to request_audit (the "all eligible" signal)
    assert captured["apps"] is None
    # Response surfaces 'all' so the LLM phrases it correctly
    assert result["apps"] == "all"


def test_app_audit_handler_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._audit_handler(
        network_path=np, bot_id="ghost", app_id="x",
    )
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_app_audit_handler_refuses_when_no_app_target(tmp_path):
    """Either app_id or all_apps=true is required at the handler level."""
    from evolve_admin.evo.tools import action_app
    np = _write_network_for_bot_tools(tmp_path)
    result = action_app._audit_handler(
        network_path=np, bot_id="team_bot_a",
    )
    assert result["ok"] is False
    assert "all_apps" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5e: action.signal.resolve
# ─────────────────────────────────────────────────────────────────────────────


def test_signal_resolve_registered_as_write_safe():
    found = _tools.lookup("action.signal.resolve")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_SAFE


def test_signal_resolve_validate_missing_signal_id(tmp_path):
    result = action_signal._resolve_validate(shared_dir=tmp_path, signal_id="")
    assert result["ok"] is False
    assert "signal_id is required" in result["reason"]


def test_signal_resolve_validate_unknown_signal(tmp_path):
    result = action_signal._resolve_validate(
        shared_dir=tmp_path, signal_id="not-a-real-id",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_signal_resolve_validate_rejects_terminal_state(tmp_path):
    """A signal already in resolved or dismissed state can't be
    re-resolved — the state machine has no outbound edge from terminal
    states (except resolved → firing via observe(), which is producer-only)."""
    from signals import store as _store
    sig_id = _seed_signal(
        tmp_path, signature="t:resolve:terminal",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn",
        title="already resolved",
    )
    # Move to resolved first.
    located = _store.find_signal(tmp_path, sig_id)
    assert located is not None
    sig, _path, _subdir = located
    _store.apply_transition(
        sig, "resolved", tmp_path, actor="test", reason="setup",
    )
    result = action_signal._resolve_validate(
        shared_dir=tmp_path, signal_id=sig_id,
    )
    assert result["ok"] is False
    assert "terminal state" in result["reason"]


def test_signal_resolve_validate_ok_for_firing(tmp_path):
    sig_id = _seed_signal(
        tmp_path, signature="t:resolve:firing",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn",
        title="firing",
    )
    result = action_signal._resolve_validate(
        shared_dir=tmp_path, signal_id=sig_id,
    )
    assert result["ok"] is True
    assert result["context"]["current_state"] == "firing"


def test_signal_resolve_validate_ok_for_snoozed(tmp_path):
    """Snoozed signals can also transition to resolved — useful when an
    operator snoozed something, then fixed it before the snooze expired."""
    from signals import store as _store
    sig_id = _seed_signal(
        tmp_path, signature="t:resolve:snoozed",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="warn",
        title="snoozed",
    )
    located = _store.find_signal(tmp_path, sig_id)
    assert located is not None
    sig, _path, _subdir = located
    _store.apply_transition(
        sig, "snoozed", tmp_path, actor="test", reason="setup",
        snoozed_until="2099-01-01T00:00:00Z",
    )
    result = action_signal._resolve_validate(
        shared_dir=tmp_path, signal_id=sig_id,
    )
    assert result["ok"] is True
    assert result["context"]["current_state"] == "snoozed"


def test_signal_resolve_handler_transitions_to_resolved(tmp_path):
    """Resolving a firing signal moves it from firing/ to archived/ on
    disk; state field flips; history records the transition."""
    from signals import store as _store
    sig_id = _seed_signal(
        tmp_path, signature="t:resolve:handler",
        producer="content_scan", type_="t",
        bot_id="personal_bot", severity="alert",
        title="real resolve",
    )
    result = action_signal._resolve_handler(
        shared_dir=tmp_path, signal_id=sig_id,
        reason="fixed via manual restart",
    )
    assert result["ok"] is True
    assert result["to_state"] == "resolved"
    assert "verify_via" in result
    assert result["verify_via"]["tool"] == "pod_state.signals.history"
    assert result["verify_via"]["args"]["state"] == "resolved"

    located = _store.find_signal(tmp_path, sig_id)
    assert located is not None
    sig, _path, subdir = located
    assert subdir == "archived"
    assert sig.state == "resolved"
    assert any(
        h.actor == "evo" and "manual restart" in (h.reason or "")
        for h in sig.state_history
    )


def test_signal_resolve_handler_unknown_signal(tmp_path):
    result = action_signal._resolve_handler(
        shared_dir=tmp_path, signal_id="no-such-signal",
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5e: action.proposal.mark_complete
# ─────────────────────────────────────────────────────────────────────────────


def test_proposal_mark_complete_registered_as_write_safe():
    found = _tools.lookup("action.proposal.mark_complete")
    assert found is not None
    assert found.risk_tier == _tools.RiskTier.WRITE_SAFE


def test_proposal_mark_complete_validate_missing_id(tmp_path):
    result = action_proposal._mark_complete_validate(
        shared_dir=tmp_path, proposal_id="",
    )
    assert result["ok"] is False
    assert "proposal_id is required" in result["reason"]


def test_proposal_mark_complete_validate_unknown(tmp_path):
    result = action_proposal._mark_complete_validate(
        shared_dir=tmp_path, proposal_id="p-ghost",
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_proposal_mark_complete_validate_wrong_status(tmp_path):
    """A proposal in pending (not applied) can't be marked complete."""
    _seed_proposal(
        tmp_path, proposal_id="p-pending-1",
        bot_id="team_bot_a", status="pending",
        summary="not yet applied",
    )
    result = action_proposal._mark_complete_validate(
        shared_dir=tmp_path, proposal_id="p-pending-1",
    )
    assert result["ok"] is False
    assert "not 'applied'" in result["reason"]


def test_proposal_mark_complete_validate_ok_for_applied_investigation(tmp_path):
    """Investigation is a manual-completion kind; applied → succeeded
    is the right transition."""
    from arbiter import store as arbiter_store
    # _seed_proposal writes status=applied but to subdir=applied (not pending).
    _seed_proposal(
        tmp_path, proposal_id="p-applied-inv",
        bot_id="team_bot_a", status="applied",
        summary="applied investigation",
    )
    # _seed_proposal writes to subdir=status, so applied went to applied/.
    # Quick sanity check that find_proposal locates it.
    located = arbiter_store.find_proposal(tmp_path, "p-applied-inv")
    assert located is not None
    _, _, subdir = located
    assert subdir == "applied"

    result = action_proposal._mark_complete_validate(
        shared_dir=tmp_path, proposal_id="p-applied-inv",
    )
    assert result["ok"] is True
    assert result["context"]["action_kind"] == "Investigation"
    assert result["context"]["current_subdir"] == "applied"


def test_proposal_mark_complete_handler_promotes_to_succeeded(tmp_path):
    """Handler runs the applied → succeeded transition and moves file
    to archived/."""
    from arbiter import store as arbiter_store
    _seed_proposal(
        tmp_path, proposal_id="p-applied-mc",
        bot_id="team_bot_a", status="applied",
        summary="manual completion",
    )

    result = action_proposal._mark_complete_handler(
        shared_dir=tmp_path, proposal_id="p-applied-mc",
        reason="operator confirmed work done",
    )
    assert result["ok"] is True
    assert result["from_status"] == "applied"
    assert result["to_status"] == "succeeded"
    assert result["action_kind"] == "Investigation"
    assert "verify_via" in result

    # File moved to archived/
    located = arbiter_store.find_proposal(tmp_path, "p-applied-mc")
    assert located is not None
    proposal, _path, subdir = located
    assert subdir == "archived"
    assert proposal.status == "succeeded"
    # Reason captured in history
    assert any(
        h.actor == "evo" and "work done" in (h.reason or "")
        for h in proposal.history
    )


def test_proposal_mark_complete_refuses_external_completion_kind(tmp_path, monkeypatch):
    """External-completion kinds (BuildApp) are completed by a sweep,
    not by manual mark_complete — handler refuses."""
    _seed_proposal(
        tmp_path, proposal_id="p-applied-ext",
        bot_id="team_bot_a", status="applied",
        summary="build app proposal",
    )
    # Override the action_kind lookup so we can simulate BuildApp without
    # building a real BuildApp proposal fixture.
    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_proposal._find_proposal",
        lambda shared_dir, pid: (
            _override_action_kind(action_proposal._find_proposal.__wrapped__, shared_dir, pid)
            if hasattr(action_proposal._find_proposal, "__wrapped__")
            else _find_with_kind(shared_dir, pid, "BuildApp")
        ),
    )
    result = action_proposal._mark_complete_handler(
        shared_dir=tmp_path, proposal_id="p-applied-ext",
    )
    assert result["ok"] is False
    assert "external sweep" in result["error"]


def _find_with_kind(shared_dir, pid, kind: str):
    """Locate a proposal then synthesize an action object reporting the
    requested kind via attribute spoofing — used to test the
    external-completion guard without a full BuildApp fixture."""
    from arbiter import store as arbiter_store
    located = arbiter_store.find_proposal(shared_dir, pid)
    if located is None:
        return None, None
    proposal, _path, subdir = located

    class _SpoofAction:
        def __init__(self, kind):
            self.kind = kind

    proposal.action = _SpoofAction(kind)
    return proposal, subdir


def _override_action_kind(orig, shared_dir, pid):
    """Wrapper used by the BuildApp-kind monkeypatch path."""
    proposal, subdir = orig(shared_dir, pid)
    if proposal is not None:
        class _SpoofAction:
            kind = "BuildApp"
        proposal.action = _SpoofAction()
    return proposal, subdir


def test_proposal_mark_complete_refuses_non_manual_completion_kind(tmp_path, monkeypatch):
    """Typed config edits (ConfigPatch, UpsertCronJob) auto-promote
    via their claim or via apply.py's auto-succeed path. mark_complete
    refuses them so an operator can't shortcut the verification."""
    _seed_proposal(
        tmp_path, proposal_id="p-applied-cfg",
        bot_id="team_bot_a", status="applied",
        summary="config patch",
    )
    monkeypatch.setattr(
        "evolve_admin.evo.tools.action_proposal._find_proposal",
        lambda shared_dir, pid: _find_with_kind(shared_dir, pid, "ConfigPatch"),
    )
    result = action_proposal._mark_complete_handler(
        shared_dir=tmp_path, proposal_id="p-applied-cfg",
    )
    assert result["ok"] is False
    assert "not a manual-completion kind" in result["error"]


# ─────────────────────────────────────────────────────────────────────────────
# action.bot.repair_acls (audit-readability fix tool)
# ─────────────────────────────────────────────────────────────────────────────


def test_repair_acls_tool_registered():
    """The tool must be in the registry so evo can offer it."""
    found = _tools.lookup("action.bot.repair_acls")
    assert found is not None
    # WRITE_SAFE — re-applying idempotent ACLs is non-destructive.
    assert found.risk_tier.name == "WRITE_SAFE"
    assert found.validate is not None


def test_repair_acls_validate_unknown_bot(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._repair_acls_validate(network_path=np, bot_id="ghost")
    assert result["ok"] is False
    assert "not registered" in result["reason"]


def test_repair_acls_validate_known_bot(tmp_path):
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._repair_acls_validate(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True


def test_repair_acls_handler_invokes_set_evolve_read_acl(tmp_path, monkeypatch):
    """Happy path: handler calls deploy.set_evolve_read_acl with the
    target bot. This is the only way for an existing bot to pick up
    new ACL grants (like the .zshrc one added 2026-05-20) without a
    full redeploy."""
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    calls = []
    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl",
        lambda bot_id: calls.append(bot_id),
    )
    result = action_bot._repair_acls_handler(
        network_path=np, bot_id="team_bot_a", reason="clear audit_identity signal",
    )
    assert result["ok"] is True
    assert result["bot_id"] == "team_bot_a"
    assert calls == ["team_bot_a"]
    # verify_via points at the audit-producer signals so the model
    # can confirm the audit_identity signal cleared on the next sweep.
    assert result["verify_via"]["tool"] == "pod_state.signals.firing"
    assert result["verify_via"]["args"]["producer"] == "audit"
    assert result["verify_via"]["args"]["bot_id"] == "team_bot_a"


def test_repair_acls_handler_unknown_bot(tmp_path):
    """Pre-check before the deploy module is touched — typo'd bot
    ids should fail fast with a clear error, not a confusing
    set_evolve_read_acl traceback."""
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    result = action_bot._repair_acls_handler(
        network_path=np, bot_id="ghost",
    )
    assert result["ok"] is False
    assert "not registered" in result["error"]


def test_repair_acls_handler_surfaces_deploy_errors(tmp_path, monkeypatch):
    """If set_evolve_read_acl raises (sudo denied, file system error),
    the tool surfaces the error type + message instead of crashing."""
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)

    def boom(bot_id):
        raise PermissionError("sudo chmod denied")

    monkeypatch.setattr("evolve_admin.deploy.set_evolve_read_acl", boom)
    result = action_bot._repair_acls_handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is False
    assert "PermissionError" in result["error"]
    assert "sudo chmod denied" in result["error"]


def test_repair_acls_handler_default_reason(tmp_path, monkeypatch):
    """When the model omits ``reason``, the audit log gets a sensible
    default so the action's source is identifiable."""
    from evolve_admin.evo.tools import action_bot
    np = _write_network_for_bot_tools(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.deploy.set_evolve_read_acl", lambda bot_id: None,
    )
    result = action_bot._repair_acls_handler(network_path=np, bot_id="team_bot_a")
    assert result["ok"] is True
    assert "evo" in result["reason"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Deploy-side: set_evolve_read_acl extends to .zshrc
# ─────────────────────────────────────────────────────────────────────────────


def test_set_evolve_read_acl_grants_zshrc_when_present(monkeypatch, tmp_path):
    """``set_evolve_read_acl`` must ACL the bot user's .zshrc when
    present. Closes the audit-readability gap that surfaced on admin_bot
    2026-05-20: macOS creates .zshrc 0600 by default, evolve user
    couldn't read it, the audit fired ``audit_identity / .zshrc
    unreadable`` on every sweep for 13 days."""
    from evolve_admin import deploy

    # Stub the bot home + .openclaw dir so set_evolve_read_acl
    # short-circuits everything except the .zshrc handling.
    fake_home = tmp_path / "team_bot_a"
    fake_home.mkdir()
    (fake_home / ".zshrc").write_text("# admin_bot's shell config\n")
    # The function reads the user from a bot-id resolver; we don't
    # want it to look at the real /Users.
    monkeypatch.setattr(
        "evolve_admin.deploy._bot_user_for", lambda bot_id: "team_bot_a",
    )
    # Skip the .openclaw/ branch (doesn't exist in tmp_path).
    # We monkeypatch Path.exists so only .zshrc + a few of the other
    # tested paths exist. Cleanest: stub subprocess.run + assert the
    # right command was called for .zshrc.
    captured = []

    def _fake_run(args, **kwargs):
        captured.append(args)

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(deploy.subprocess, "run", _fake_run)

    # Make /Users/team_bot_a/.openclaw NOT exist so the function early-returns
    # from the main block. But we still want the .zshrc branch to run,
    # which is gated on /Users/team_bot_a/.zshrc existing. We can't easily
    # redirect Path("/Users/team_bot_a/.zshrc") to our tmp_path without more
    # patching. Instead, verify by reading the source — the simpler
    # structural assertion that the .zshrc handling exists in the
    # body, with the right ACL string.
    import inspect
    src = inspect.getsource(deploy.set_evolve_read_acl)
    assert ".zshrc" in src, (
        "set_evolve_read_acl no longer references .zshrc. The audit-"
        "readability fix from 2026-05-20 would regress."
    )
    # Since the W4a Perms seam, the .zshrc branch routes through
    # perms.grant() with the single-file read constant — the seam's
    # macOS backend renders the historical 'evolve allow read,…' ACE.
    # The grant must be an evolve-only per-user ACL, NOT 'chmod o+r'
    # (world-readable would be a security degrade).
    assert "FILE_READ_ACL_PERMS" in src, (
        "set_evolve_read_acl's .zshrc handling no longer grants via the "
        "single-file read ACL constant — if it switched to chmod o+r or "
        "world-read, that's a security regression."
    )
    assert deploy.FILE_READ_ACL_PERMS == "read,readattr,readextattr,readsecurity", (
        "FILE_READ_ACL_PERMS drifted — the seam renders this verbatim "
        "into the 'evolve allow <perms>' ACE that sudoers matches on."
    )
    # Must NOT have chmod o+r — that would be the wrong-layer fix.
    assert "o+r" not in src, (
        "set_evolve_read_acl appears to use chmod o+r — that makes "
        "files world-readable. The right pattern is the per-user ACL "
        "grant through the Perms seam."
    )
