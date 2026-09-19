"""Tests for board_actions.py (D-BI5: vocabulary, cost estimate, gating).

WHAT THESE PIN:
  * ``kind: tool`` actions are always $0 and never gated — pure code, no
    model, no integration.
  * A ``kind: llm`` action needing an integration is disabled with a reason
    when that integration is not granted, and enabled with a numeric
    ``est_cost`` when it is (and pricing is known).
  * "calling" is a pod capability gap, not a per-bot grant — always disabled.
  * The starter vocabulary never exceeds three actions and never gives an
    email-derived card a send/book/pay action (D-BI6).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import board_actions as ba  # noqa: E402


def test_default_actions_capped_at_three_for_every_cluster():
    for cluster in ("health", "fitness", "travel", "work", "social",
                     "hobbies", "family", "home", "admin", "unknown-cluster"):
        actions = ba.default_actions_for(cluster=cluster, source="manual")
        assert 1 <= len(actions) <= ba.MAX_ACTIONS_PER_CARD


def test_email_source_never_carries_a_send_book_or_pay_action():
    actions = ba.default_actions_for(cluster="admin", source="email")
    ids = {a["id"] for a in actions}
    assert ids == {"draft_reply", "summarise_thread", "extract_tasks"}
    assert all(a["kind"] == "llm" for a in actions)
    reply = next(a for a in actions if a["id"] == "draft_reply")
    assert reply.get("requires_confirm") is True


def test_calendar_source_overrides_cluster_vocabulary():
    actions = ba.default_actions_for(cluster="admin", source="calendar")
    assert {a["id"] for a in actions} == {
        "prepare_dossier", "find_a_time", "draft_decline"}


def test_tool_kind_is_always_free_and_never_disabled():
    action = {"id": "find_directions", "label": "Find directions",
              "kind": "tool", "_integration": None}
    ctx = {"google": False, "llm_est_cost": None}
    out = ba.annotate_action(action, ctx)
    assert out["est_cost"] == 0.0
    assert "disabled_reason" not in out
    assert out["requires_rung"] == ba.RUNG_NONE


def test_llm_action_needing_google_is_disabled_when_not_granted():
    action = {"id": "draft_reply", "label": "Draft a reply", "kind": "llm",
              "_integration": "google"}
    out = ba.annotate_action(action, {"google": False, "llm_est_cost": 0.01})
    assert out["est_cost"] is None
    assert "connect Google" in out["disabled_reason"]
    assert out["requires_rung"] == ba.RUNG_INTEGRATION


def test_llm_action_needing_google_is_enabled_and_priced_when_granted():
    action = {"id": "draft_reply", "label": "Draft a reply", "kind": "llm",
              "_integration": "google"}
    out = ba.annotate_action(action, {"google": True, "llm_est_cost": 0.0123})
    assert out["est_cost"] == 0.0123
    assert "disabled_reason" not in out


def test_llm_action_disabled_when_cost_estimate_unavailable():
    action = {"id": "draft_reminder", "label": "Draft a reminder",
              "kind": "llm", "_integration": None}
    out = ba.annotate_action(action, {"google": True, "llm_est_cost": None})
    assert out["est_cost"] is None
    assert "cost estimate unavailable" in out["disabled_reason"]


def test_calling_integration_is_always_disabled_a_pod_capability_gap():
    action = {"id": "call_ahead", "label": "Call ahead", "kind": "llm",
              "_integration": "calling"}
    out = ba.annotate_action(action, {"google": True, "llm_est_cost": 0.5})
    assert out["est_cost"] is None
    assert "no calling integration" in out["disabled_reason"]
    assert out["requires_rung"] == ba.RUNG_UNBUILT


def test_internal_bookkeeping_never_reaches_the_wire():
    action = {"id": "x", "label": "x", "kind": "tool", "_integration": None}
    out = ba.annotate_action(action, {"google": False, "llm_est_cost": None})
    assert not any(k.startswith("_") for k in out)


def test_find_action_by_id_and_miss():
    card = {"actions": [{"id": "a"}, {"id": "b"}]}
    assert ba.find_action(card, "b") == {"id": "b"}
    assert ba.find_action(card, "nope") is None
    assert ba.find_action({}, "a") is None


def test_bot_action_context_degrades_to_no_estimate_with_no_pricing(monkeypatch, tmp_path):
    monkeypatch.setattr(ba, "_fast_rung_cost_per_token", lambda *a, **k: None)
    monkeypatch.setattr(ba, "_google_granted", lambda *a, **k: False)
    ctx = ba.bot_action_context("bot-a", {}, tmp_path)
    assert ctx == {"google": False, "llm_est_cost": None}


def test_bot_action_context_computes_cost_from_rates(monkeypatch, tmp_path):
    monkeypatch.setattr(ba, "_fast_rung_cost_per_token",
                        lambda *a, **k: (0.000001, 0.000005))
    monkeypatch.setattr(ba, "_google_granted", lambda *a, **k: True)
    ctx = ba.bot_action_context("bot-a", {}, tmp_path)
    expected = round(ba._EST_INPUT_TOKENS * 0.000001 + ba._EST_OUTPUT_TOKENS * 0.000005, 4)
    assert ctx == {"google": True, "llm_est_cost": expected}
