"""Tests for evolve_admin.config_sandbox.overrides.

Phase 2 of internal/spec-openclaw-json-derived-artifact-2026-05-24.md.

The overrides module is the durable layer for per-bot Policy deviations.
These tests pin:

- Read returns an empty BotOverrides when the file is missing.
- Write creates the file with the right shape, atomically.
- Write of an unknown schema path is rejected (otherwise we'd recreate
  the PR #1525 bug class inside the overrides file).
- Write of a pod-wide key (per_bot=False) is rejected — Phase 2 is
  per-bot only.
- Write of a wrong-typed value is rejected before touching disk.
- Each write also lands in provenance.json (cross-cutting audit log).
- Re-write of the same key overwrites the existing entry.
- Delete removes the entry AND its provenance.
- mark_reviewed clears needs_review and re-attributes set_by.
- iter_all_overrides walks every bot file.
- Path traversal attempts in bot_id are rejected.
- Strict type checks: bool is not int, int is not float, etc.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evolve_admin.config_sandbox import (
    BotOverrides,
    OverrideEntry,
    OverrideStateError,
    OverrideValidationError,
    delete_override,
    iter_all_overrides,
    lookup,
    mark_reviewed,
    overrides_dir,
    path_for_bot,
    read_bot_overrides,
    write_override,
)


# Known per_bot schema paths used throughout the tests. Picking these to
# pin behavior: if any of these is removed from the schema we want the
# tests to fail loudly with a clear "fix the test" signal rather than
# silently testing nothing.
_TIER_KEY = "openclaw.plugins.evolve.tier"                              # enum
_SUMMARIZER_KEY = "openclaw.plugins.evolve.summarizerMinTurns"          # int
_CONFIDENCE_KEY = "openclaw.plugins.evolve.classifierKeywordConfidenceFloor"  # float
_COST_LEDGER_KEY = "openclaw.plugins.evolve.costLedgerEnabled"          # bool


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 24, 18, 0, 0, tzinfo=timezone.utc)


# ─── Read ─────────────────────────────────────────────────────────────────


def test_read_returns_empty_when_missing(shared_dir: Path):
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert isinstance(bo, BotOverrides)
    assert bo.bot_id == "security_bot"
    assert bo.overrides == {}


def test_read_returns_empty_when_malformed(shared_dir: Path):
    path = path_for_bot(shared_dir, "security_bot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not: json")
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.overrides == {}


def test_path_layout(shared_dir: Path):
    assert overrides_dir(shared_dir) == shared_dir / "sandbox" / "overrides"
    assert path_for_bot(shared_dir, "team_bot_a") == shared_dir / "sandbox" / "overrides" / "team_bot_a.json"


def test_invalid_bot_id_rejected(shared_dir: Path):
    """Stricter than a blocklist: positive regex match against the canonical
    bot-id shape. Anything else — empty, path traversal, leading dot, upper
    case, embedded slash — is rejected."""
    for bad in (
        "",
        "../etc/passwd",
        "foo/bar",
        ".hidden",          # leading dot would collide with .tmp prefix
        ".",                # ".." in "." is False but "." starts with "."
        "..",
        "Foo",              # canonical IDs are lowercase
        "team_bot_a name",         # whitespace
        "team_bot_a\x00.json",     # null byte
    ):
        with pytest.raises(OverrideValidationError, match="invalid bot_id"):
            path_for_bot(shared_dir, bad)


def test_valid_bot_ids_accepted(shared_dir):
    """Sanity: the canonical shape works for the bots in this codebase."""
    for good in ("team_bot_a", "security_bot", "team_bot_b", "evo", "personal_bot_user", "personal_bot", "bot-1", "bot_1"):
        # Should not raise.
        assert path_for_bot(shared_dir, good).name == f"{good}.json"


# ─── Write (happy path) ───────────────────────────────────────────────────


def test_write_creates_file_with_expected_shape(shared_dir, fixed_now):
    oe = write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", note="testing", now=fixed_now,
    )
    assert isinstance(oe, OverrideEntry)
    assert oe.value == "monitor"
    assert oe.set_by == "operator"
    assert oe.set_at == "2026-05-24T18:00:00Z"
    assert oe.note == "testing"
    assert oe.needs_review is False

    path = path_for_bot(shared_dir, "security_bot")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["bot_id"] == "security_bot"
    assert _TIER_KEY in data["overrides"]
    assert data["overrides"][_TIER_KEY]["value"] == "monitor"
    assert data["overrides"][_TIER_KEY]["set_at"] == "2026-05-24T18:00:00Z"


def test_write_file_is_world_readable(shared_dir):
    """File mode is 0644 (not the 0600 mkstemp default), since multiple
    user contexts read it."""
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    path = path_for_bot(shared_dir, "security_bot")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o644


def test_write_round_trip(shared_dir, fixed_now):
    write_override(
        shared_dir, "security_bot", _SUMMARIZER_KEY, 5,
        set_by="rsi:p123", note="proposal_p123", expires_at="2026-08-01",
        needs_review=False, now=fixed_now,
    )
    bo = read_bot_overrides(shared_dir, "security_bot")
    entry = bo.get(_SUMMARIZER_KEY)
    assert entry is not None
    assert entry.value == 5
    assert entry.set_by == "rsi:p123"
    assert entry.note == "proposal_p123"
    assert entry.expires_at == "2026-08-01"
    assert entry.needs_review is False


def test_write_rewrite_overwrites_previous_entry(shared_dir, fixed_now):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", note="first", now=fixed_now,
    )
    later = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "manage",
        set_by="operator", note="second", now=later,
    )
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert len(bo.overrides) == 1
    assert bo.overrides[_TIER_KEY].value == "manage"
    assert bo.overrides[_TIER_KEY].note == "second"
    assert bo.overrides[_TIER_KEY].set_at == "2026-05-25T12:00:00Z"


def test_write_multiple_keys_same_bot(shared_dir):
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    write_override(shared_dir, "security_bot", _SUMMARIZER_KEY, 5, set_by="operator")
    write_override(shared_dir, "security_bot", _COST_LEDGER_KEY, False, set_by="operator")
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert set(bo.overrides.keys()) == {_TIER_KEY, _SUMMARIZER_KEY, _COST_LEDGER_KEY}


def test_write_multiple_bots_create_separate_files(shared_dir):
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    write_override(shared_dir, "team_bot_a", _TIER_KEY, "manage", set_by="operator")
    security_bot_bo = read_bot_overrides(shared_dir, "security_bot")
    team_bot_a_bo = read_bot_overrides(shared_dir, "team_bot_a")
    assert security_bot_bo.overrides[_TIER_KEY].value == "monitor"
    assert team_bot_a_bo.overrides[_TIER_KEY].value == "manage"


def test_write_records_provenance(shared_dir, fixed_now):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", note="cross-cutting log check", now=fixed_now,
    )
    prov = lookup(_TIER_KEY, bot_id="security_bot", shared_dir=shared_dir)
    assert prov is not None
    assert prov.set_by == "operator"
    assert prov.set_value == "monitor"
    assert prov.reason == "cross-cutting log check"


# ─── Write (validation) ───────────────────────────────────────────────────


def test_write_unknown_schema_path_rejected(shared_dir):
    """Otherwise the overrides file would carry the same bug class as PR #1525
    (schema-removed key persisting through writes)."""
    with pytest.raises(OverrideValidationError, match="unknown schema path"):
        write_override(
            shared_dir, "security_bot", "openclaw.not.a.real.key", "x",
            set_by="operator",
        )
    # And the file must NOT be created as a side effect.
    assert not path_for_bot(shared_dir, "security_bot").exists()


def test_write_pod_wide_key_rejected(shared_dir):
    """Phase 2 is per-bot only — pod-wide keys belong in network.json."""
    # network.thresholds.dailySpendAlertUsd is a known per_bot=False entry.
    pod_key = "network.thresholds.dailySpendAlertUsd"
    with pytest.raises(OverrideValidationError, match="pod-wide key"):
        write_override(shared_dir, "security_bot", pod_key, 10.0, set_by="operator")


def test_write_wrong_type_rejected_int_for_enum(shared_dir):
    with pytest.raises(OverrideValidationError, match="wrong type"):
        write_override(shared_dir, "security_bot", _TIER_KEY, 42, set_by="operator")
    assert not path_for_bot(shared_dir, "security_bot").exists()


def test_write_wrong_type_rejected_bool_for_int(shared_dir):
    """Strict: bool is not int. summarizerMinTurns=True should be rejected
    even though Python considers True a valid int."""
    with pytest.raises(OverrideValidationError, match="wrong type"):
        write_override(shared_dir, "security_bot", _SUMMARIZER_KEY, True, set_by="operator")


def test_write_wrong_type_rejected_string_for_bool(shared_dir):
    with pytest.raises(OverrideValidationError, match="wrong type"):
        write_override(shared_dir, "security_bot", _COST_LEDGER_KEY, "yes", set_by="operator")


def test_write_int_accepted_for_float_key(shared_dir):
    """Python ints are valid floats — confidence floor accepts 1 as 1.0."""
    write_override(shared_dir, "security_bot", _CONFIDENCE_KEY, 1, set_by="operator")
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.get(_CONFIDENCE_KEY).value == 1


def test_write_float_accepted_for_float_key(shared_dir):
    write_override(shared_dir, "security_bot", _CONFIDENCE_KEY, 0.75, set_by="operator")
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.get(_CONFIDENCE_KEY).value == 0.75


def test_write_rejected_with_invalid_bot_id(shared_dir):
    with pytest.raises(OverrideValidationError, match="invalid bot_id"):
        write_override(shared_dir, "../escape", _TIER_KEY, "monitor", set_by="operator")


# ─── expires_at validation ────────────────────────────────────────────────


def test_expires_at_none_accepted(shared_dir):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", expires_at=None,
    )
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.get(_TIER_KEY).expires_at is None


def test_expires_at_iso_date_accepted(shared_dir):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-08-01",
    )
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.get(_TIER_KEY).expires_at == "2026-08-01"


def test_expires_at_iso_datetime_with_z_accepted(shared_dir):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", expires_at="2026-08-01T14:00:00Z",
    )
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.get(_TIER_KEY).expires_at == "2026-08-01T14:00:00Z"


def test_expires_at_malformed_rejected(shared_dir):
    """Phase 5 will parse this field; if it's malformed there, the override
    silently never expires. Catch at write time."""
    for bad in ("not a date", "2026-13-99", "tomorrow", "01-08-2026"):
        with pytest.raises(OverrideValidationError, match="expires_at"):
            write_override(
                shared_dir, "security_bot", _TIER_KEY, "monitor",
                set_by="operator", expires_at=bad,
            )


def test_expires_at_non_string_rejected(shared_dir):
    with pytest.raises(OverrideValidationError, match="expires_at"):
        write_override(
            shared_dir, "security_bot", _TIER_KEY, "monitor",
            set_by="operator", expires_at=12345,
        )


# ─── Malformed-existing-file safety ───────────────────────────────────────


def test_write_refuses_to_clobber_malformed_file(shared_dir, fixed_now):
    """Otherwise a corrupted security_bot.json silently destroys every operator
    override on security_bot the next time anyone writes one. Refuse instead;
    surface the corruption so the operator can recover it manually."""
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", now=fixed_now,
    )
    path = path_for_bot(shared_dir, "security_bot")
    path.write_text("{ corrupted mid-line")

    with pytest.raises(OverrideStateError, match="refusing to overwrite"):
        write_override(
            shared_dir, "security_bot", _SUMMARIZER_KEY, 5,
            set_by="operator",
        )


def test_write_refuses_future_schema_version(shared_dir, fixed_now):
    """A newer admin writing schema_version=2 means rollback in progress;
    we don't downgrade silently."""
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="operator", now=fixed_now,
    )
    path = path_for_bot(shared_dir, "security_bot")
    # Hand-craft a file with schema_version 99
    path.write_text(json.dumps({
        "schema_version": 99,
        "bot_id": "security_bot",
        "overrides": {},
    }))

    with pytest.raises(OverrideStateError, match="schema_version=99 is newer"):
        write_override(
            shared_dir, "security_bot", _SUMMARIZER_KEY, 5,
            set_by="operator",
        )


