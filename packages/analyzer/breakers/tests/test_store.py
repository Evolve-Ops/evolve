"""Tests for breakers.store — state store + audit log."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from breakers import store
from breakers.store import (
    BREAKER_TYPES,
    POD_SCOPE,
    BreakerRecord,
    audit_log_path,
    breaker_file_path,
    breakers_dir,
    extend,
    is_expired,
    list_active,
    list_all,
    parse_duration,
    read_audit_log,
    read_trip,
    reset,
    trip,
    update_audit_fields,
)


# Reference "now" used by most tests.
FIXED_NOW = datetime(2026, 5, 21, 16, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers + validation
# ─────────────────────────────────────────────────────────────────────────────


class TestPathHelpers:
    def test_breakers_dir_creates_log_subdir(self, tmp_path: Path) -> None:
        root = breakers_dir(tmp_path)
        assert root == tmp_path / "breakers"
        assert root.is_dir()
        assert (root / "log").is_dir()

    def test_breaker_file_path_layout(self, tmp_path: Path) -> None:
        p = breaker_file_path(tmp_path, "team_bot_a", "cost")
        assert p == tmp_path / "breakers" / "team_bot_a" / "cost.json"

    def test_pod_scope_layout(self, tmp_path: Path) -> None:
        p = breaker_file_path(tmp_path, POD_SCOPE, "full")
        assert p == tmp_path / "breakers" / "pod" / "full.json"

    @pytest.mark.parametrize("bad", ["..", "/team_bot_a", "team_bot_a/cost", "team_bot_a ", ".",
                                     "team_bot_a\nname", "", "team_bot_a;rm"])
    def test_invalid_scope_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            breaker_file_path(tmp_path, bad, "cost")

    @pytest.mark.parametrize("bad", ["unknown", "security", "", "nuclear"])
    def test_invalid_type_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            breaker_file_path(tmp_path, "team_bot_a", bad)

    def test_audit_log_path_date_stamped(self, tmp_path: Path) -> None:
        p = audit_log_path(tmp_path, when=FIXED_NOW)
        assert p.name == "2026-05-21.jsonl"


# ─────────────────────────────────────────────────────────────────────────────
# trip / read_trip / reset round-trip
# ─────────────────────────────────────────────────────────────────────────────


class TestTripReadReset:
    def test_trip_writes_file_with_correct_fields(self, tmp_path: Path) -> None:
        rec = trip(
            shared_dir=tmp_path,
            scope="team_bot_a",
            breaker_type="cost",
            duration=timedelta(hours=24),
            initiated_by="admin:pod_admin",
            reason="testing",
            now=FIXED_NOW,
        )
        assert rec.bot_id == "team_bot_a"
        assert rec.type == "cost"
        assert rec.state == "tripped"
        assert rec.tripped_at == FIXED_NOW.isoformat()
        assert rec.expires_at == (FIXED_NOW + timedelta(hours=24)).isoformat()
        assert rec.initiated_by == "admin:pod_admin"
        assert rec.reason == "testing"
        assert rec.trip_id  # non-empty uuid

        path = breaker_file_path(tmp_path, "team_bot_a", "cost")
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["trip_id"] == rec.trip_id

    def test_indefinite_trip_has_none_expires_at(self, tmp_path: Path) -> None:
        rec = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="full",
            duration=None, initiated_by="admin:pod_admin",
            reason="indefinite", now=FIXED_NOW,
        )
        assert rec.expires_at is None

    def test_read_trip_returns_record(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="rate spike", now=FIXED_NOW,
        )
        got = read_trip(tmp_path, "team_bot_a", "cost")
        assert got is not None
        assert got.bot_id == "team_bot_a"
        assert got.type == "cost"

    def test_read_trip_returns_none_when_no_file(self, tmp_path: Path) -> None:
        assert read_trip(tmp_path, "team_bot_a", "cost") is None

    def test_read_trip_returns_none_on_corrupt_file(self, tmp_path: Path) -> None:
        """Fail-open: a truncated JSON file MUST NOT lock out the bot."""
        path = breaker_file_path(tmp_path, "team_bot_a", "cost")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
        assert read_trip(tmp_path, "team_bot_a", "cost") is None

    def test_read_trip_returns_none_on_unexpected_shape(self, tmp_path: Path) -> None:
        path = breaker_file_path(tmp_path, "team_bot_a", "cost")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["not", "a", "dict"]', encoding="utf-8")
        assert read_trip(tmp_path, "team_bot_a", "cost") is None

    def test_reset_deletes_file(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=24), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        path = breaker_file_path(tmp_path, "team_bot_a", "cost")
        assert path.is_file()

        prior = reset(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            initiated_by="admin:pod_admin",
        )
        assert prior is not None
        assert not path.is_file()

    def test_reset_is_idempotent_when_nothing_tripped(self, tmp_path: Path) -> None:
        prior = reset(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            initiated_by="admin:pod_admin",
        )
        assert prior is None
        # And no audit row was written:
        audit = read_audit_log(tmp_path)
        assert audit == []

    def test_retrip_overwrites_with_new_trip_id(self, tmp_path: Path) -> None:
        first = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="first", now=FIXED_NOW,
        )
        second = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="second", now=FIXED_NOW + timedelta(minutes=10),
        )
        assert first.trip_id != second.trip_id
        assert read_trip(tmp_path, "team_bot_a", "cost").trip_id == second.trip_id

        # Audit log records both events.
        audit = read_audit_log(tmp_path)
        actions = [r["action"] for r in audit]
        # newest-first ordering — retrip should come before the initial trip.
        assert actions[0] == "retrip"
        assert actions[1] == "trip"


# ─────────────────────────────────────────────────────────────────────────────
# Expiry semantics
# ─────────────────────────────────────────────────────────────────────────────


class TestExpiry:
    def test_is_expired_false_for_future(self, tmp_path: Path) -> None:
        rec = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        assert not is_expired(rec, now=FIXED_NOW)
        assert not is_expired(rec, now=FIXED_NOW + timedelta(minutes=30))

    def test_is_expired_true_after_expiry(self, tmp_path: Path) -> None:
        rec = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        assert is_expired(rec, now=FIXED_NOW + timedelta(hours=2))

    def test_indefinite_never_expires(self, tmp_path: Path) -> None:
        rec = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=None, initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        assert not is_expired(rec, now=FIXED_NOW + timedelta(days=365))

    def test_read_trip_does_not_filter_expired(self, tmp_path: Path) -> None:
        """File existence semantics: raw read returns even expired records."""
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(seconds=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        # Read "after expiry" — record still on disk.
        rec = read_trip(tmp_path, "team_bot_a", "cost")
        assert rec is not None
        assert is_expired(rec, now=FIXED_NOW + timedelta(hours=1))


# ─────────────────────────────────────────────────────────────────────────────
# list_active / list_all
# ─────────────────────────────────────────────────────────────────────────────


class TestListing:
    def _trip(self, tmp_path: Path, scope: str, breaker_type: str,
              *, duration: timedelta | None = timedelta(hours=24)) -> None:
        trip(
            shared_dir=tmp_path, scope=scope, breaker_type=breaker_type,
            duration=duration, initiated_by="auto",
            reason="test", now=FIXED_NOW,
        )

    def test_list_active_returns_only_unexpired(self, tmp_path: Path) -> None:
        self._trip(tmp_path, "team_bot_a", "cost", duration=timedelta(seconds=10))
        self._trip(tmp_path, "security_bot", "cost", duration=timedelta(hours=24))
        self._trip(tmp_path, POD_SCOPE, "full", duration=None)

        active = list_active(
            tmp_path, now=FIXED_NOW + timedelta(hours=1),
        )
        scopes = sorted(r.bot_id for r in active)
        # team_bot_a expired; security_bot + pod remain.
        assert scopes == [POD_SCOPE, "security_bot"]

    def test_list_all_includes_expired(self, tmp_path: Path) -> None:
        self._trip(tmp_path, "team_bot_a", "cost", duration=timedelta(seconds=10))
        all_recs = list_all(tmp_path)
        assert len(all_recs) == 1

    def test_list_active_empty_when_no_trips(self, tmp_path: Path) -> None:
        assert list_active(tmp_path) == []
        assert list_all(tmp_path) == []

    def test_list_includes_pod_scope(self, tmp_path: Path) -> None:
        self._trip(tmp_path, POD_SCOPE, "full")
        active = list_active(tmp_path, now=FIXED_NOW)
        assert len(active) == 1
        assert active[0].bot_id == POD_SCOPE
        assert active[0].type == "full"


# ─────────────────────────────────────────────────────────────────────────────
# Extend
# ─────────────────────────────────────────────────────────────────────────────


class TestExtend:
    def test_extend_pushes_expiry_forward(self, tmp_path: Path) -> None:
        first = trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        result = extend(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            additional=timedelta(hours=2), initiated_by="admin:pod_admin",
            now=FIXED_NOW + timedelta(minutes=10),
        )
        assert result is not None
        original_expiry = datetime.fromisoformat(first.expires_at)
        new_expiry = datetime.fromisoformat(result.expires_at)
        assert new_expiry == original_expiry + timedelta(hours=2)
        # trip_id preserved (extend doesn't rotate identity).
        assert result.trip_id == first.trip_id

    def test_extend_returns_none_when_nothing_tripped(self, tmp_path: Path) -> None:
        result = extend(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            additional=timedelta(hours=1), initiated_by="admin:pod_admin",
        )
        assert result is None

    def test_extend_does_not_touch_indefinite(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=None, initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        result = extend(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            additional=timedelta(hours=1), initiated_by="admin:pod_admin",
        )
        assert result is not None
        assert result.expires_at is None  # still indefinite

    def test_extend_after_expiry_pushes_from_now(self, tmp_path: Path) -> None:
        """If the trip expired at T and we extend at T+1h by 2h,
        the new expiry is T+1h+2h, not T+2h."""
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        extend_at = FIXED_NOW + timedelta(hours=2)
        result = extend(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            additional=timedelta(hours=2), initiated_by="admin:pod_admin",
            now=extend_at,
        )
        assert result is not None
        new_expiry = datetime.fromisoformat(result.expires_at)
        assert new_expiry == extend_at + timedelta(hours=2)


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditLog:
    def test_trip_writes_audit_entry(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=24), initiated_by="admin:pod_admin",
            reason="testing", now=FIXED_NOW,
        )
        entries = read_audit_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["action"] == "trip"
        assert entries[0]["scope"] == "team_bot_a"
        assert entries[0]["type"] == "cost"
        assert entries[0]["initiated_by"] == "admin:pod_admin"
        assert entries[0]["reason"] == "testing"
        assert entries[0]["duration_seconds"] == 24 * 3600

    def test_reset_writes_audit_entry(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=24), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        reset(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            initiated_by="admin:pod_admin", reason="false alarm",
        )
        entries = read_audit_log(tmp_path)
        actions = [r["action"] for r in entries]
        # Newest-first: reset comes before trip.
        assert actions == ["reset", "trip"]
        assert entries[0]["reason"] == "false alarm"

    def test_audit_log_per_day(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="day-1", now=FIXED_NOW,
        )
        trip(
            shared_dir=tmp_path, scope="security_bot", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="day-2", now=FIXED_NOW + timedelta(days=1),
        )
        # Two files, one per day.
        log_dir = tmp_path / "breakers" / "log"
        files = sorted(f.name for f in log_dir.iterdir())
        assert files == ["2026-05-21.jsonl", "2026-05-22.jsonl"]


# ─────────────────────────────────────────────────────────────────────────────
# Atomic writes — sanity that no .tmp files leak
# ─────────────────────────────────────────────────────────────────────────────


class TestAtomicWrites:
    def test_no_tmp_files_after_trip(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        bot_dir = tmp_path / "breakers" / "team_bot_a"
        tmp_files = [f for f in bot_dir.iterdir() if f.name.startswith(".")]
        assert tmp_files == [], f"tempfile leak: {tmp_files}"

    def test_failed_write_does_not_leave_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If os.replace raises, the temp file must be cleaned up."""
        # Set up a partial state.
        breakers_dir(tmp_path)

        real_replace = os.replace
        call_count = {"n": 0}

        def failing_replace(src: str, dst: str) -> None:
            call_count["n"] += 1
            raise OSError("simulated failure")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError):
            trip(
                shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
                duration=timedelta(hours=1), initiated_by="auto",
                reason="x", now=FIXED_NOW,
            )
        # Restore replace so the test fixture cleanup doesn't break.
        monkeypatch.setattr(os, "replace", real_replace)

        bot_dir = tmp_path / "breakers" / "team_bot_a"
        if bot_dir.exists():
            tmp_files = [f for f in bot_dir.iterdir() if f.name.startswith(".")]
            assert tmp_files == [], f"tempfile leak after failed write: {tmp_files}"


