"""tests/test_cache_ttl_tuner.py — cache_ttl_tuner factory + observe tests.

PR F retargeted the generator from Investigation-only proposals to a
typed UpdateAgentDefaults Proposal flipping
``agents.defaults.models.*.params.cacheRetention`` from ``"short"`` to
``"long"``, closing the loop on the team-bot-a session 3d5cde22
incident (2026-05-29 Slack DM, $7.67, 92% prompt-cache writes).

What this file covers:

  * The three Signal → Proposal factories (UpdateAgentDefaults,
    Investigation fallback for invalidation, Investigation for
    hit-rate-low).
  * The two suppression gates in ``observe()``: config-intent (operator
    pinned ``short``) and proposal_history dedup (similar Proposal
    raised in the last 14 days).
  * The Eligibility classification — UpdateAgentDefaults + hygiene
    urgency + low risk_tag must resolve to ``tier_floor=auto-small``
    once the apply pipeline fills in revert_on_failure.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.cache_ttl_tuner.observe import (  # noqa: E402
    CacheTtlTunerContext,
    observe,
)
from generators.cache_ttl_tuner import signal_proposals as sp_module  # noqa: E402
from generators.cache_ttl_tuner.signal_proposals import (  # noqa: E402
    CACHE_RETENTION_DOTPATH,
    CACHE_WRITE_FRACTION_ON_INVALIDATED,
    CAUSE_KEY_TTL_TOO_SHORT,
    FRACTION_REMEDIABLE_BY_TTL_BUMP,
    INTENT_FIELD_PATH,
    PROPOSAL_HISTORY_WINDOW_DAYS,
    SIGNAL_TYPE_TO_FACTORY,
    _estimate_savings_for_invalidation,
    make_cache_hit_rate_low_proposal,
    make_cache_invalidation_elevated_proposal,
    make_cache_invalidation_investigation_fallback,
)
from schema.signal import make_signature  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── Signal fixtures ──────────────────────────────────────────────────────────


def _invalidation_signal(
    *,
    sig_id: str = "sig-inv-1",
    bot_id: str = "admin_bot",
    invalidated: int = 30,
    participating: int = 100,
    ratio: float = 0.30,
    threshold: float = 0.15,
    window_days: int = 7,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="cache_invalidation_elevated",
        severity="warn",
        details={
            "bot_id": bot_id,
            "window_days": window_days,
            "invalidated_count": invalidated,
            "participating_count": participating,
            "invalidated_ratio": ratio,
            "threshold_ratio": threshold,
        },
    )


def _hit_rate_signal(
    *,
    sig_id: str = "sig-hr-1",
    bot_id: str = "admin_bot",
    hit_rate: float = 0.25,
    threshold: float = 0.50,
    participating: int = 100,
    window_days: int = 7,
    cache_read: int = 5000,
    cache_write: int = 10000,
    fresh_input: int = 5000,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id,
        bot_id=bot_id,
        type="cache_hit_rate_low",
        severity="warn",
        details={
            "bot_id": bot_id,
            "window_days": window_days,
            "hit_rate": hit_rate,
            "threshold_ratio": threshold,
            "participating_count": participating,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "input_tokens": fresh_input,
        },
    )


# ── make_cache_invalidation_elevated_proposal (typed UpdateAgentDefaults) ────


def test_invalidation_factory_emits_update_agent_defaults_action():
    """Cornerstone PR F change: the autonomous-fix proposal targets
    cacheRetention via UpdateAgentDefaults, not contextPruning.ttl via
    Investigation."""
    sig = _invalidation_signal()
    p = make_cache_invalidation_elevated_proposal(sig)
    assert p.action.kind == "UpdateAgentDefaults"
    assert p.action.bot_id == "admin_bot"
    assert p.action.fields == {CACHE_RETENTION_DOTPATH: "long"}


def test_invalidation_factory_dotpath_matches_applier_whitelist():
    """The Proposal's dotpath must be in the L2 applier's narrow
    whitelist (schema.proposal._UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS)
    — a mismatch would fail at the applier and the closed-loop story
    dies at the last step."""
    from schema.proposal import _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS

    sig = _invalidation_signal()
    p = make_cache_invalidation_elevated_proposal(sig)
    for dotpath in p.action.fields:
        assert dotpath in _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS


def test_invalidation_factory_value_matches_applier_validator():
    """The emitted value must pass the applier's per-dotpath validator
    (``"short" | "long"``). A typo or different casing dies at apply
    time with a clear refusal — but the proposal should be born clean."""
    from schema.proposal import _UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS

    sig = _invalidation_signal()
    p = make_cache_invalidation_elevated_proposal(sig)
    for dotpath, value in p.action.fields.items():
        validator = _UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS.get(dotpath)
        assert validator is not None
        if isinstance(validator, tuple):
            assert value in validator
        elif isinstance(validator, type):
            assert isinstance(value, validator)


def test_invalidation_factory_proposal_well_formed():
    sig = _invalidation_signal()
    p = make_cache_invalidation_elevated_proposal(sig)
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "cache_ttl_tuner"
    assert p.dimension == "efficiency"
    assert p.motivating_signals == ["sig-inv-1"]
    assert p.trigger_observations == ["cache_invalidation_elevated:admin_bot"]
    assert p.urgency == "hygiene"
    assert p.approval_audience == "pod_operator"
    assert p.risk_tag.blast_radius == "bot"
    assert p.risk_tag.reversibility == "auto"
    # touches model_config because we're writing to agents.defaults; the
    # applier enforces its own value validators on top.
    assert "model_config" in p.risk_tag.touches
    # Phase B operator-first title (2026-06-04). Was "Set admin_bot
    # cacheRetention=long — XX% invalidated"; now humanized per the
    # protocol — no slug ("cacheRetention"), no metric in the title.
    assert p.admin_surface_summary == "Make admin_bot's prompt cache last longer"
    assert len(p.admin_surface_summary) <= 120


def test_invalidation_factory_has_falsifiable_claim():
    """Eligibility classification needs a claim to grade the proposal
    as auto-eligible. The verify daemon checks
    ``cache_invalidation_ratio`` direction=down after the apply window."""
    sig = _invalidation_signal(ratio=0.45)
    p = make_cache_invalidation_elevated_proposal(sig)
    assert p.claim is not None
    assert p.claim.metric == "cache_invalidation_ratio"
    assert p.claim.direction == "down"
    assert p.claim.baseline == 0.45
    assert p.claim.fallback == "revert"
    assert p.claim.window_days > 0


def test_invalidation_factory_includes_root_cause_attribution():
    """proposal_history.proposal_history() dedups by cause_key on the
    provenance.signals.root_cause_attribution.cause_key path — without
    this the 14-day dedup window can't suppress repeats."""
    sig = _invalidation_signal()
    p = make_cache_invalidation_elevated_proposal(sig)
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == CAUSE_KEY_TTL_TOO_SHORT
    assert rca["primary_target"] == INTENT_FIELD_PATH


