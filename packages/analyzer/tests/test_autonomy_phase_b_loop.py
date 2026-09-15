"""Phase B end-to-end loop against the synthetic-pod harness.

The live-pod email-promotion proof is blocked (no pod bot has an email
integration yet — only evo_tools MCP servers exist as of 2026-06-11),
so this suite IS the Phase B proof artifact: the full loop
backfill → operator confirm → ledger streak → candidate Signal →
promotion Proposal → operator apply → render → drift-clean, plus the
demotion arc (cap → pause → probe → auto-demote → restore), against
the same synthetic tree Phase A used. All emissions run through the
real Signal store and the monitor's shared ``emit_findings`` helper so
signatures, dedup, and sweep-resolve behave exactly as on a pod.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autonomy import actions_ledger as _ledger
from autonomy import backfill as _backfill
from autonomy import coherence as _coherence
from autonomy import limits as _limits
from autonomy import reflex as _reflex
from autonomy import store as _store
from autonomy import streaks as _streaks
from generators.autonomy_promoter.observe import AutonomyPromoterContext, observe
from permissions import monitor as _mon
from signals import store as _signals_store


BOT = "alpha"
IID = "google_workspace"
SEND = "send_gmail_message"
NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
EARLY_ISO = "2026-05-20T00:00:00Z"


@pytest.fixture
def pod(tmp_path: Path) -> dict:
    shared = tmp_path / "shared"
    shared.mkdir()
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "mcp": {"servers": {IID: {"command": "uvx", "args": ["workspace-mcp"]}}},
    }))
    return {"shared": shared, "home": home}


def _write_sends(shared: Path, *, n: int, days: int, result: str = "ok",
                 ts_prefix_hour: int = 8) -> None:
    root = _ledger.ledger_dir(shared, BOT)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        day = (NOW - timedelta(days=days - 1 - (i % days))).date().isoformat()
        rec = {"ts": f"{day}T{ts_prefix_hour + (i % 9):02d}:00:00Z",
               "integration_id": IID, "tool_name": SEND,
               "result": result, "session_id": "s", "turn_id": f"t{i}"}
        with (root / f"{_ledger.LEDGER_FILE_PREFIX}{day}.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")


def _firing(shared: Path, sig_type: str) -> list:
    return [
        s for s in _signals_store.iter_active(shared, state="firing")
        if s.type == sig_type
    ]


def _emit_and_sweep(shared: Path, findings: list[dict]) -> None:
    """The monitor/daemon emission contract: observe everything, then
    sweep the autonomy types for this bot against what was derived."""
    for f in findings:
        f.setdefault("bot_id", BOT)
    kept = _mon.emit_findings(shared, findings)
    _signals_store.sweep_resolve(
        shared,
        producer=_mon.PRODUCER,
        kept_signatures=kept,
        types=set(_mon.AUTONOMY_SIGNAL_TYPES),
        bot_ids={BOT},
    )


def _derive_all(shared: Path, home: Path, now: datetime = NOW) -> list[dict]:
    """The monitor's autonomy pass, against the synthetic tree."""
    findings: list[dict] = []
    findings.extend(_backfill.ensure_backfilled(shared, BOT, home_override=home))
    limit_f, _ = _limits.evaluate_bot(shared, BOT, home_override=home, now=now)
    findings.extend(limit_f)
    reflex_f, _ = _reflex.run_bot(shared, BOT, home_override=home, now=now)
    findings.extend(reflex_f)
    streak_f, _ = _streaks.candidates(shared, BOT, now=now)
    findings.extend(streak_f)
    findings.extend(
        _coherence.check_bot(BOT, shared, home_override=home, now=now)
    )
    return findings


