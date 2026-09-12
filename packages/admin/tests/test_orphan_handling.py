"""Orphan handling tests — PR 14.

Spec: internal/spec-app-coherence-and-reconciliation-2026-06-05.md §9.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.orphan_handling import (  # noqa: E402
    DEFAULT_CONSECUTIVE_VERDICTS,
    VERDICT_NOT_ORPHAN,
    VERDICT_ORPHAN,
    VERDICT_PENDING,
    OrphanVerdict,
    evaluate_orphan,
    notification_message,
    record_orphan_verdict,
)


# ── No infra files case ────────────────────────────────────────────────

def test_no_infra_files_is_not_orphan() -> None:
    """A pure-data app (data/log/state only) can't be 'orphan' — it has
    no infra contract to fail."""
    manifest = {
        "files": [{"path": "data/x.md", "layer": "data"}],
    }
    verdict = evaluate_orphan(manifest)
    assert verdict.verdict == VERDICT_NOT_ORPHAN


def test_procedure_layer_counts_as_infra() -> None:
    """Production calibration 2026-06-06: procedures/*.md are the LLM's
    instructions — if they disappear, the cron has nothing to do.
    Same orphan-eligibility shape as code/config/contract."""
    manifest = {
        "files": [{"path": "procedures/x.md", "layer": "procedure"}],
        "reconciliation": {
            "missing_files": [{"path": "procedures/x.md"}],
        },
    }
    verdict = evaluate_orphan(manifest)
    # First tick → pending (hysteresis); important is that it ISN'T
    # not_orphan (which would mean the layer was ignored entirely).
    assert verdict.verdict != VERDICT_NOT_ORPHAN


def test_empty_files_list_is_not_orphan() -> None:
    """Manifest with no files at all → not orphan (could be a
    pure-config app or a manifest awaiting initialisation)."""
    manifest = {"files": []}
    verdict = evaluate_orphan(manifest)
    assert verdict.verdict == VERDICT_NOT_ORPHAN


# ── Hysteresis ─────────────────────────────────────────────────────────

def test_first_tick_with_all_infra_missing_is_pending() -> None:
    """Spec §9: orphan requires 2 consecutive verdicts. First missing-
    everything tick → pending, not orphan."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
        },
    }
    verdict = evaluate_orphan(manifest)
    assert verdict.verdict == VERDICT_PENDING
    assert verdict.consecutive_orphan_verdicts == 1


def test_second_consecutive_tick_confirms_orphan() -> None:
    """After 2 consecutive orphan ticks, status is confirmed."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
            "orphan_check_history": [
                {"ts": "...", "verdict": VERDICT_PENDING},
            ],
        },
    }
    verdict = evaluate_orphan(manifest)
    assert verdict.verdict == VERDICT_ORPHAN
    assert verdict.confirmed is True
    assert verdict.consecutive_orphan_verdicts == 2


def test_intervening_non_orphan_resets_chain() -> None:
    """A not_orphan verdict in the history breaks the chain. Spec §9
    hysteresis prevents flapping."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
            # History has 2 pending verdicts but record_orphan_verdict
            # would have CLEARED them on a not_orphan tick — so any
            # entries present count toward consecutive.
            # This test confirms the count semantics.
            "orphan_check_history": [
                # Simulating: prior 1 pending was cleared by a
                # not_orphan; history starts fresh.
            ],
        },
    }
    verdict = evaluate_orphan(manifest)
    # No history → this is the first orphan tick → pending.
    assert verdict.verdict == VERDICT_PENDING


# ── Volatile_paths can prevent orphan ──────────────────────────────────

def test_volatile_paths_match_keeps_app_alive() -> None:
    """Spec §9: orphan requires NO volatile_paths glob to have files.
    An active data directory → not orphan."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
        },
    }
    verdict = evaluate_orphan(
        manifest,
        workspace_files={"data/2026-06-06.md"},   # glob matches
    )
    assert verdict.verdict == VERDICT_NOT_ORPHAN


def test_volatile_paths_no_match_does_not_save_app() -> None:
    """When the glob doesn't match anything, the app is still orphan-eligible."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
            "orphan_check_history": [{"verdict": VERDICT_PENDING}],
        },
    }
    verdict = evaluate_orphan(
        manifest,
        workspace_files={"logs/audit.log"},   # doesn't match glob
    )
    assert verdict.verdict == VERDICT_ORPHAN


def test_workspace_files_none_skips_glob_check() -> None:
    """Defensive: when scanner didn't compute workspace_files, skip
    the glob check — assume the manifest's infra-files-missing claim
    is authoritative."""
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "volatile_paths": [{"glob": "data/*.md", "layer": "data"}],
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
            "orphan_check_history": [{"verdict": VERDICT_PENDING}],
        },
    }
    verdict = evaluate_orphan(manifest, workspace_files=None)
    assert verdict.verdict == VERDICT_ORPHAN


# ── Partial missing → not orphan ────────────────────────────────────────

def test_partial_missing_is_not_orphan() -> None:
    """Some infra files missing but not all → drift, not orphan."""
    manifest = {
        "files": [
            {"path": "scripts/a.py", "layer": "code"},
            {"path": "scripts/b.py", "layer": "code"},
        ],
        "reconciliation": {
            "missing_files": [{"path": "scripts/a.py"}],
            # b.py present.
        },
    }
    verdict = evaluate_orphan(manifest)
    assert verdict.verdict == VERDICT_NOT_ORPHAN


# ── Provenance modulates is_authored flag ───────────────────────────────

def test_orphan_verdict_records_observational() -> None:
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        # No provenance → observational default.
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
            "orphan_check_history": [{"verdict": VERDICT_PENDING}],
        },
    }
    verdict = evaluate_orphan(manifest)
    assert verdict.confirmed
    assert verdict.is_authored is False


def test_orphan_verdict_records_authored() -> None:
    manifest = {
        "files": [{"path": "scripts/main.py", "layer": "code"}],
        "provenance": {"field_origins": {"files": {"source": "forge_built"}}},
        "reconciliation": {
            "missing_files": [{"path": "scripts/main.py"}],
            "orphan_check_history": [{"verdict": VERDICT_PENDING}],
        },
    }
    verdict = evaluate_orphan(manifest)
    assert verdict.confirmed
    assert verdict.is_authored is True


# ── record_orphan_verdict ─────────────────────────────────────────────

def test_record_appends_to_history() -> None:
    manifest = {"reconciliation": {}}
    verdict = OrphanVerdict(verdict=VERDICT_PENDING,
                              checked_at="2026-06-06T12:00:00Z",
                              reason="x")
    record_orphan_verdict(manifest, verdict)
    history = manifest["reconciliation"]["orphan_check_history"]
    assert len(history) == 1


def test_record_not_orphan_clears_history() -> None:
    """Spec §9: a not_orphan tick clears the chain — hysteresis must
    start from zero for the next orphan-confirmation."""
    manifest = {"reconciliation": {
        "orphan_check_history": [
            {"verdict": VERDICT_PENDING},
            {"verdict": VERDICT_PENDING},
        ],
    }}
    verdict = OrphanVerdict(verdict=VERDICT_NOT_ORPHAN,
                              checked_at="...")
    record_orphan_verdict(manifest, verdict)
    assert manifest["reconciliation"]["orphan_check_history"] == []


def test_record_orphan_sets_reconciliation_status() -> None:
    manifest = {"reconciliation": {}}
    verdict = OrphanVerdict(verdict=VERDICT_ORPHAN,
                              checked_at="...", is_authored=True)
    record_orphan_verdict(manifest, verdict)
    assert manifest["reconciliation"]["status"] == "orphan"


def test_record_not_orphan_clears_status() -> None:
    """A previously-orphan app that revives clears its orphan status."""
    manifest = {"reconciliation": {"status": "orphan"}}
    verdict = OrphanVerdict(verdict=VERDICT_NOT_ORPHAN, checked_at="...")
    record_orphan_verdict(manifest, verdict)
    assert manifest["reconciliation"]["status"] == "ok"


def test_record_caps_history_length() -> None:
    manifest = {"reconciliation": {
        "orphan_check_history": [
            {"verdict": VERDICT_PENDING, "ts": f"t{i}"}
            for i in range(20)
        ],
    }}
    verdict = OrphanVerdict(verdict=VERDICT_PENDING, checked_at="new")
    record_orphan_verdict(manifest, verdict, max_history=5)
    history = manifest["reconciliation"]["orphan_check_history"]
    assert len(history) == 5
    # Most recent is at the end.
    assert history[-1]["ts"] == "new"


# ── notification_message ──────────────────────────────────────────────

def test_notification_silent_for_observational_orphan() -> None:
    """Spec §9: observational orphan is silent (no notification)."""
    verdict = OrphanVerdict(verdict=VERDICT_ORPHAN, is_authored=False)
    assert notification_message(app_id="x", verdict=verdict) is None


def test_notification_emitted_for_authored_orphan() -> None:
    verdict = OrphanVerdict(verdict=VERDICT_ORPHAN, is_authored=True)
    msg = notification_message(app_id="myapp", verdict=verdict,
                               bot_id="personal-bot")
    assert msg is not None
    assert "myapp" in msg
    assert "Retired" in msg
    assert "Reinstall" in msg


def test_notification_none_for_pending() -> None:
    verdict = OrphanVerdict(verdict=VERDICT_PENDING, is_authored=True)
    assert notification_message(app_id="x", verdict=verdict) is None


# ── Constants ────────────────────────────────────────────────────────

def test_default_consecutive_verdicts_is_two() -> None:
    assert DEFAULT_CONSECUTIVE_VERDICTS == 2