def test_write_accepts_empty_file(shared_dir):
    """An empty (zero-byte) file is fine — equivalent to absent."""
    path = path_for_bot(shared_dir, "security_bot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    # Should not raise.
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.get(_TIER_KEY).value == "monitor"


def test_read_malformed_file_still_returns_empty(shared_dir):
    """The defensive read path stays defensive — callers that just want to
    *read* (e.g. the customizations UI) shouldn't crash on a corrupted
    file. Only the write path refuses."""
    path = path_for_bot(shared_dir, "security_bot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken")
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert bo.overrides == {}


# ─── Locking / concurrency ────────────────────────────────────────────────


def test_concurrent_writes_serialize_via_flock(shared_dir):
    """Two threads writing different keys for the same bot — both writes
    survive because the lock serializes the read-modify-write."""
    import threading

    results: list[Exception | None] = []

    def w(key: str, value):
        try:
            write_override(shared_dir, "security_bot", key, value, set_by="operator")
            results.append(None)
        except Exception as e:   # pragma: no cover - shouldn't trigger
            results.append(e)

    t1 = threading.Thread(target=w, args=(_TIER_KEY, "monitor"))
    t2 = threading.Thread(target=w, args=(_SUMMARIZER_KEY, 5))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert all(r is None for r in results)
    bo = read_bot_overrides(shared_dir, "security_bot")
    # Without flock, one of these would be lost (classic last-writer-wins).
    assert set(bo.overrides.keys()) == {_TIER_KEY, _SUMMARIZER_KEY}


# ─── iter_all_overrides resilience ────────────────────────────────────────


def test_iter_all_overrides_logs_and_skips_corrupted_file(shared_dir, caplog):
    write_override(shared_dir, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    write_override(shared_dir, "security_bot", _TIER_KEY, "manage", set_by="operator")
    # Corrupt security_bot's file in-place.
    path_for_bot(shared_dir, "security_bot").write_text("{ broken")

    import logging
    with caplog.at_level(logging.WARNING, logger="evolve_admin.config_sandbox.overrides"):
        entries = list(iter_all_overrides(shared_dir))

    # team_bot_a still yields; security_bot is skipped but logged.
    keys = {(b, k) for b, k, _ in entries}
    assert keys == {("team_bot_a", _TIER_KEY)}
    assert any("skipping corrupted file" in rec.message for rec in caplog.records)


def test_iter_all_overrides_skips_files_with_invalid_bot_id_names(shared_dir):
    """A stray ``Backup.json`` or ``.hidden.json`` in the dir doesn't blow
    up the iterator — they don't match the bot_id regex, so they're
    skipped silently."""
    write_override(shared_dir, "team_bot_a", _TIER_KEY, "monitor", set_by="operator")
    d = overrides_dir(shared_dir)
    (d / "Backup.json").write_text("{}")
    (d / ".hidden.json").write_text("{}")
    entries = list(iter_all_overrides(shared_dir))
    assert len(entries) == 1
    assert entries[0][0] == "team_bot_a"


# ─── Delete ───────────────────────────────────────────────────────────────


def test_delete_removes_entry_and_provenance(shared_dir):
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    assert lookup(_TIER_KEY, bot_id="security_bot", shared_dir=shared_dir) is not None

    removed = delete_override(shared_dir, "security_bot", _TIER_KEY)
    assert removed is True
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert _TIER_KEY not in bo.overrides
    assert lookup(_TIER_KEY, bot_id="security_bot", shared_dir=shared_dir) is None


def test_delete_missing_key_returns_false(shared_dir):
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    removed = delete_override(shared_dir, "security_bot", _SUMMARIZER_KEY)
    assert removed is False
    # Existing override is untouched
    bo = read_bot_overrides(shared_dir, "security_bot")
    assert _TIER_KEY in bo.overrides


def test_delete_when_no_file_returns_false(shared_dir):
    assert delete_override(shared_dir, "security_bot", _TIER_KEY) is False


# ─── mark_reviewed ────────────────────────────────────────────────────────


def test_mark_reviewed_clears_flag_and_reattributes(shared_dir, fixed_now):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="auto:drift_2026-05-24T18:00:00Z",
        note="Auto-recorded from ad-hoc edit; review.",
        needs_review=True, now=fixed_now,
    )
    later = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    updated = mark_reviewed(shared_dir, "security_bot", _TIER_KEY, now=later)
    assert updated is not None
    assert updated.value == "monitor"          # value preserved
    assert updated.set_by == "operator"        # reattributed
    assert updated.needs_review is False
    assert updated.set_at == "2026-05-25T12:00:00Z"
    # Provenance reflects the operator acceptance, not the auto-record.
    prov = lookup(_TIER_KEY, bot_id="security_bot", shared_dir=shared_dir)
    assert prov is not None
    assert prov.set_by == "operator"


def test_mark_reviewed_preserves_existing_note_when_not_provided(shared_dir):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="auto:drift", note="original note", needs_review=True,
    )
    updated = mark_reviewed(shared_dir, "security_bot", _TIER_KEY)
    assert updated.note == "original note"


