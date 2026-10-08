"""Tests for the intake activity-log schema + store helpers (Phase 1).

Covers:
  - ActivityEvent dataclass: to_dict/from_dict round trip, defensive
    parsing of malformed entries.
  - Intake schema: activity_log + last_seen_activity_at round-trip
    through to_dict/from_dict.
  - unread_activity_count() semantics with various cursor positions.
  - store.find_by_github_issue: matches by URL substring + number,
    rejects on missing fields, ignores non-filed states.
  - store.append_activity: writes back atomically + bumps updated_at.
  - store.mark_activity_seen: moves the cursor + persists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.intake import store  # noqa: E402
from evolve_admin.intake.envelope import (  # noqa: E402
    ActivityEvent,
    Intake,
)


# ─── Helpers ───────────────────────────────────────────────────────────────


def _make_filed_intake(
    *, id_: str = "intake-test-1",
    repo: str = "openclaw/openclaw", number: int = 84820,
) -> Intake:
    """Build a filed intake with the promotion fields populated."""
    ix = Intake(id=id_, kind="bug", body="Gateway crashes every 2h")
    ix.state = "filed"
    ix.promotion.github_issue_url = f"https://github.com/{repo}/issues/{number}"
    ix.promotion.github_issue_number = number
    return ix


# ─── ActivityEvent ─────────────────────────────────────────────────────────


def test_activity_event_round_trip():
    e = ActivityEvent(
        kind="new_comment", actor="oc-maintainer",
        observed_at="2026-05-22T19:00:00Z",
        snippet="Could you share more?", ref="c12345",
    )
    d = e.to_dict()
    restored = ActivityEvent.from_dict(d)
    assert restored == e


def test_activity_event_from_dict_unknown_kind_returns_none():
    """Unknown kinds must round-trip to None — the from_dict caller
    can filter them out so a malformed file doesn't pollute state."""
    assert ActivityEvent.from_dict({"kind": "totally_fake"}) is None
    assert ActivityEvent.from_dict({}) is None
    assert ActivityEvent.from_dict(None) is None
    assert ActivityEvent.from_dict("not a dict") is None  # type: ignore[arg-type]


def test_activity_event_from_dict_missing_fields_use_defaults():
    """A well-formed but minimal entry should parse with safe defaults."""
    e = ActivityEvent.from_dict({"kind": "new_comment"})
    assert e is not None
    assert e.kind == "new_comment"
    assert e.actor == ""
    assert e.snippet == ""
    assert e.ref == ""


# ─── Intake schema with activity ───────────────────────────────────────────


def test_intake_schema_activity_round_trip():
    ix = _make_filed_intake()
    ix.activity_log = [
        ActivityEvent(kind="new_comment", actor="x", observed_at="2026-05-22T10:00:00Z"),
        ActivityEvent(kind="closed", actor="y", observed_at="2026-05-22T11:00:00Z"),
    ]
    ix.last_seen_activity_at = "2026-05-22T09:00:00Z"

    restored = Intake.from_dict(ix.to_dict())
    assert restored.activity_log == ix.activity_log
    assert restored.last_seen_activity_at == ix.last_seen_activity_at


def test_intake_from_dict_skips_malformed_activity_entries():
    """A corrupt entry in activity_log shouldn't break the whole intake."""
    raw = _make_filed_intake().to_dict()
    raw["activity_log"] = [
        {"kind": "new_comment", "actor": "good"},
        {"kind": "totally_fake"},  # bad — should be dropped
        "not even a dict",
        {"kind": "closed", "actor": "also-good"},
    ]
    restored = Intake.from_dict(raw)
    assert len(restored.activity_log) == 2
    assert restored.activity_log[0].actor == "good"
    assert restored.activity_log[1].actor == "also-good"


def test_intake_from_dict_handles_missing_activity_fields():
    """Existing intakes from before Phase 1 won't have activity_log /
    last_seen_activity_at — from_dict must default them, not raise."""
    raw = _make_filed_intake().to_dict()
    del raw["activity_log"]
    del raw["last_seen_activity_at"]
    restored = Intake.from_dict(raw)
    assert restored.activity_log == []
    assert restored.last_seen_activity_at == ""


# ─── unread_activity_count ────────────────────────────────────────────────


def test_unread_count_empty_log():
    ix = _make_filed_intake()
    assert ix.unread_activity_count() == 0


def test_unread_count_no_cursor_all_unread():
    """When the cursor isn't set, every activity event counts as unread
    — first time the operator opens the Inbox they see the full history."""
    ix = _make_filed_intake()
    ix.activity_log = [
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T10:00:00Z"),
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T11:00:00Z"),
    ]
    assert ix.unread_activity_count() == 2


def test_unread_count_cursor_partitions_log():
    ix = _make_filed_intake()
    ix.activity_log = [
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T10:00:00Z"),
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T11:00:00Z"),
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T12:00:00Z"),
    ]
    ix.last_seen_activity_at = "2026-05-22T10:30:00Z"
    assert ix.unread_activity_count() == 2  # the 11:00 and 12:00 events