def test_full_promotion_loop(pod: dict, monkeypatch):
    shared, home = pod["shared"], pod["home"]

    # 1) Backfill: send reachable, nothing recorded → observe-only entry
    #    + suggestion Signal.
    _emit_and_sweep(shared, _derive_all(shared, home))
    posture = _store.load(shared, BOT).integrations[IID]
    assert posture.set_by["actor"] == _store.ACTOR_BACKFILL
    assert len(_firing(shared, "autonomy_backfill_review")) == 1

    # 2) Operator confirms "Asks first" — the first deliberate act.
    #    (set_at back-dated so the streak window has room.)
    monkeypatch.setattr(_store, "now_iso", lambda: EARLY_ISO)
    _store.set_posture(
        shared, BOT, IID, rung="act_with_approval",
        actor=_store.ACTOR_OPERATOR_UI,
    )
    monkeypatch.undo()
    _emit_and_sweep(shared, _derive_all(shared, home))
    assert _firing(shared, "autonomy_backfill_review") == []

    # 3) The bot works at "Asks first": 12 sends across 8 days land in
    #    the bot-side ledger → streak condition fires.
    _write_sends(shared, n=12, days=8)
    _emit_and_sweep(shared, _derive_all(shared, home))
    candidates = _firing(shared, "autonomy_promotion_candidate")
    assert len(candidates) == 1

    # 4) The generator turns the Signal into a typed Proposal.
    proposals = observe(AutonomyPromoterContext(
        bot_ids=[BOT], shared_dir=shared, now=NOW,
    ))
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.motivating_signals == [candidates[0].id]

    # 5) Every auto-approve lane refuses the promotion.
    from arbiter.routing import is_autonomous_eligible
    from eligibility import classify_proposal
    assert is_autonomous_eligible(proposal) is False
    assert classify_proposal(proposal.to_dict()).tier_floor == "ask"

    # 6) The operator approves; the applier is the deliberate act.
    from arbiter.appliers import get_applier
    applier = get_applier("UpdateAutonomyPosture")
    applier.shared_override = shared
    applier.home_override = home
    result = applier.apply(proposal.action, BOT, proposal_id=proposal.id)
    assert result.ok, result.message
    posture = _store.load(shared, BOT).integrations[IID]
    assert posture.rung == "autonomous_within_rules"
    assert posture.set_by["actor"] == f"proposal:{proposal.id}"
    # Intent file stays bot-readable (the session_surface contract).
    assert (_store.autonomy_path(shared, BOT).stat().st_mode & 0o777) == 0o644

    # 7) Render landed and the audit agrees: drift-clean, and the
    #    streak Signal sweep-resolves (the rung moved).
    assert _coherence.check_bot(BOT, shared, home_override=home) == []
    _emit_and_sweep(shared, _derive_all(shared, home))
    assert _firing(shared, "autonomy_promotion_candidate") == []
    assert _firing(shared, "autonomy_posture_drift") == []