def test_invalidation_factory_includes_diagnostics_in_provenance():
    sig = _invalidation_signal(
        invalidated=45, participating=200, ratio=0.225
    )
    p = make_cache_invalidation_elevated_proposal(sig)
    assert p.provenance.signals["invalidated_count"] == 45
    assert p.provenance.signals["participating_count"] == 200
    assert p.provenance.signals["invalidated_ratio"] == 0.225
    assert p.provenance.technique == (
        "cache_ttl_tuner.cache_invalidation_elevated"
    )


def test_invalidation_factory_handles_dict_signal_form():
    sig_dict = {
        "id": "sig-d-1",
        "bot_id": "team_bot_a",
        "type": "cache_invalidation_elevated",
        "details": {
            "invalidated_count": 20,
            "participating_count": 50,
            "invalidated_ratio": 0.40,
            "threshold_ratio": 0.15,
            "window_days": 7,
        },
    }
    p = make_cache_invalidation_elevated_proposal(sig_dict)
    assert p.bot_id == "team_bot_a"
    assert p.action.bot_id == "team_bot_a"
    assert p.motivating_signals == ["sig-d-1"]


# ── Investigation fallback ───────────────────────────────────────────────────


def test_invalidation_fallback_is_investigation():
    """When the autonomous fix is suppressed, the fallback factory emits
    an Investigation so the firing signal still surfaces with an
    explanation — never silently swallow."""
    sig = _invalidation_signal()
    p = make_cache_invalidation_investigation_fallback(sig)
    assert p.action.kind == "Investigation"
    assert p.motivating_signals == ["sig-inv-1"]
    assert p.urgency == "improvement"
    # Still records the cause_key so the dedup window covers the
    # fallback emission too.
    rca = p.provenance.signals["root_cause_attribution"]
    assert rca["cause_key"] == CAUSE_KEY_TTL_TOO_SHORT


