"""Tests for analyzer.generators._intent_memory.

Phase 1 of internal/spec-config-intent-system-2026-05-21.md §2.5.

The dedup log lets intent-aware generators skip re-emitting the same
audit-only Signal every sweep. Tests pin:

  - first emission is never deduped
  - repeated (generator, signature) within window dedups
  - distinct generators/signatures don't cross-dedup
  - entries older than the 90-day window stop suppressing
  - malformed lines in the log don't block iteration
  - missing log dir / file is auto-created on first record
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators._intent_memory import (  # noqa: E402
    DEDUP_WINDOW_DAYS,
    already_emitted,
    record_emission,
)


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "shared"
    sd.mkdir()
    return sd


_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


# ── First emission is never deduped ─────────────────────────────────────────


def test_first_emission_not_deduped(shared_dir: Path):
    assert already_emitted(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc123",
        shared_dir=shared_dir, now=_NOW,
    ) is False


def test_record_then_already_emitted_returns_true(shared_dir: Path):
    record_emission(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc123",
        shared_dir=shared_dir, now=_NOW,
    )
    assert already_emitted(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc123",
        shared_dir=shared_dir, now=_NOW + timedelta(minutes=1),
    ) is True


# ── Cross-isolation: different generators/signatures don't dedup each other ─


def test_distinct_generators_do_not_cross_dedup(shared_dir: Path):
    record_emission(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW,
    )
    assert already_emitted(
        generator_id="cron_caps_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW + timedelta(minutes=1),
    ) is False


def test_distinct_signatures_do_not_cross_dedup(shared_dir: Path):
    record_emission(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW,
    )
    assert already_emitted(
        generator_id="auth_drift_filler",
        signature="admin_bot:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW + timedelta(minutes=1),
    ) is False


# ── 90-day dedup window (spec §2.5) ─────────────────────────────────────────


def test_entry_outside_window_does_not_suppress(shared_dir: Path):
    """An intent the operator forgot about should get a periodic reminder
    instead of silence forever. Pin the 90-day window from the spec."""
    record_emission(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW,
    )
    future = _NOW + timedelta(days=DEDUP_WINDOW_DAYS + 1)
    assert already_emitted(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=future,
    ) is False


def test_entry_inside_window_does_suppress(shared_dir: Path):
    record_emission(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW,
    )
    inside = _NOW + timedelta(days=DEDUP_WINDOW_DAYS - 1)
    assert already_emitted(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=inside,
    ) is True


# ── Malformed lines + missing file fall back safely ─────────────────────────


def test_malformed_lines_do_not_block_dedup(shared_dir: Path):
    """JSONL's value: one corrupt line never blocks the rest of the log.
    Silent-failure guard: a previous design used JSON-array reads that
    failed loud on a single torn write, swallowing the entire dedup
    state and producing a wave of duplicate audit signals."""
    log_path = shared_dir / "config_intents" / "_generator_memory.jsonl"
    log_path.parent.mkdir(parents=True)
    good = {"at": _NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator_id": "auth_drift_filler",
            "signature": "team_bot_a:tools.exec.security:audit_only:intent-abc",
            "event": "audit_signal_emitted"}
    log_path.write_text(
        "{not json"
        "\n"
        + json.dumps(good) + "\n"
        + "{still not json\n"
    )
    assert already_emitted(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW + timedelta(minutes=1),
    ) is True


def test_record_emission_creates_parent_dir(shared_dir: Path):
    assert not (shared_dir / "config_intents").exists()
    record_emission(
        generator_id="auth_drift_filler",
        signature="team_bot_a:tools.exec.security:audit_only:intent-abc",
        shared_dir=shared_dir, now=_NOW,
    )
    log_path = shared_dir / "config_intents" / "_generator_memory.jsonl"
    assert log_path.exists()
    assert log_path.read_text().strip().count("\n") == 0  # one line


def test_record_emission_appends_each_call(shared_dir: Path):
    record_emission(
        generator_id="auth_drift_filler", signature="a",
        shared_dir=shared_dir, now=_NOW,
    )
    record_emission(
        generator_id="auth_drift_filler", signature="b",
        shared_dir=shared_dir, now=_NOW + timedelta(seconds=1),
    )
    log_path = shared_dir / "config_intents" / "_generator_memory.jsonl"
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    sigs = {json.loads(l)["signature"] for l in lines}
    assert sigs == {"a", "b"}


def test_empty_signature_or_generator_id_is_a_no_op(shared_dir: Path):
    """Defensive guard: a caller passing an empty string by mistake mustn't
    poison the dedup log with an entry that matches every other empty
    accidental lookup."""
    record_emission(generator_id="", signature="x",
                    shared_dir=shared_dir, now=_NOW)
    record_emission(generator_id="x", signature="",
                    shared_dir=shared_dir, now=_NOW)
    log_path = shared_dir / "config_intents" / "_generator_memory.jsonl"
    assert not log_path.exists()
    assert already_emitted(generator_id="", signature="x",
                           shared_dir=shared_dir, now=_NOW) is False