# ─────────────────────────────────────────────────────────────────────────────
# update_audit_fields
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdateAuditFields:
    def test_populates_audit_summary(self, tmp_path: Path) -> None:
        trip(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            duration=timedelta(hours=1), initiated_by="auto",
            reason="x", now=FIXED_NOW,
        )
        updated = update_audit_fields(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            audit_summary="130 heartbeat turns on sonnet",
            audit_recommendation="override heartbeat.model = haiku-4-5",
        )
        assert updated is not None
        assert updated.audit_summary == "130 heartbeat turns on sonnet"
        # Other fields preserved.
        assert updated.reason == "x"
        # Read-back consistent.
        again = read_trip(tmp_path, "team_bot_a", "cost")
        assert again.audit_summary == "130 heartbeat turns on sonnet"

    def test_returns_none_when_no_trip(self, tmp_path: Path) -> None:
        result = update_audit_fields(
            shared_dir=tmp_path, scope="team_bot_a", breaker_type="cost",
            audit_summary="anything",
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# parse_duration
# ─────────────────────────────────────────────────────────────────────────────


class TestParseDuration:
    @pytest.mark.parametrize("s,expected", [
        ("1h", timedelta(hours=1)),
        ("4h", timedelta(hours=4)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("30m", timedelta(minutes=30)),
        ("0h", timedelta(0)),
    ])
    def test_parses_valid(self, s: str, expected: timedelta) -> None:
        assert parse_duration(s) == expected

    @pytest.mark.parametrize("s", ["indefinite", "indef", "none", "INDEFINITE"])
    def test_indefinite_returns_none(self, s: str) -> None:
        assert parse_duration(s) is None

    @pytest.mark.parametrize("s", ["", "h", "1x", "-1h", "abc", "1.5h"])
    def test_invalid_raises(self, s: str) -> None:
        with pytest.raises(ValueError):
            parse_duration(s)