# ── Eligibility — auto-small lands when revert_on_failure is filled ──────────


def test_invalidation_proposal_eligibility_resolves_to_auto_small():
    """End-to-end eligibility cross-check. The classify_proposal pipeline
    sees: action_kind=UpdateAgentDefaults + urgency=hygiene + risk_tag
    {blast=bot, reversibility=auto, touches=[model_config]}. Once the
    apply pipeline has filled in revert_on_failure (mocked here as the
    eligibility test fixture does), the proposal lands at
    tier_floor=auto-small.

    This is the contract that makes PR F's closed-loop story work: the
    arbiter auto-applies without operator click.
    """
    import eligibility as elg

    sig = _invalidation_signal()
    p = make_cache_invalidation_elevated_proposal(sig)
    proposal_dict = p.to_dict()
    # Apply pipeline assigns revert_on_failure after capture_snapshot.
    # Simulate that here so we can test the post-snapshot eligibility.
    proposal_dict["revert_on_failure"] = {"strategy": "rollback"}
    e = elg.classify_proposal(proposal_dict)
    assert e.fix_risk == "low"
    assert e.decidable is True
    assert e.tier_floor == "auto-small"


# ── make_cache_hit_rate_low_proposal (unchanged: Investigation) ──────────────


def test_hit_rate_factory_produces_well_formed_proposal():
    sig = _hit_rate_signal()
    p = make_cache_hit_rate_low_proposal(sig)
    assert p.bot_id == "admin_bot"
    assert p.generator_id == "cache_ttl_tuner"
    assert p.motivating_signals == ["sig-hr-1"]
    assert p.trigger_observations == ["cache_hit_rate_low:admin_bot"]
    assert p.action.kind == "Investigation"
    # Phase B operator-first title (2026-06-04). Was "Investigate
    # admin_bot cache hit rate — XX% over Nd"; now humanized.
    assert (
        p.admin_surface_summary
        == "Look at why admin_bot's prompt cache isn't paying off"
    )
    assert len(p.admin_surface_summary) <= 120


def test_hit_rate_factory_includes_token_totals_in_context():
    sig = _hit_rate_signal(
        cache_read=12345, cache_write=67890, fresh_input=11111
    )
    p = make_cache_hit_rate_low_proposal(sig)
    assert "12,345" in p.action.context
    assert "67,890" in p.action.context
    assert "11,111" in p.action.context


def test_hit_rate_factory_points_at_prompt_structure_first():
    """Hit-rate-low is primarily a prompt-structure failure mode, not
    TTL. The context should name prompt-structure causes before the
    TTL cross-reference."""
    sig = _hit_rate_signal()
    p = make_cache_hit_rate_low_proposal(sig)
    sys_prompt_idx = p.action.context.find("System prompt regenerating")
    ttl_idx = p.action.context.find("TTL too short")
    assert sys_prompt_idx > -1
    assert ttl_idx > -1
    assert sys_prompt_idx < ttl_idx


# ── Dispatch table ────────────────────────────────────────────────────────────


def test_dispatch_includes_both_consumed_types_and_excludes_bot_unused():
    assert set(SIGNAL_TYPE_TO_FACTORY.keys()) == {
        "cache_invalidation_elevated",
        "cache_hit_rate_low",
    }
    assert "bot_unused" not in SIGNAL_TYPE_TO_FACTORY


# ── observe() end-to-end ──────────────────────────────────────────────────────


def _write_signal(
    shared_dir: Path,
    *,
    bot_id: str,
    sig_type: str,
    details: dict,
) -> str:
    """Write a session_economics Signal to a tmp store via the real API."""
    sig = signals_store.observe(
        shared_dir,
        signature=make_signature("session_economics", sig_type, bot_id),
        producer="session_economics",
        type=sig_type,
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: {sig_type}",
        details=details,
    )
    return sig.id


