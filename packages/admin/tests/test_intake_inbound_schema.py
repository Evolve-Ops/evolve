"""Tests for the inbound + TriageRecord schema additions (Phase 4a).

Schema changes on Intake:
  - inbound: bool (default False)
  - triage: TriageRecord | None (default None)

TriageRecord defends against malformed payloads in from_dict so a
hand-edited or older-snapshot intake never crashes the loader.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.intake.envelope import Intake, TriageRecord  # noqa: E402


# ─── TriageRecord ─────────────────────────────────────────────────────────


def test_triage_record_defaults_to_unknown():
    """Default-constructed TriageRecord has 'unknown' on every literal
    field — safe baseline for Phase 5 auto-action gates."""
    t = TriageRecord()
    assert t.category == "unknown"
    assert t.merit == "unknown"
    assert t.urgency == "unknown"
    assert t.recommendation == "unknown"
    assert t.estimated_effort == "unknown"
    assert t.confidence == 0.0


def test_triage_record_to_dict_round_trip():
    t = TriageRecord(
        category="bug", merit="real", urgency="p1",
        duplicate_of=["o/r#1"], recommendation="route_to_admin",
        draft_reply="Thanks for the report.", draft_labels=["bug", "p1"],
        estimated_effort="medium", confidence=0.85,
        reasoning="reproducer included",
        inbound_author="someone", inbound_title="X broken",
        inbound_body_short="details",
    )
    d = t.to_dict()
    restored = TriageRecord.from_dict(d)
    assert restored is not None
    assert restored.category == "bug"
    assert restored.urgency == "p1"
    assert restored.duplicate_of == ["o/r#1"]
    assert restored.draft_labels == ["bug", "p1"]
    assert restored.confidence == 0.85
    assert restored.inbound_author == "someone"


def test_triage_record_from_dict_none_returns_none():
    assert TriageRecord.from_dict(None) is None
    assert TriageRecord.from_dict("not a dict") is None  # type: ignore[arg-type]


def test_triage_record_from_dict_unknown_category_collapses():
    """Defensive coercion at the schema level too — schema and
    classifier layers both refuse to make up a category."""
    r = TriageRecord.from_dict({"category": "totally_fake"})
    assert r is not None
    assert r.category == "unknown"


def test_triage_record_from_dict_clamps_confidence():
    r = TriageRecord.from_dict({"confidence": 5.0})
    assert r is not None
    assert r.confidence == 1.0
    r2 = TriageRecord.from_dict({"confidence": -1.0})
    assert r2 is not None
    assert r2.confidence == 0.0


def test_triage_record_from_dict_drops_non_string_duplicates():
    r = TriageRecord.from_dict({
        "duplicate_of": ["good/repo#1", 42, None, "another/repo#2"],
    })
    assert r is not None
    assert r.duplicate_of == ["good/repo#1", "another/repo#2"]


# ─── Intake inbound flag ──────────────────────────────────────────────────


def test_intake_inbound_defaults_to_false():
    """Existing intakes (operator-filed) remain inbound=False."""
    ix = Intake(id="i-1", kind="bug", body="x")
    assert ix.inbound is False
    assert ix.triage is None


def test_intake_inbound_round_trip():
    ix = Intake(id="i-1", kind="bug", body="x", inbound=True)
    ix.triage = TriageRecord(
        category="bug", urgency="p2", confidence=0.7,
        inbound_author="someone",
    )
    restored = Intake.from_dict(ix.to_dict())
    assert restored.inbound is True
    assert restored.triage is not None
    assert restored.triage.category == "bug"
    assert restored.triage.urgency == "p2"
    assert restored.triage.inbound_author == "someone"


def test_intake_from_dict_handles_missing_inbound_fields():
    """Old intakes from before Phase 4 won't have inbound/triage keys;
    from_dict must default them, not raise."""
    ix = Intake(id="i-1", kind="bug", body="x")
    raw = ix.to_dict()
    del raw["inbound"]
    del raw["triage"]
    restored = Intake.from_dict(raw)
    assert restored.inbound is False
    assert restored.triage is None


def test_intake_from_dict_handles_malformed_triage():
    """A malformed triage block (string instead of dict, missing
    keys, etc.) should silently degrade to triage=None rather than
    crashing the loader."""
    ix = Intake(id="i-1", kind="bug", body="x", inbound=True)
    raw = ix.to_dict()
    raw["triage"] = "not a dict"
    restored = Intake.from_dict(raw)
    assert restored.triage is None
    # The intake itself still loads; inbound flag preserved.
    assert restored.inbound is True


def test_inbound_intake_can_start_in_filed_state():
    """Inbound intakes are pre-promoted: created in state=filed with
    the source-issue URL stuffed onto the promotion record. Make sure
    that combination is valid."""
    ix = Intake(
        id="i-1", kind="bug", body="x",
        state="filed", inbound=True,
    )
    ix.promotion.github_issue_url = "https://github.com/o/r/issues/42"
    ix.promotion.github_issue_number = 42
    restored = Intake.from_dict(ix.to_dict())
    assert restored.state == "filed"
    assert restored.inbound is True
    assert restored.promotion.github_issue_url == "https://github.com/o/r/issues/42"


def test_inbound_intake_kind_must_still_be_valid():
    """inbound=True doesn't relax kind validation — invalid kinds
    still raise on construction."""
    import pytest
    with pytest.raises(ValueError, match="kind"):
        Intake(id="i-1", kind="not-a-kind",  # type: ignore[arg-type]
               body="x", inbound=True)