def test_full_demotion_arc(pod: dict, monkeypatch):
    shared, home = pod["shared"], pod["home"]

    # Rung 3 with a tight cap, set deliberately.
    monkeypatch.setattr(_store, "now_iso", lambda: EARLY_ISO)
    _store.set_posture(
        shared, BOT, IID, rung="autonomous_within_rules",
        rules={"actions_per_day": 2}, actor=_store.ACTOR_OPERATOR_UI,
    )
    monkeypatch.undo()

    # 1) Cap hit → pause + autonomy_limit_hit fires; pause is rendered.
    _write_sends(shared, n=2, days=1)
    _emit_and_sweep(shared, _derive_all(shared, home))
    assert len(_firing(shared, "autonomy_limit_hit")) == 1
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert f"mcp__{IID}__{SEND}" in cfg["tools"]["deny"]

    # 2) The bot probes the wall: 3 failed attempts after the pause →
    #    auto-demotion, one rung, 🔴 alert with the restore payload.
    paused_at = _limits.load_limits(shared, BOT)[IID]["paused_at"]

    def _after(iso_z: str, seconds: int) -> str:
        dt = datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc,
        ) + timedelta(seconds=seconds)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    root = _ledger.ledger_dir(shared, BOT)
    day = paused_at[:10]
    with (root / f"{_ledger.LEDGER_FILE_PREFIX}{day}.jsonl").open("a") as f:
        for i in range(3):
            f.write(json.dumps({
                "ts": _after(paused_at, i + 1), "integration_id": IID,
                "tool_name": SEND, "result": "error",
                "session_id": "s", "turn_id": f"p{i}",
            }) + "\n")
    _emit_and_sweep(shared, _derive_all(shared, home))
    posture = _store.load(shared, BOT).integrations[IID]
    assert posture.rung == "act_with_approval"
    # The step-down must NOT lift the day's pause wall: send stays
    # mechanically denied even though "Asks first" normally leaves it
    # reachable (second-pass review finding).
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert f"mcp__{IID}__{SEND}" in cfg["tools"]["deny"]
    demoted = _firing(shared, "autonomy_demoted")
    assert len(demoted) == 1
    assert demoted[0].severity == "alert"
    rem = demoted[0].remediation
    assert rem is not None and rem.kind == "restore_autonomy_posture"
    assert rem.params["rules"] == {"actions_per_day": 2}

    # 3) The alert keeps firing across passes until the operator acts.
    _emit_and_sweep(shared, _derive_all(shared, home))
    assert len(_firing(shared, "autonomy_demoted")) == 1

    # 4) Restore (= promotion, via the remediation params). The reflex
    #    must NOT re-demote off the same pre-restore probing — the
    #    operator just ruled on exactly that evidence. set_at is pinned
    #    safely after the probe timestamps to keep the assertion
    #    deterministic regardless of wall-clock.
    monkeypatch.setattr(_store, "now_iso", lambda: _after(paused_at, 600))
    _store.set_posture(
        shared, BOT, IID,
        rung=rem.params["rung"], rules=dict(rem.params["rules"]),
        actor=_store.ACTOR_OPERATOR_UI,
        expected_current_rung=rem.params["expected_current_rung"],
        note="restored after reviewing the trigger",
    )
    monkeypatch.undo()
    _emit_and_sweep(shared, _derive_all(shared, home))
    assert _firing(shared, "autonomy_demoted") == []
    posture = _store.load(shared, BOT).integrations[IID]
    assert posture.rung == "autonomous_within_rules"
    assert posture.rules == {"actions_per_day": 2}
    # The daily-cap pause itself still stands for the rest of the day —
    # restoring the level does not refund the budget.
    assert len(_firing(shared, "autonomy_limit_hit")) == 1


def test_daemon_pass_emits_and_sweeps_with_seams(pod: dict, monkeypatch):
    """limits_daemon.run_pass with the limits/reflex seams injected —
    verifies the emission + type-scoped sweep contract without a real
    bot home (seam injection per house rules)."""
    shared = pod["shared"]
    import evolve_config
    from autonomy import limits_daemon

    monkeypatch.setattr(evolve_config, "get_members", lambda cfg: [BOT])
    monkeypatch.setattr(evolve_config, "get_primary", lambda cfg: None)

    finding = {
        "type": "autonomy_limit_hit", "severity": "warn",
        "signature_scope": f"{BOT}:{IID}",
        "title": "t", "body": "b",
        "details": {"bot_id": BOT, "integration_id": IID},
    }
    from autonomy import limits as limits_mod
    from autonomy import reflex as reflex_mod
    monkeypatch.setattr(
        limits_mod, "evaluate_bot",
        lambda *a, **k: ([dict(finding)], True),
    )
    monkeypatch.setattr(reflex_mod, "run_bot", lambda *a, **k: ([], True))

    summary = limits_daemon.run_pass(shared, {})
    assert summary["bots_checked"] == 1
    assert len(_firing(shared, "autonomy_limit_hit")) == 1

    # Condition clears → the daemon's scoped sweep resolves it…
    monkeypatch.setattr(limits_mod, "evaluate_bot", lambda *a, **k: ([], True))
    # …but a bot whose checks failed keeps its Signals (tooling failure
    # is never "condition cleared").
    monkeypatch.setattr(reflex_mod, "run_bot", lambda *a, **k: ([], False))
    limits_daemon.run_pass(shared, {})
    assert len(_firing(shared, "autonomy_limit_hit")) == 1

    monkeypatch.setattr(reflex_mod, "run_bot", lambda *a, **k: ([], True))
    summary = limits_daemon.run_pass(shared, {})
    assert summary["swept_resolved"] == 1
    assert _firing(shared, "autonomy_limit_hit") == []