def _default_invalidation_details() -> dict:
    return {
        "invalidated_count": 30,
        "participating_count": 100,
        "invalidated_ratio": 0.30,
        "threshold_ratio": 0.15,
        "window_days": 7,
    }


def test_observe_emits_update_agent_defaults_on_invalidation(tmp_path):
    """The happy path — invalidation signal firing, no intent, no
    prior proposal: observe() returns a typed UpdateAgentDefaults
    Proposal targeting cacheRetention=long."""
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path)
    )
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action.kind == "UpdateAgentDefaults"
    assert p.action.fields == {CACHE_RETENTION_DOTPATH: "long"}


def test_observe_consumes_both_session_economics_signal_types(tmp_path):
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_hit_rate_low",
        details={
            "hit_rate": 0.25,
            "threshold_ratio": 0.50,
            "participating_count": 100,
            "window_days": 7,
            "cache_read_tokens": 5000,
            "cache_write_tokens": 10000,
            "input_tokens": 5000,
        },
    )
    proposals = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
    by_type = {p.trigger_observations[0].split(":")[0]: p for p in proposals}
    assert set(by_type.keys()) == {"cache_invalidation_elevated", "cache_hit_rate_low"}
    # Invalidation → typed UpdateAgentDefaults; hit_rate stays Investigation.
    assert by_type["cache_invalidation_elevated"].action.kind == "UpdateAgentDefaults"
    assert by_type["cache_hit_rate_low"].action.kind == "Investigation"


def test_observe_filters_by_bot_id(tmp_path):
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    _write_signal(
        tmp_path,
        bot_id="team_bot_c",
        sig_type="cache_invalidation_elevated",
        details={
            "invalidated_count": 50,
            "participating_count": 100,
            "invalidated_ratio": 0.50,
            "threshold_ratio": 0.15,
            "window_days": 7,
        },
    )

    admin_props = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path)
    )
    team_props = observe(
        CacheTtlTunerContext(bot_id="team_bot_c", shared_dir=tmp_path)
    )
    assert len(admin_props) == 1
    assert admin_props[0].bot_id == "admin_bot"
    assert len(team_props) == 1
    assert team_props[0].bot_id == "team_bot_c"


def test_observe_ignores_unrelated_producers(tmp_path):
    """Signals from cost_watchdog or other producers must not be consumed."""
    signals_store.observe(
        tmp_path,
        signature=make_signature("cost_watchdog", "context_bloat", "admin_bot/HEARTBEAT.md"),
        producer="cost_watchdog",
        type="context_bloat",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id="admin_bot",
        title="admin_bot: HEARTBEAT.md too big",
        details={"filename": "HEARTBEAT.md", "size_kb": 80, "threshold_kb": 30},
    )
    proposals = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


def test_observe_ignores_bot_unused_signal_type(tmp_path):
    _write_signal(
        tmp_path,
        bot_id="team_bot_b",
        sig_type="bot_unused",
        details={
            "unused_days": 14,
            "automation_event_count": 0,
            "last_user_turn_ts": None,
        },
    )
    proposals = observe(CacheTtlTunerContext(bot_id="team_bot_b", shared_dir=tmp_path))
    assert proposals == []


def test_observe_swallows_factory_error_on_one_signal(tmp_path, monkeypatch):
    """A bad payload on one signal must not torpedo the rest."""
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_hit_rate_low",
        details={
            "hit_rate": 0.25,
            "threshold_ratio": 0.50,
            "participating_count": 100,
            "window_days": 7,
            "cache_read_tokens": 5000,
            "cache_write_tokens": 10000,
            "input_tokens": 5000,
        },
    )

    def _exploding_factory(sig):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        sp_module.SIGNAL_TYPE_TO_FACTORY,
        "cache_invalidation_elevated",
        _exploding_factory,
    )

    proposals = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
    # The invalidation factory blows up → its branch returns nothing;
    # hit_rate still emits.
    assert len(proposals) == 1
    assert proposals[0].trigger_observations == ["cache_hit_rate_low:admin_bot"]


def test_observe_returns_empty_when_no_signals(tmp_path):
    proposals = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert proposals == []


# ── Suppression: operator pinned cacheRetention=short via intent ─────────────


