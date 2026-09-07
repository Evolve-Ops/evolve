"""Phase C — proposal-lifecycle pair (analyze, outcome) routes
through alerts.dispatcher.

The two remaining sources cover the proposal pipeline:

  analyze (proposals generated) → decisions.proposal_ready
  outcome (7-day "did this help?")→ decisions.proposal_outcome_checkin

The third source, ``apply`` (decisions.proposal_applied), was the retired
per-bot ``apply.py`` daemon; its routing cases went with it 2026-08-18
(internal/design-proposal-signing-key-2026-08-18.md). It had never emitted the
event on a live pod — its entire logged history is "No new proposals".

Tests pin per-source source/catalog_event/dedup_key invariants plus a
regression guard that subprocess.run isn't called for openclaw.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analyze  # noqa: E402
import outcome  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import (
        DispatchOutcome, DispatchResult, Severity,
    )

    captured: list = []

    def fake_send(*, shared_dir, network, source, severity,
                  message=None, payload=None,
                  dedup_key=None, catalog_event=None, **_kw):
        captured.append({
            "source": source, "message": message, "payload": payload,
            "severity": severity, "dedup_key": dedup_key,
            "catalog_event": catalog_event,
        })
        return DispatchOutcome(
            result=DispatchResult.SENT, source=source, severity=severity,
            dedup_key=dedup_key, catalog_event=catalog_event,
            channel="telegram", chat_id="12345",
        )

    monkeypatch.setattr(dispatcher, "send", fake_send)

    shared = tmp_path / "evolve"
    shared.mkdir()
    return {
        "captured": captured,
        "shared_dir": shared,
        "network": {"alerts": {"channel": "telegram", "chatId": "12345"}},
        "Severity": Severity,
        "DispatchResult": DispatchResult,
    }


# ── analyze ────────────────────────────────────────────────────────────────


def _inbox(proposal_id, target_bot, problem, confidence=0.8):
    """A proposal that WILL land in the Pending actionable inbox:
    surface=improvement + non-informational. Only these may fire the
    "proposal ready → Pending" push (see proposal_routing)."""
    return {"proposal_id": proposal_id, "target_bot": target_bot,
            "problem": problem, "confidence": confidence,
            "surface": "improvement"}


def test_analyze_routes_proposals_ready(env):
    proposals = [
        _inbox("prop_a", "team_bot_a", "Lower context cap", 0.9),
        _inbox("prop_b", "admin_bot", "Trim AGENTS.md size", 0.7),
    ]
    analyze.send_telegram_alert(
        proposals, env["network"], shared_dir=env["shared_dir"],
    )
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["source"] == "analyze"
    assert call["catalog_event"] == "decisions.proposal_ready"
    assert call["dedup_key"].startswith("analyze/proposals_ready/")
    assert call["severity"] == env["Severity"].INFO
    # Phase F5: payload-driven render. Catalog body_template renders
    # "📋 {count} new proposal{s} ready / {titles}" + ui_action.
    assert call["message"] is None
    assert call["payload"]["count"] == 2
    assert call["payload"]["s"] == "s"
    assert "team_bot_a" in call["payload"]["titles"]
    assert "admin_bot" in call["payload"]["titles"]


def test_analyze_empty_batch_does_not_dispatch(env):
    analyze.send_telegram_alert([], env["network"], shared_dir=env["shared_dir"])
    assert env["captured"] == []


def test_analyze_dedup_key_changes_with_batch(env):
    """Identical batches → same key (cooldown applies). Different
    batches → distinct keys (operator sees the new findings)."""
    proposals_a = [_inbox("p1", "team_bot_a", "trim", 0.8)]
    proposals_b = [_inbox("p2", "admin_bot", "cap", 0.9)]
    analyze.send_telegram_alert(proposals_a, env["network"], shared_dir=env["shared_dir"])
    analyze.send_telegram_alert(proposals_b, env["network"], shared_dir=env["shared_dir"])
    keys = [c["dedup_key"] for c in env["captured"]]
    assert keys[0] != keys[1]


# ── Surface-honest gating (the trust fix) ────────────────────────────────────


def test_analyze_only_counts_inbox_bound_proposals(env):
    """A mixed batch: one inbox-bound (improvement, non-informational), one
    husk (surface=null → routes to Alerts), one investigation (improvement but
    informational → routes to Observations). Only the inbox-bound one may drive
    the "ready → Pending" push — count and titles reflect ONLY it, and the
    dedup fingerprint is computed on the filtered list."""
    proposals = [
        _inbox("prop_inbox", "team_bot_a", "Lower context cap", 0.9),
        # husk: surface omitted entirely (reads as null) → Alerts
        {"proposal_id": "prop_husk", "target_bot": "admin_bot",
         "problem": "high maintenance ratio", "confidence": 0.7},
        # investigation: recommendable but informational → Observations
        {"proposal_id": "prop_inv", "target_bot": "team_bot_b",
         "problem": "investigate billing", "confidence": 0.8,
         "surface": "improvement", "informational": True},
    ]
    analyze.send_telegram_alert(
        proposals, env["network"], shared_dir=env["shared_dir"],
    )
    assert len(env["captured"]) == 1
    call = env["captured"][0]
    assert call["payload"]["count"] == 1
    assert call["payload"]["s"] == ""  # singular — only one survived the gate
    assert "team_bot_a" in call["payload"]["titles"]
    assert "admin_bot" not in call["payload"]["titles"]
    assert "team_bot_b" not in call["payload"]["titles"]

    # Fingerprint is over the FILTERED list: a batch of just the inbox-bound
    # proposal must produce the SAME dedup_key as the mixed batch above.
    env["captured"].clear()
    analyze.send_telegram_alert(
        [_inbox("prop_inbox", "team_bot_a", "Lower context cap", 0.9)],
        env["network"], shared_dir=env["shared_dir"],
    )
    assert call["dedup_key"] == env["captured"][0]["dedup_key"]


def test_analyze_all_non_inbox_batch_fires_nothing(env):
    """A batch with no inbox-bound proposal must fire ZERO pushes — never a
    "0 ready", never a push pointing at an empty Pending inbox."""
    proposals = [
        # husk (surface null)
        {"proposal_id": "h1", "target_bot": "admin_bot",
         "problem": "high maintenance ratio", "confidence": 0.7},
        # legacy investigation (improvement + informational)
        {"proposal_id": "i1", "target_bot": "team_bot_a",
         "problem": "investigate resolution drop", "confidence": 0.8,
         "surface": "improvement", "informational": True},
    ]
    analyze.send_telegram_alert(
        proposals, env["network"], shared_dir=env["shared_dir"],
    )
    assert env["captured"] == []


# ── outcome ────────────────────────────────────────────────────────────────


def test_outcome_routes_checkin(env):
    outcome_record = {
        "outcome_id": "out_abc1234567",
        "target_bot": "admin_bot",
        "proposal_summary": "Reduce context tokens",
        "applied_date": "2026-05-03",
    }
    ok = outcome.send_checkin_telegram(
        outcome_record, env["network"], shared_dir=env["shared_dir"],
    )
    assert ok is True
    call = env["captured"][0]
    assert call["source"] == "outcome"
    assert call["catalog_event"] == "decisions.proposal_outcome_checkin"
    assert call["dedup_key"] == "outcome/checkin/out_abc1234567"
    assert call["severity"] == env["Severity"].INFO
    # Phase F5: payload-driven; catalog renders "📋 How did this go? /
    # Bot: admin_bot  Change: Reduce context tokens / Applied: 2026-05-03"
    # + bot_action("yes / no / details").
    assert call["message"] is None
    assert call["payload"] == {
        "bot_id": "admin_bot",
        "summary": "Reduce context tokens",
        "apply_date": "2026-05-03",
    }


def test_outcome_returns_false_on_dispatcher_suppression(env, monkeypatch):
    """If the operator muted decisions.proposal_outcome_checkin, the
    dispatcher returns SUPPRESSED_DISABLED → send_checkin_telegram must
    return False so the caller's flag-write doesn't fire (a re-enable
    later → next eligible tick re-sends)."""
    from evolve_admin.alerts import dispatcher
    from evolve_admin.alerts.dispatcher import DispatchOutcome, DispatchResult, Severity

    def suppressed_send(*, source, severity, dedup_key=None, **_kw):
        return DispatchOutcome(
            result=DispatchResult.SUPPRESSED_DISABLED,
            source=source, severity=severity, dedup_key=dedup_key,
            error="subscription_off:decisions.proposal_outcome_checkin",
        )
    monkeypatch.setattr(dispatcher, "send", suppressed_send)

    outcome_record = {"outcome_id": "out_x", "target_bot": "team_bot_a",
                      "proposal_summary": "x", "applied_date": "2026-05-03"}
    ok = outcome.send_checkin_telegram(
        outcome_record, env["network"], shared_dir=env["shared_dir"],
    )
    assert ok is False


# ── Dispatcher source registration ─────────────────────────────────────────


def test_dispatcher_recognizes_all_three_sources():
    """All three sources must be in dispatcher.known_sources for the
    Config-page toggles to exist."""
    from evolve_admin.alerts.dispatcher import known_sources
    sources = set(known_sources())
    assert "analyze" in sources
    assert "apply" in sources
    assert "outcome" in sources
