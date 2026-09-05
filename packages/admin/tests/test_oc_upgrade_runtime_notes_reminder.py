"""Tests for the RUNTIME_NOTES.md review-reminder Signal fired by
evolve_admin.ocadmin._emit_runtime_notes_review_signal.

When OpenClaw is upgraded to a new version, RUNTIME_NOTES.md entries —
each one tied to an OC upstream constraint — may have become stale.
The oc_upgrade flow fires a Signal nudging the operator to walk the
file and prune stale entries. This test pins:

- A real version change (old != new) produces a firing Signal with the
  expected producer, type, signature, scope, and severity.
- Re-running the helper with the same old/new version is a no-op (the
  early return guard, so a no-op `oc upgrade` re-run doesn't fire).
- Re-firing with the SAME new version dedups via observe()'s signature
  index — a single firing signal, observation_count bumped.
- A different new_version supersedes: the new reminder fires and any
  still-active reminder for an OLDER version is auto-resolved (Track 1.4
  of internal/review-alerts-findings-noise-2026-08-30.md — the mini had
  reminders for two versions firing simultaneously).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin import ocadmin


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "evolve"
    (sd / "signals" / "firing").mkdir(parents=True)
    (sd / "signals" / "snoozed").mkdir(parents=True)
    (sd / "signals" / "archived").mkdir(parents=True)
    return sd


def _firing_signals(shared_dir: Path) -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted((shared_dir / "signals" / "firing").glob("*.json"))
    ]


def test_version_change_fires_signal(shared_dir: Path) -> None:
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.20", "2026.5.26", shared_dir=shared_dir,
    )
    signals = _firing_signals(shared_dir)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["producer"] == ocadmin.RUNTIME_NOTES_REMINDER_PRODUCER
    assert sig["type"] == ocadmin.RUNTIME_NOTES_REMINDER_TYPE
    # Signature must include the new version so each upgrade tracks
    # independently.
    assert "2026.5.26" in sig["signature"]
    assert sig["scope"] == "pod"
    assert sig["severity"] == "info"
    assert sig["flavor"] == "maintenance"
    assert sig["state"] == "firing"
    assert sig["details"]["old_version"] == "2026.5.20"
    assert sig["details"]["new_version"] == "2026.5.26"
    assert "RUNTIME_NOTES" in sig["title"]


def test_no_version_change_does_not_fire(shared_dir: Path) -> None:
    """`oc upgrade --force` against the same version (or any re-run that
    doesn't actually flip the package) must not fire the reminder."""
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.26", "2026.5.26", shared_dir=shared_dir,
    )
    assert _firing_signals(shared_dir) == []


def test_same_new_version_dedups_via_signature(shared_dir: Path) -> None:
    """Calling twice with the same new_version produces one Signal, with
    observation_count bumped — observe()'s find-or-create behavior."""
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.20", "2026.5.26", shared_dir=shared_dir,
    )
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.20", "2026.5.26", shared_dir=shared_dir,
    )
    signals = _firing_signals(shared_dir)
    assert len(signals) == 1
    assert signals[0]["observation_count"] == 2


def _archived_signals(shared_dir: Path) -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted((shared_dir / "signals" / "archived").glob("*.json"))
    ]


def test_new_version_supersedes_older_reminder(shared_dir: Path) -> None:
    """A subsequent upgrade's reminder replaces any still-open reminder
    for an older version — walking RUNTIME_NOTES.md once against the
    newest version covers everything the older reminders asked for."""
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.20", "2026.5.26", shared_dir=shared_dir,
    )
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.26", "2026.6.0", shared_dir=shared_dir,
    )
    firing = _firing_signals(shared_dir)
    assert len(firing) == 1
    assert firing[0]["details"]["new_version"] == "2026.6.0"

    archived = _archived_signals(shared_dir)
    assert len(archived) == 1
    old = archived[0]
    assert old["details"]["new_version"] == "2026.5.26"
    assert old["state"] == "resolved"
    last_transition = old["state_history"][-1]
    assert "superseded" in last_transition["reason"]
    assert "2026.6.0" in last_transition["reason"]


def test_supersede_scoped_to_reminder_type(shared_dir: Path) -> None:
    """The supersede sweep must not resolve other signals — only
    runtime_notes_review_due reminders for older versions."""
    import importlib

    store = importlib.import_module("signals.store")
    unrelated = store.observe(
        shared_dir,
        signature="other_producer:other_type:all",
        producer="other_producer",
        type="other_type",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        title="unrelated",
    )
    ocadmin._emit_runtime_notes_review_signal(
        "2026.5.26", "2026.6.0", shared_dir=shared_dir,
    )
    firing = _firing_signals(shared_dir)
    states = {s["id"]: s["state"] for s in firing}
    assert states.get(unrelated.id) == "firing"
    assert len(firing) == 2