def test_unread_count_cursor_in_future_returns_zero():
    """Operator marking everything seen at a wall-clock future time
    should clear the badge entirely."""
    ix = _make_filed_intake()
    ix.activity_log = [
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T10:00:00Z"),
    ]
    ix.last_seen_activity_at = "2099-01-01T00:00:00Z"
    assert ix.unread_activity_count() == 0


# ─── store.find_by_github_issue ───────────────────────────────────────────


def test_find_by_github_issue_locates_matching_intake(tmp_path):
    ix = _make_filed_intake(id_="intake-find-test")
    store.write_intake(ix, tmp_path)
    found = store.find_by_github_issue(tmp_path, "openclaw/openclaw", 84820)
    assert found is not None
    assert found.id == "intake-find-test"


def test_find_by_github_issue_returns_none_for_unknown(tmp_path):
    """No matching intake → None, no exception."""
    store.write_intake(_make_filed_intake(), tmp_path)
    assert store.find_by_github_issue(tmp_path, "openclaw/openclaw", 99999) is None
    assert store.find_by_github_issue(tmp_path, "wrong/repo", 84820) is None


def test_find_by_github_issue_ignores_unfiled_intakes(tmp_path):
    """An intake captured but never filed has no GitHub link — it must
    not be returned even if its id happens to be close to a number."""
    ix = Intake(id="intake-open-1", kind="bug", body="hi")
    # Stays in 'open' — no promotion URL.
    store.write_intake(ix, tmp_path)
    assert store.find_by_github_issue(tmp_path, "openclaw/openclaw", 84820) is None


def test_find_by_github_issue_rejects_invalid_args(tmp_path):
    """Defensive: bad caller args should return None, never raise."""
    assert store.find_by_github_issue(tmp_path, "", 1) is None
    assert store.find_by_github_issue(tmp_path, "x/y", 0) is None
    assert store.find_by_github_issue(tmp_path, "x/y", -1) is None
    assert store.find_by_github_issue(tmp_path, "x/y", "not an int") is None  # type: ignore[arg-type]


def test_find_by_github_issue_disambiguates_same_number_different_repo(tmp_path):
    """Two intakes can share an issue number across different repos.
    The repo-substring check must disambiguate."""
    a = _make_filed_intake(id_="intake-a", repo="evolve-ops/evolve", number=42)
    b = _make_filed_intake(id_="intake-b", repo="openclaw/openclaw", number=42)
    store.write_intake(a, tmp_path)
    store.write_intake(b, tmp_path)
    assert store.find_by_github_issue(tmp_path, "evolve-ops/evolve", 42).id == "intake-a"
    assert store.find_by_github_issue(tmp_path, "openclaw/openclaw", 42).id == "intake-b"


# ─── store.append_activity ────────────────────────────────────────────────


def test_append_activity_writes_to_disk(tmp_path):
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)
    event = ActivityEvent(
        kind="new_comment", actor="oc-maintainer",
        snippet="reply", ref="c1",
    )
    store.append_activity(ix, event, tmp_path)

    # Re-read from disk and verify it landed.
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert len(reloaded.activity_log) == 1
    assert reloaded.activity_log[0].actor == "oc-maintainer"


def test_append_activity_bumps_updated_at(tmp_path):
    ix = _make_filed_intake()
    ix.updated_at = "2026-01-01T00:00:00Z"  # stale
    store.write_intake(ix, tmp_path)
    store.append_activity(ix, ActivityEvent(kind="new_comment"), tmp_path)
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert reloaded.updated_at != "2026-01-01T00:00:00Z"


def test_append_activity_preserves_log_order(tmp_path):
    """Activity events should land in insertion order — that's what the
    UI relies on to render conversation flow."""
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)
    for i in range(3):
        store.append_activity(
            ix, ActivityEvent(kind="new_comment", ref=f"c{i}"),
            tmp_path,
        )
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert [e.ref for e in reloaded.activity_log] == ["c0", "c1", "c2"]


# ─── store.mark_activity_seen ─────────────────────────────────────────────


def test_mark_activity_seen_clears_unread(tmp_path):
    ix = _make_filed_intake()
    ix.activity_log = [
        ActivityEvent(kind="new_comment", observed_at="2026-05-22T10:00:00Z"),
    ]
    store.write_intake(ix, tmp_path)
    assert ix.unread_activity_count() == 1

    store.mark_activity_seen(ix, tmp_path)
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert reloaded.unread_activity_count() == 0


def test_mark_activity_seen_with_explicit_cursor(tmp_path):
    """Explicit cursor argument should be used as-is — useful when the
    UI wants to mark seen up to a specific event, not 'now'."""
    ix = _make_filed_intake()
    store.write_intake(ix, tmp_path)
    store.mark_activity_seen(ix, tmp_path, cursor="2026-05-22T10:30:00Z")
    reloaded, _, _ = store.find_intake(tmp_path, ix.id)
    assert reloaded.last_seen_activity_at == "2026-05-22T10:30:00Z"