def _record_intent(shared_dir: Path, bot_id: str, value: str = "short") -> None:
    """Drive evolve_admin.config_intent.set_intent against the tmp dir."""
    from evolve_admin.config_intent import set_intent

    set_intent(
        bot_id, INTENT_FIELD_PATH, value,
        reason="operator deliberately chose short",
        set_by="operator",
        actor="operator",
        shared_dir=shared_dir,
    )


def test_observe_suppresses_typed_proposal_when_operator_pinned_short(
    tmp_path, monkeypatch,
):
    """When the operator has recorded a config-intent pinning
    ``cacheRetention=short``, the autonomous fix must NOT emit — that
    would override the deliberate choice. Falls through to the
    Investigation fallback so the operator still sees the firing
    signal and can act if their intent is stale.
    """
    # Don't sync to network.json (operator intent v1 always touches a
    # mirror; for the test we sandbox to tmp_path only).
    monkeypatch.setattr(
        "evolve_admin.config_intent._sync_inline_mirror",
        lambda *a, **k: None,
    )
    _record_intent(tmp_path, "admin_bot", value="short")
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )

    proposals = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    # NOT UpdateAgentDefaults — the operator's intent suppressed the
    # autonomous path. Fallback Investigation surfaces the finding.
    assert proposals[0].action.kind == "Investigation"


def test_observe_does_not_suppress_when_operator_pinned_long(
    tmp_path, monkeypatch,
):
    """An intent pinning ``long`` (the value we'd propose) doesn't
    block the autonomous fix — it just means the operator agrees with
    us. The autonomous path is still the cleanest expression."""
    monkeypatch.setattr(
        "evolve_admin.config_intent._sync_inline_mirror",
        lambda *a, **k: None,
    )
    _record_intent(tmp_path, "admin_bot", value="long")
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )

    proposals = observe(CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path))
    assert len(proposals) == 1
    assert proposals[0].action.kind == "UpdateAgentDefaults"


def test_observe_consult_config_intent_false_bypasses_check(
    tmp_path, monkeypatch,
):
    """Test hook for the runner — when consult_config_intent is False,
    the gate is skipped (used by the unit-test path that doesn't want
    the intent system in scope)."""
    monkeypatch.setattr(
        "evolve_admin.config_intent._sync_inline_mirror",
        lambda *a, **k: None,
    )
    _record_intent(tmp_path, "admin_bot", value="short")
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(CacheTtlTunerContext(
        bot_id="admin_bot",
        shared_dir=tmp_path,
        consult_config_intent=False,
    ))
    assert len(proposals) == 1
    # Gate bypassed, autonomous fix emits despite intent.
    assert proposals[0].action.kind == "UpdateAgentDefaults"


# ── Suppression: proposal_history dedup window ───────────────────────────────


def _write_prior_proposal(
    shared_dir: Path,
    *,
    subdir: str,
    proposal_id: str,
    bot_id: str,
    cause_key: str = CAUSE_KEY_TTL_TOO_SHORT,
    status: str = "applied",
    created_at: str,
) -> None:
    """Drop a minimal proposal JSON the proposal_history reader can
    parse. Mirrors the helper in test_proposal_history.py."""
    target_dir = shared_dir / "proposals" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": proposal_id,
        "bot_id": bot_id,
        "generator_id": "cache_ttl_tuner",
        "dimension": "efficiency",
        "trigger_observations": [],
        "provenance": {
            "technique": "cache_ttl_tuner.cache_invalidation_elevated",
            "signals": {
                "root_cause_attribution": {
                    "cause_key": cause_key,
                    "headline": "x",
                    "confidence": 0.85,
                    "primary_target": INTENT_FIELD_PATH,
                    "evidence": {},
                },
            },
            "confidence": 0.85,
        },
        "problem": "p",
        "action": {
            "kind": "UpdateAgentDefaults",
            "bot_id": bot_id,
            "fields": {CACHE_RETENTION_DOTPATH: "long"},
        },
        "risk_tag": {
            "blast_radius": "bot",
            "reversibility": "auto",
            "touches": ["model_config"],
        },
        "claim": None,
        "revert_on_failure": None,
        "approval_audience": "pod_operator",
        "urgency": "hygiene",
        "admin_surface_summary": "s",
        "conversational_pitch": None,
        "guardian_annotations": [],
        "conflicts_with": [],
        "status": status,
        "snoozed_until": None,
        "history": [],
        "revisions": [],
        "adjacency_type": None,
        "motivating_signals": [],
        "schema_version": 1,
        "created_at": created_at,
        "signature": "",
    }
    (target_dir / f"{proposal_id}.json").write_text(json.dumps(payload))