def test_mark_reviewed_appends_new_note_when_provided(shared_dir):
    write_override(
        shared_dir, "security_bot", _TIER_KEY, "monitor",
        set_by="auto:drift", note="original", needs_review=True,
    )
    updated = mark_reviewed(shared_dir, "security_bot", _TIER_KEY, note="operator's annotation")
    assert updated.note == "operator's annotation"


def test_mark_reviewed_missing_key_returns_none(shared_dir):
    assert mark_reviewed(shared_dir, "security_bot", _TIER_KEY) is None


# ─── iter_all_overrides ───────────────────────────────────────────────────


def test_iter_all_overrides_yields_every_bots_entries(shared_dir):
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    write_override(shared_dir, "team_bot_a", _TIER_KEY, "manage", set_by="operator")
    write_override(shared_dir, "team_bot_a", _SUMMARIZER_KEY, 5, set_by="operator")
    entries = list(iter_all_overrides(shared_dir))
    keys = {(bot_id, key) for bot_id, key, _ in entries}
    assert keys == {
        ("security_bot", _TIER_KEY),
        ("team_bot_a", _TIER_KEY),
        ("team_bot_a", _SUMMARIZER_KEY),
    }


def test_iter_all_overrides_empty_when_no_dir(shared_dir):
    assert list(iter_all_overrides(shared_dir)) == []


def test_iter_all_overrides_skips_non_json_files(shared_dir):
    # Touch a stray file in the overrides dir.
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    stray = overrides_dir(shared_dir) / "readme.txt"
    stray.write_text("ignore me")
    entries = list(iter_all_overrides(shared_dir))
    assert len(entries) == 1
    assert entries[0][0] == "security_bot"


# ─── Atomicity ────────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_temp_on_success(shared_dir):
    write_override(shared_dir, "security_bot", _TIER_KEY, "monitor", set_by="operator")
    # No .tmp files left behind
    tmps = list(overrides_dir(shared_dir).glob(".*.tmp"))
    assert tmps == []
