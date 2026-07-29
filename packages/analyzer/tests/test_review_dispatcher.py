"""Phase C — review.py routes its three alerts through alerts.dispatcher.

Pins the migration contract:

  _alert_rejection   → catalog_event=decisions.proposal_rejected
  _alert_reviewed    → catalog_event=decisions.proposal_ready
  _alert_quarantine  → catalog_event=security.proposal_quarantined

  All use source="review" so alerts.review.enabled gates the trio.
  Each dedup_key is per-proposal so distinct proposals all push.
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

import review  # noqa: E402


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

    return {
        "captured": captured,
        "shared_dir": tmp_path / "evolve",
        "config": {"alerts": {"channel": "telegram", "chatId": "12345"}},
        "Severity": Severity,
    }


def _proposal(target_bot="team_bot_a"):
    # Inbox-bound (surface=improvement, non-informational): the only shape that
    # may drive a "proposal ready → Pending" push from _alert_reviewed.
    return {
        "target_bot": target_bot,
        "type": "trim_context",
        "problem": "Lower context tokens by ~30%",
        "surface": "improvement",
    }


class _MockReview:
    """Minimal SecurityReview shape — only the fields the alert
    helpers actually read."""
    def __init__(self, result="approved", risk_level="low", risk_score=2,
                 flags=None, rejection_reason=""):
        self.result = result
        self.risk_level = risk_level
        self.risk_score = risk_score
        self.flags = flags or []
        self.rejection_reason = rejection_reason


def test_alert_rejection_routes_with_proposal_rejected(env):
    review._alert_rejection(
        _proposal("team_bot_a"), _MockReview(result="rejected",
                                      rejection_reason="touches auth-profiles.json"),
        "prop_abc1234", env["shared_dir"], env["config"],
    )
    call = env["captured"][0]
    assert call["source"] == "review"
    assert call["catalog_event"] == "decisions.proposal_rejected"
    assert call["dedup_key"] == "review/rejected/prop_abc1234"
    assert call["severity"] == env["Severity"].INFO
    # Phase F5: payload-driven; catalog renders "📋 Proposal rejected /
    # {proposal_id} / Reason: {reason}".
    assert call["message"] is None
    assert call["payload"] == {
        "proposal_id": "prop_abc1234",
        "reason": "touches auth-profiles.json",
    }


def test_alert_reviewed_routes_with_proposal_ready(env):
    review._alert_reviewed(
        _proposal("admin_bot"), _MockReview(result="approved"),
        "prop_def5678", env["shared_dir"], env["config"],
    )
    call = env["captured"][0]
    assert call["source"] == "review"
    # Both analyze and review can fire this event — operator mutes either
    # independently via Subscriptions UI if they're getting too many.
    assert call["catalog_event"] == "decisions.proposal_ready"
    assert call["dedup_key"] == "review/reviewed/prop_def5678"
    assert call["severity"] == env["Severity"].INFO
    # Phase F5: payload-driven; catalog renders the proposals-ready
    # template with count=1.
    assert call["message"] is None
    assert call["payload"]["count"] == 1
    assert call["payload"]["s"] == ""
    assert "admin_bot" in call["payload"]["titles"]
    assert "cleared" in call["payload"]["titles"].lower()


def test_alert_reviewed_gates_non_inbox_proposal(env):
    """A reviewed proposal that won't land in the actionable Inbox must
    NOT fire decisions.proposal_ready — the breadcrumb is "Recommendations → Inbox"
    and the push would point at an empty inbox. Covers both husks (surface null)
    and informational investigations (surface=improvement but informational)."""
    husk = {"target_bot": "admin_bot", "type": "investigation",
            "problem": "high maintenance ratio"}  # surface omitted → null
    review._alert_reviewed(
        husk, _MockReview(result="approved"),
        "prop_husk", env["shared_dir"], env["config"],
    )
    investigation = {"target_bot": "team_bot_a", "type": "investigation",
                     "problem": "investigate billing",
                     "surface": "improvement", "informational": True}
    review._alert_reviewed(
        investigation, _MockReview(result="approved"),
        "prop_inv", env["shared_dir"], env["config"],
    )
    assert env["captured"] == []


def test_alert_reviewed_flagged_uses_flag_icon(env):
    review._alert_reviewed(
        _proposal("team_bot_b"),
        _MockReview(result="flagged", risk_level="medium", risk_score=5,
                    flags=["touches plugin manifest", "writes to system path"]),
        "prop_xyz", env["shared_dir"], env["config"],
    )
    # Phase F5: flag status + flags list go into the payload's titles
    # field so the catalog body_template surfaces them.
    titles = env["captured"][0]["payload"]["titles"]
    assert "⚑" in titles
    assert "flagged" in titles.lower()
    # Flags surfaced in titles
    assert "touches plugin manifest" in titles


def test_alert_quarantine_routes_with_security_quarantined(env):
    """Phase F2: quarantine source passes structured payload; catalog
    body_template renders the body. Drops the source-side "Possible
    Tampering / unauthorized injection" prose — the catalog's
    is_safety_critical flag + UI confirmation modal carry urgency."""
    review._alert_quarantine(
        "prop_evil1337", "invalid_evolve_sig",
        env["shared_dir"], env["config"],
    )
    call = env["captured"][0]
    assert call["source"] == "review"
    assert call["catalog_event"] == "security.proposal_quarantined"
    assert call["dedup_key"] == "review/quarantine/prop_evil1337"
    assert call["severity"] == env["Severity"].CRITICAL
    assert call["message"] is None
    assert call["payload"] == {
        "proposal_id": "prop_evil1337",
        "reason": "invalid_evolve_sig",
    }


def test_distinct_proposals_get_distinct_dedup_keys(env):
    """Two rejected proposals must both reach the operator, not collapse
    under one cooldown — pinned via per-proposal dedup_keys."""
    review._alert_rejection(
        _proposal(), _MockReview(result="rejected", rejection_reason="r1"),
        "prop_a", env["shared_dir"], env["config"],
    )
    review._alert_rejection(
        _proposal(), _MockReview(result="rejected", rejection_reason="r2"),
        "prop_b", env["shared_dir"], env["config"],
    )
    keys = [c["dedup_key"] for c in env["captured"]]
    assert keys == ["review/rejected/prop_a", "review/rejected/prop_b"]


def test_dispatcher_recognizes_review_source():
    """review must be in dispatcher.known_sources so alerts.review.enabled
    is a real Config-page toggle. Drift here = "unknown source" in the
    audit log when review tries to push."""
    from evolve_admin.alerts.dispatcher import known_sources
    assert "review" in set(known_sources())