def test_observe_suppresses_when_recent_applied_proposal_exists(tmp_path):
    """A Proposal that already landed (status=applied) within the dedup
    window should suppress re-emission. Let the fix bake — if invalidation
    persists past the verify window, the next run after PROPOSAL_HISTORY_WINDOW_DAYS
    will re-emit naturally.
    """
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _write_prior_proposal(
        tmp_path,
        subdir="applied",
        proposal_id="prior_p1",
        bot_id="admin_bot",
        status="applied",
        created_at=recent,
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path, now=now)
    )
    assert len(proposals) == 1
    assert proposals[0].action.kind == "Investigation"


def test_observe_suppresses_when_recent_dismissed_proposal_exists(tmp_path):
    """An archived dismissed Proposal within the window also suppresses
    — re-emitting after the operator dismissed it is noise."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _write_prior_proposal(
        tmp_path,
        subdir="archived",
        proposal_id="prior_p2",
        bot_id="admin_bot",
        status="dismissed",
        created_at=recent,
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path, now=now)
    )
    assert len(proposals) == 1
    assert proposals[0].action.kind == "Investigation"


def test_observe_re_emits_after_dedup_window_expires(tmp_path):
    """A Proposal older than the dedup window does NOT suppress — the
    generator gets a fresh shot at the same finding."""
    now = datetime.now(timezone.utc)
    ancient = (now - timedelta(days=PROPOSAL_HISTORY_WINDOW_DAYS + 5)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    _write_prior_proposal(
        tmp_path,
        subdir="archived",
        proposal_id="ancient",
        bot_id="admin_bot",
        status="dismissed",
        created_at=ancient,
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path, now=now)
    )
    assert len(proposals) == 1
    assert proposals[0].action.kind == "UpdateAgentDefaults"


def test_observe_dedup_ignores_other_bots_history(tmp_path):
    """A prior proposal for a different bot doesn't block this bot's
    autonomous fix."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _write_prior_proposal(
        tmp_path,
        subdir="applied",
        proposal_id="prior_other",
        bot_id="team_bot_c",
        status="applied",
        created_at=recent,
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path, now=now)
    )
    assert len(proposals) == 1
    assert proposals[0].action.kind == "UpdateAgentDefaults"


def test_observe_dedup_ignores_other_cause_keys(tmp_path):
    """A prior proposal with a different cause_key doesn't block — the
    14-day window only covers the same root cause."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _write_prior_proposal(
        tmp_path,
        subdir="applied",
        proposal_id="prior_other_cause",
        bot_id="admin_bot",
        cause_key="some_other_cause",
        status="applied",
        created_at=recent,
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(
        CacheTtlTunerContext(bot_id="admin_bot", shared_dir=tmp_path, now=now)
    )
    assert len(proposals) == 1
    assert proposals[0].action.kind == "UpdateAgentDefaults"


def test_observe_consult_proposal_history_false_bypasses_check(tmp_path):
    """Test hook — the dedup gate honors the context flag."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _write_prior_proposal(
        tmp_path,
        subdir="applied",
        proposal_id="recent",
        bot_id="admin_bot",
        status="applied",
        created_at=recent,
    )
    _write_signal(
        tmp_path,
        bot_id="admin_bot",
        sig_type="cache_invalidation_elevated",
        details=_default_invalidation_details(),
    )
    proposals = observe(CacheTtlTunerContext(
        bot_id="admin_bot",
        shared_dir=tmp_path,
        consult_proposal_history=False,
        now=now,
    ))
    assert len(proposals) == 1
    assert proposals[0].action.kind == "UpdateAgentDefaults"


# ── Estimated savings (PR H) ──────────────────────────────────────────────────


