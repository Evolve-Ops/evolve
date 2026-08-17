"""Unit tests for model_liveness — the auto-upgrade Phase 2 liveness probe.

Cover the probe's invocation shape (the gateway agent-turn path, --model
override, no --deliver / no --local), every verdict class, the #3489
regression shape (resolver says available, dispatch fails → verdict failed),
the Phase 1 pending-guard clearing helper, and the Signal fire/sweep/fail-safe
behaviour against a real signal store on a tmp shared dir.

Everything runs through injected fakes — no subprocess, no network, no
sudo, per the seam-factory convention.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_liveness as ml  # noqa: E402
from model_auto_upgrade import UpgradeDecision  # noqa: E402
from signals import store as signals_store  # noqa: E402

TARGET = "anthropic/claude-opus-5-0"
TARGET_BARE = "claude-opus-5-0"


# ── Fakes ─────────────────────────────────────────────────────────────────────


def _envelope(model: str | None = TARGET_BARE, text: str = "OK",
              status: str = "ok", error: str | None = None) -> dict:
    """A faithful `openclaw agent --json` envelope (the shape evo.proxy
    documents against OC 2026.5+)."""
    payload: dict = {
        "runId": "run-1",
        "status": status,
        "result": {
            "payloads": [{"text": text, "mediaUrl": None}],
            "meta": {"durationMs": 42},
        },
    }
    if model is not None:
        payload["result"]["meta"]["agentMeta"] = {
            "sessionId": "s", "model": model,
        }
    if error is not None:
        payload["error"] = error
    return payload


class FakeRunner:
    """Records every ProbeRequest and returns a canned AgentRunResult."""

    def __init__(self, result: ml.AgentRunResult):
        self.result = result
        self.requests: list[ml.ProbeRequest] = []

    def __call__(self, req: ml.ProbeRequest) -> ml.AgentRunResult:
        self.requests.append(req)
        return self.result


def _eligible_decision(latest: str = TARGET) -> UpgradeDecision:
    """An eligible Phase 1 decision, exactly as `_judge` emits it: no holds,
    liveness pending."""
    return UpgradeDecision(
        provider="anthropic",
        family="claude-opus",
        current_model="anthropic/claude-opus-4-8",
        latest_model=latest,
        rung_slug="opus-class",
        roles=["max"],
        eligible=True,
        pending_guards=("liveness",),
        scopes=["pod"],
        enabled_scopes=["pod"],
    )


# ── Invocation shape ──────────────────────────────────────────────────────────


def test_cli_args_use_gateway_agent_turn_with_model_override():
    args = ml.ProbeRequest(
        bot_id="team_bot_a", model=TARGET, session_id="sid",
    ).cli_args
    assert args[0] == "agent"
    assert args[args.index("--model") + 1] == TARGET
    assert args[args.index("--agent") + 1] == ml.DEFAULT_AGENT
    assert "--json" in args
    # Thinking off + a one-line message is the whole cost-control story
    # (no output-cap flag exists on `openclaw agent`).
    assert args[args.index("--thinking") + 1] == "off"
    assert args[args.index("--message") + 1] == ml.PROBE_MESSAGE
    # Load-bearing absences: no --deliver (the reply must go nowhere) and no
    # --local (the probe must ride the Gateway dispatch path — the path #3489
    # broke — not an in-process embedded run that skips it).
    assert "--deliver" not in args
    assert "--local" not in args


def test_probe_uses_fresh_synthetic_session_per_run():
    runner = FakeRunner(ml.AgentRunResult(payload=_envelope()))
    ml.probe_model("team_bot_a", TARGET, runner=runner)
    ml.probe_model("team_bot_a", TARGET, runner=runner)
    sids = [r.session_id for r in runner.requests]
    assert all(s.startswith("evolve-liveness-") for s in sids)
    assert sids[0] != sids[1]


# ── Verdicts ──────────────────────────────────────────────────────────────────


def test_verdict_ok_when_target_itself_answers():
    runner = FakeRunner(ml.AgentRunResult(payload=_envelope(), latency_ms=321))
    v = ml.probe_model("team_bot_a", TARGET, runner=runner)
    assert v.ok and v.code == ml.VERDICT_OK
    assert v.observed_model == TARGET_BARE
    assert v.latency_ms == 321
    assert not v.proven_dead


def test_verdict_ok_across_date_suffix_alias():
    # Providers may report the dated snapshot of the rolling id they served.
    runner = FakeRunner(ml.AgentRunResult(
        payload=_envelope(model=f"{TARGET_BARE}-20260201"),
    ))
    assert ml.probe_model("b", TARGET, runner=runner).ok


def test_verdict_dispatch_failed_when_cli_produces_nothing():
    runner = FakeRunner(ml.AgentRunResult(
        payload=None, errors=["HTTP 401 Incorrect API key provided"],
    ))
    v = ml.probe_model("team_bot_a", TARGET, runner=runner)
    assert v.code == ml.VERDICT_DISPATCH_FAILED and v.proven_dead
    assert v.error == "cli-failure"
    assert "401" in v.detail


def test_verdict_dispatch_failed_on_error_status():
    runner = FakeRunner(ml.AgentRunResult(
        payload=_envelope(status="error", error="model fetch failed"),
    ))
    v = ml.probe_model("b", TARGET, runner=runner)
    assert v.code == ml.VERDICT_DISPATCH_FAILED
    assert v.error == "status:error"
    assert "model fetch failed" in v.detail


def test_verdict_dispatch_failed_on_empty_reply():
    runner = FakeRunner(ml.AgentRunResult(payload=_envelope(text="")))
    v = ml.probe_model("b", TARGET, runner=runner)
    assert v.code == ml.VERDICT_DISPATCH_FAILED
    assert v.error == "empty-reply"


def test_verdict_rerouted_when_turn_served_by_other_model():
    # The OC 2026.7 silent-override class: the turn "succeeds" but on a
    # different model. That proves nothing about the target — worse, it
    # proves routing to the target does not reach it.
    runner = FakeRunner(ml.AgentRunResult(
        payload=_envelope(model="claude-haiku-4-5"),
    ))
    v = ml.probe_model("b", TARGET, runner=runner)
    assert v.code == ml.VERDICT_REROUTED and v.proven_dead
    assert v.observed_model == "claude-haiku-4-5"


def test_verdict_unverified_when_no_model_reported():
    runner = FakeRunner(ml.AgentRunResult(payload=_envelope(model=None)))
    v = ml.probe_model("b", TARGET, runner=runner)
    assert v.code == ml.VERDICT_UNVERIFIED
    assert not v.ok and not v.proven_dead  # neither fires nor clears


# ── The #3489 regression shape ────────────────────────────────────────────────


def test_3489_resolver_says_available_but_dispatch_fails():
    """The motivating incident: `models list` reports the model
    `available: true, missing: false` while every real request 401s at the
    wrong transport. The probe must judge from the DISPATCH round-trip alone —
    a green resolver row must not produce a green verdict."""
    resolver_row = {"id": TARGET_BARE, "available": True, "missing": False}
    assert resolver_row["available"] is True  # the lie the probe must ignore

    runner = FakeRunner(ml.AgentRunResult(
        payload=None,
        errors=["HTTP 401 Incorrect API key provided: AIzaSy... "
                "(POST https://api.openai.com/v1/responses)"],
    ))
    v = ml.probe_model("team_bot_a", TARGET, runner=runner)
    assert v.proven_dead  # dispatch verdict, regardless of resolver claims

    # And the Phase 1 seam: the eligible decision goes HELD, with the reason
    # surfaced like conditions 1-5+7 — never applied.
    decision = _eligible_decision()
    judged = ml.apply_liveness_guard(decision, v)
    assert judged.eligible is False
    assert judged.holds[0].code == ml.HOLD_LIVENESS_FAILED
    assert "available" in judged.holds[0].message  # names the disagreement


# ── Guard clearing (the Phase 1 seam) ─────────────────────────────────────────


def test_guard_cleared_on_ok_verdict():
    decision = _eligible_decision()
    v = ml.ProbeVerdict(bot_id="b", model=TARGET, code=ml.VERDICT_OK,
                        observed_model=TARGET_BARE)
    out = ml.apply_liveness_guard(decision, v)
    assert out.eligible is True
    assert out.pending_guards == ()
    assert out.holds == []
    # Purity: the input decision is untouched.
    assert decision.pending_guards == ("liveness",)


def test_guard_failed_verdict_holds_with_transient_reason():
    v = ml.ProbeVerdict(
        bot_id="b", model=TARGET, code=ml.VERDICT_DISPATCH_FAILED,
        error="cli-failure", detail="agent turn produced no result (HTTP 401)",
    )
    out = ml.apply_liveness_guard(_eligible_decision(), v)
    assert out.eligible is False
    assert out.pending_guards == ()  # the guard ran; it has an answer
    hold = out.holds[0]
    assert hold.code == ml.HOLD_LIVENESS_FAILED
    assert not hold.durable  # transient: the next cycle re-probes
    assert hold.detail["verdict"]["code"] == ml.VERDICT_DISPATCH_FAILED


def test_guard_rerouted_message_names_the_serving_model():
    v = ml.ProbeVerdict(
        bot_id="b", model=TARGET, code=ml.VERDICT_REROUTED,
        observed_model="claude-haiku-4-5", error="rerouted",
        detail="served by claude-haiku-4-5",
    )
    out = ml.apply_liveness_guard(_eligible_decision(), v)
    assert out.eligible is False
    assert "claude-haiku-4-5" in out.holds[0].message


def test_guard_unverified_keeps_liveness_pending():
    v = ml.ProbeVerdict(bot_id="b", model=TARGET, code=ml.VERDICT_UNVERIFIED)
    out = ml.apply_liveness_guard(_eligible_decision(), v)
    assert out.eligible is False
    assert out.pending_guards == ("liveness",)  # guard did NOT complete
    assert out.holds[0].code == ml.HOLD_LIVENESS_UNVERIFIED


def test_guard_refuses_mismatched_probe():
    v = ml.ProbeVerdict(bot_id="b", model="anthropic/claude-sonnet-5",
                        code=ml.VERDICT_OK)
    with pytest.raises(ValueError):
        ml.apply_liveness_guard(_eligible_decision(), v)


# ── Signals: fire / sweep / fail-safe ─────────────────────────────────────────


def _firing(shared_dir: Path) -> list:
    return [s for s in signals_store.iter_signals(shared_dir)
            if s.state == "firing" and s.producer == ml.PRODUCER]


def _dead_verdict(model: str = TARGET) -> ml.ProbeVerdict:
    return ml.ProbeVerdict(
        bot_id="team_bot_a", model=model, code=ml.VERDICT_DISPATCH_FAILED,
        error="cli-failure", detail="HTTP 401 at the wrong transport",
    )


def test_failure_fires_pod_scoped_signal(tmp_path):
    ml.emit_signals(tmp_path, [_dead_verdict()])
    sigs = _firing(tmp_path)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.type == ml.SIGNAL_TYPE
    assert sig.signature == f"{ml.PRODUCER}:{ml.SIGNAL_TYPE}:{TARGET_BARE}"
    assert sig.scope == "pod"
    assert sig.details["provider"] == "anthropic"
    assert TARGET_BARE in sig.title


def test_refire_bumps_existing_signal_not_a_new_one(tmp_path):
    ml.emit_signals(tmp_path, [_dead_verdict()])
    ml.emit_signals(tmp_path, [_dead_verdict()])
    assert len(_firing(tmp_path)) == 1


def test_recovery_sweep_resolves(tmp_path):
    ml.emit_signals(tmp_path, [_dead_verdict()])
    ok = ml.ProbeVerdict(bot_id="team_bot_a", model=TARGET,
                         code=ml.VERDICT_OK, observed_model=TARGET_BARE)
    ml.emit_signals(tmp_path, [ok])
    assert _firing(tmp_path) == []


def test_unverified_neither_fires_nor_sweeps(tmp_path):
    # Fail-safe: a probe that could not see must not page ("model down")
    # and must not archive a live outage either.
    ml.emit_signals(tmp_path, [_dead_verdict()])
    blind = ml.ProbeVerdict(bot_id="team_bot_a", model=TARGET,
                            code=ml.VERDICT_UNVERIFIED)
    ml.emit_signals(tmp_path, [blind])
    assert len(_firing(tmp_path)) == 1  # prior outage still firing

    other_blind = ml.ProbeVerdict(bot_id="team_bot_a",
                                  model="anthropic/claude-sonnet-5",
                                  code=ml.VERDICT_UNVERIFIED)
    ml.emit_signals(tmp_path, [other_blind, blind])
    assert len(_firing(tmp_path)) == 1  # and no new signal for the blind one


def test_dry_run_writes_nothing(tmp_path):
    ml.emit_signals(tmp_path, [_dead_verdict()], dry_run=True)
    assert list(signals_store.iter_signals(tmp_path)) == []


# The partial-coverage sweep hole (pass-1 review finding): the sweep may
# resolve ONLY signatures this run positively probed ok. An outage for a
# target the run never touched must survive every partial-run shape.


def test_spot_check_of_one_model_never_sweeps_another(tmp_path):
    ml.emit_signals(tmp_path, [_dead_verdict()])  # TARGET is down
    other_ok = ml.ProbeVerdict(
        bot_id="team_bot_a", model="anthropic/claude-haiku-4-5",
        code=ml.VERDICT_OK, observed_model="claude-haiku-4-5",
    )
    ml.emit_signals(tmp_path, [other_ok])  # --model spot check elsewhere
    assert len(_firing(tmp_path)) == 1  # TARGET's outage untouched


def test_zero_verdict_run_sweeps_nothing(tmp_path):
    ml.emit_signals(tmp_path, [_dead_verdict()])
    ml.emit_signals(tmp_path, [])  # zero-target run (enumeration came up empty)
    assert len(_firing(tmp_path)) == 1


def test_cap_dropped_dead_target_is_not_swept_as_recovered(tmp_path):
    dead_model = "google/gemini-3-pro"
    ml.emit_signals(tmp_path, [_dead_verdict(model=dead_model)])
    targets = [
        ml.ProbeTarget(bot_id="admin_bot", model="anthropic/claude-haiku-4-5"),
        ml.ProbeTarget(bot_id="admin_bot", model=dead_model),  # past the cap
    ]

    def ok_runner(req):
        return ml.AgentRunResult(
            payload=_envelope(model=req.model.split("/", 1)[-1]),
        )

    summary = ml.run_probes(
        tmp_path, _NETWORK, runner=ok_runner, targets=targets, max_targets=1,
    )
    assert [d["model"] for d in summary["dropped_by_cap"]] == [dead_model]
    assert len(_firing(tmp_path)) == 1  # the dropped target's outage stands


# ── Target selection ──────────────────────────────────────────────────────────


_NETWORK = {"primary": "admin_bot", "members": ["team_bot_a", "team_bot_b"]}


def _resolver_for(view_by_bot):
    def _resolver(network, bot_id):
        view = view_by_bot[bot_id]
        if isinstance(view, Exception):
            raise view
        return view
    return _resolver


def test_targets_dedupe_pod_wide_primary_first():
    shared = {
        "fast": {"resolvedModel": "anthropic/claude-haiku-4-5"},
        "max": {"resolvedModel": TARGET},
    }
    resolver = _resolver_for({
        "admin_bot": shared,
        "team_bot_a": shared,  # pod-defaults bot: identical resolution
        "team_bot_b": {  # Custom bot with one model of its own
            "fast": {"resolvedModel": "anthropic/claude-haiku-4-5"},
            "max": {"resolvedModel": "openai/gpt-5.2"},
        },
    })
    targets, dropped = ml.routed_probe_targets(_NETWORK, roles_resolver=resolver)
    assert dropped == []
    assert [(t.bot_id, t.model) for t in targets] == [
        ("admin_bot", "anthropic/claude-haiku-4-5"),
        ("admin_bot", TARGET),
        ("team_bot_b", "openai/gpt-5.2"),
    ]
    # Roles that share a model on the SAME bot merge into one target.
    assert targets[0].roles == ("fast",)


def test_targets_cap_reports_dropped_never_silently():
    view = {
        f"r{i}": {"resolvedModel": f"anthropic/claude-model-{i}"}
        for i in range(4)
    }
    resolver = _resolver_for({"admin_bot": view,
                              "team_bot_a": view, "team_bot_b": view})
    targets, dropped = ml.routed_probe_targets(
        _NETWORK, roles_resolver=resolver, max_targets=2,
    )
    assert len(targets) == 2
    assert len(dropped) == 2  # named, not discarded


def test_targets_skip_bot_whose_resolution_breaks():
    resolver = _resolver_for({
        "admin_bot": RuntimeError("unreadable"),
        "team_bot_a": {"max": {"resolvedModel": TARGET}},
        "team_bot_b": {},
    })
    targets, _ = ml.routed_probe_targets(_NETWORK, roles_resolver=resolver)
    assert [(t.bot_id, t.model) for t in targets] == [("team_bot_a", TARGET)]


# ── run_probes summary ────────────────────────────────────────────────────────


def test_run_probes_summary_counts_and_signals(tmp_path):
    targets = [
        ml.ProbeTarget(bot_id="admin_bot", model=TARGET, roles=("max",)),
        ml.ProbeTarget(bot_id="admin_bot",
                       model="anthropic/claude-haiku-4-5", roles=("fast",)),
    ]
    runner = FakeRunner(ml.AgentRunResult(
        payload=None, errors=["connect: ECONNREFUSED"],
    ))
    summary = ml.run_probes(tmp_path, _NETWORK, runner=runner, targets=targets)
    assert summary["ok"] is False
    assert summary["probed"] == 2 and summary["dead_count"] == 2
    assert len(summary["signals_firing"]) == 2
    assert len(_firing(tmp_path)) == 2

    # Recovery: same targets now answer → sweep clears both.
    ok_runner = FakeRunner(ml.AgentRunResult(payload=_envelope()))

    def per_target_runner(req):
        return ml.AgentRunResult(
            payload=_envelope(model=req.model.split("/", 1)[-1]),
        )

    summary = ml.run_probes(
        tmp_path, _NETWORK, runner=per_target_runner, targets=targets,
    )
    assert summary["ok"] is True and summary["dead_count"] == 0
    assert _firing(tmp_path) == []
    assert ok_runner.requests == []  # (unused helper; keeps intent explicit)


def test_run_probes_cap_applies_to_injected_targets(tmp_path):
    targets = [
        ml.ProbeTarget(bot_id="admin_bot", model=f"anthropic/m-{i}")
        for i in range(3)
    ]
    runner = FakeRunner(ml.AgentRunResult(payload=_envelope(model=None)))
    summary = ml.run_probes(
        tmp_path, _NETWORK, runner=runner, targets=targets, max_targets=2,
    )
    assert summary["probed"] == 2
    assert [d["model"] for d in summary["dropped_by_cap"]] == ["anthropic/m-2"]
