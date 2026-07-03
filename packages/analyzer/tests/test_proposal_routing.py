"""proposal_routing — the canonical "where does a proposal surface" predicate.

Pins the contract the notification emitters (analyze.send_telegram_alert,
review._alert_reviewed) and the Layer 2 legacy-investigation producer rely on:

  * is_recommendable                  → surface == 'improvement'
  * proposal_surfaces_in_pending_inbox→ recommendable AND not informational
  * apply_legacy_investigation_routing→ type=='investigation' gets
                                        surface='improvement' + informational

These mirror the client-side routing in self-improvement.js.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from proposal_routing import (  # noqa: E402
    apply_legacy_investigation_routing,
    is_recommendable,
    proposal_surfaces_in_pending_inbox,
)


# ── is_recommendable ────────────────────────────────────────────────────────


def test_is_recommendable_true_for_improvement():
    assert is_recommendable({"surface": "improvement"}) is True


def test_is_recommendable_false_for_null_surface():
    # The exact husk shape from the bug: surface read as null on disk.
    assert is_recommendable({"surface": None}) is False
    assert is_recommendable({}) is False


def test_is_recommendable_false_for_alerts_surfaces():
    for s in ("firing", "drift", "cleanup"):
        assert is_recommendable({"surface": s}) is False, s


# ── proposal_surfaces_in_pending_inbox ──────────────────────────────────────


def test_pending_inbox_true_for_improvement_non_informational():
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": False}
    ) is True
    # informational key absent → treated as non-informational.
    assert proposal_surfaces_in_pending_inbox({"surface": "improvement"}) is True


def test_pending_inbox_false_for_null_surface():
    assert proposal_surfaces_in_pending_inbox({"surface": None}) is False


def test_pending_inbox_false_for_improvement_informational():
    # Recommendable but informational → Observations sub-block, NOT Pending.
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True}
    ) is False


# ── altitude carve-out (capability ideas) ───────────────────────────────────


def test_pending_inbox_high_altitude_informational_surfaces():
    # A capability idea: surface=improvement, altitude>=2, but informational
    # (Investigation kind). The carve-out lands it in Pending anyway.
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True, "altitude": 2}
    ) is True
    # Above the floor too.
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True, "altitude": 3}
    ) is True


def test_pending_inbox_low_altitude_informational_stays_observations():
    # A generic Investigation at the default L0: still Observations, not Pending.
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True, "altitude": 0}
    ) is False
    # altitude 1 is below the >=2 floor → still Observations.
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True, "altitude": 1}
    ) is False


def test_pending_inbox_high_altitude_requires_improvement_surface():
    # The carve-out only applies on the improvement surface — a high-altitude
    # non-improvement proposal still routes off the Recommendations page.
    for s in (None, "firing", "drift", "cleanup"):
        assert proposal_surfaces_in_pending_inbox(
            {"surface": s, "informational": True, "altitude": 2}
        ) is False, s


def test_pending_inbox_missing_altitude_defaults_safely():
    # No altitude key + informational → defaults to 0, stays in Observations
    # (i.e. the carve-out never fires on a malformed/legacy proposal).
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True}
    ) is False
    # None altitude is coerced to 0, not a crash.
    assert proposal_surfaces_in_pending_inbox(
        {"surface": "improvement", "informational": True, "altitude": None}
    ) is False


# ── apply_legacy_investigation_routing ──────────────────────────────────────


def test_investigation_gets_improvement_and_informational():
    p = {"type": "investigation", "problem": "look into X"}
    apply_legacy_investigation_routing(p)
    assert p["surface"] == "improvement"
    assert p["informational"] is True
    # And the combined predicate keeps it OUT of the Pending inbox (it belongs
    # in Observations), so it won't drive a "ready → Pending" push.
    assert proposal_surfaces_in_pending_inbox(p) is False
    assert is_recommendable(p) is True


def test_non_investigation_left_untouched():
    for t in ("config_change", "workflow_change", "script_change"):
        p = {"type": t}
        apply_legacy_investigation_routing(p)
        assert "surface" not in p, t
        assert "informational" not in p, t


def test_apply_returns_same_dict_for_chaining():
    p = {"type": "investigation"}
    assert apply_legacy_investigation_routing(p) is p


# ── Producer wiring: analyze.write_proposal stamps the disk file ─────────────


def test_write_proposal_stamps_investigation_routing_on_disk(tmp_path):
    """End-to-end: the legacy producer writes surface/informational into the
    on-disk proposal for a type=='investigation' item, so it routes to
    Observations rather than leaking to Alerts as a husk."""
    import json

    import analyze  # noqa: PLC0415 — module-on-path import

    proposal_id = analyze.write_proposal(
        {
            "pattern_key": "bot-a/high_maintenance_ratio",
            "target_bot": "bot-a",
            "type": "investigation",
            "problem": "Maintenance ratio averaged 36% over the last 7 days",
        },
        str(tmp_path),
    )
    assert proposal_id
    written = json.loads(
        (tmp_path / "proposals" / "pending" / f"{proposal_id}.json").read_text()
    )
    assert written["surface"] == "improvement"
    assert written["informational"] is True
    # Still a legacy alert-category proposal; routing is additive.
    assert written["category"] == "alert"


def test_write_proposal_leaves_non_investigation_unstamped(tmp_path):
    import json

    import analyze  # noqa: PLC0415

    proposal_id = analyze.write_proposal(
        {
            "pattern_key": "bot-a/unexpected_billing_mode",
            "target_bot": "bot-a",
            "type": "config_change",
            "problem": "Unexpected billing mode detected",
        },
        str(tmp_path),
    )
    assert proposal_id
    written = json.loads(
        (tmp_path / "proposals" / "pending" / f"{proposal_id}.json").read_text()
    )
    assert "surface" not in written
    assert "informational" not in written