def test_estimate_savings_formula_matches_documented_heuristic():
    """The heuristic in the module comment is the formula we ship.

    The proposal's downstream UI tooltip references this exact
    computation, so a silent drift between the docstring and the
    behavior would mislead operators.
    """
    bot_7d_cost = 7.67  # team-bot-a session 3d5cde22 reference incident
    ratio = 0.30
    out = _estimate_savings_for_invalidation(
        invalidated_ratio=ratio, bot_7d_cost_usd=bot_7d_cost,
    )
    expected = (
        bot_7d_cost
        * ratio
        * CACHE_WRITE_FRACTION_ON_INVALIDATED
        * FRACTION_REMEDIABLE_BY_TTL_BUMP
    )
    assert out == expected


def test_estimate_savings_capped_at_7d_cost():
    """Savings can't exceed what the bot actually spent — sanity floor."""
    out = _estimate_savings_for_invalidation(
        invalidated_ratio=10.0,  # impossible, but stress-tests the cap
        bot_7d_cost_usd=3.0,
    )
    assert out == 3.0  # capped at the 7d cost


def test_estimate_savings_returns_none_when_cost_unavailable():
    """No cost data → no estimate. Proposal still ships, just no chip."""
    assert _estimate_savings_for_invalidation(
        invalidated_ratio=0.30, bot_7d_cost_usd=None,
    ) is None
    assert _estimate_savings_for_invalidation(
        invalidated_ratio=0.30, bot_7d_cost_usd=0.0,
    ) is None


def test_estimate_savings_returns_none_when_ratio_non_positive():
    """No invalidations → nothing to save. Don't manufacture a number."""
    assert _estimate_savings_for_invalidation(
        invalidated_ratio=0.0, bot_7d_cost_usd=5.0,
    ) is None


def test_invalidation_factory_populates_savings_when_cost_lookup_succeeds(monkeypatch):
    """End-to-end: a factory call with a stubbed cost lookup attaches savings."""

    def _fake_cost(bot_id, *, days, shared_dir=None):
        assert bot_id == "team_bot_a"
        assert days == 7
        return 7.67

    monkeypatch.setattr(sp_module, "_bot_cost_over_window", _fake_cost)

    sig = _invalidation_signal(bot_id="team_bot_a", ratio=0.30)
    p = make_cache_invalidation_elevated_proposal(sig)
    # Heuristic: 7.67 × 0.30 × 0.92 × 0.50 ≈ 1.06
    assert p.estimated_savings_usd is not None
    assert abs(p.estimated_savings_usd - 1.058) < 0.01


def test_invalidation_factory_savings_none_when_cost_lookup_fails(monkeypatch):
    """Fail-open: cost-ledger error returns None, proposal still ships."""
    monkeypatch.setattr(
        sp_module, "_bot_cost_over_window",
        lambda bot_id, *, days, shared_dir=None: None,
    )
    sig = _invalidation_signal(bot_id="team_bot_a", ratio=0.30)
    p = make_cache_invalidation_elevated_proposal(sig)
    assert p.estimated_savings_usd is None
    # The proposal still has every other field — only the chip is missing.
    assert p.bot_id == "team_bot_a"
    # PR F retargeted this factory to UpdateAgentDefaults (not Investigation);
    # the savings field independence from the typed-action path is what we're
    # verifying — cost-lookup failure must not degrade to a different action.
    assert p.action.kind == "UpdateAgentDefaults"


def test_invalidation_factory_savings_chip_above_display_floor(monkeypatch):
    """For a high-cost bot, the savings estimate clears the UI floor.

    The UI hides chips for estimates ≤ $0.50; a meaningful estimate
    should comfortably clear that floor on a bot with real spend.
    """
    # $40/week bot at 30% invalidation → ~$5.52/wk savings — well above
    # the $0.50 display floor.
    monkeypatch.setattr(
        sp_module, "_bot_cost_over_window",
        lambda bot_id, *, days, shared_dir=None: 40.0,
    )
    sig = _invalidation_signal(ratio=0.30)
    p = make_cache_invalidation_elevated_proposal(sig)
    assert p.estimated_savings_usd is not None
    assert p.estimated_savings_usd > 0.5


def test_hit_rate_factory_does_not_populate_savings():
    """The hit-rate-low Proposal is a prompt-structure investigation,
    not a knob with a clean money model — it must leave the savings
    field as None per the PR H scope.
    """
    sig = _hit_rate_signal()
    p = make_cache_hit_rate_low_proposal(sig)
    assert p.estimated_savings_usd is None
