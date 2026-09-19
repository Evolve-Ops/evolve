"""Regression guards for SCAN_PHASES + PHASE_TOTAL invariants.

Production validation 2026-06-08: personal-bot (and atlas) scans showed
"Phase 7 of 5" — phase numbers exceeded the reported total because
SCAN_PHASES only declared 4 entries while the actual scan ran 8
phases (inventory, llm_discovery, merge, manifests, file_stamp,
layer_classify, reconcile, coherence_pass_a).

This file pins the invariant: every phase emitted by ``_write_status``
calls in scanner.py must have a corresponding entry in SCAN_PHASES
with ``num`` in the 1..PHASE_TOTAL range.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.scanner import (  # noqa: E402
    PHASE_TOTAL,
    SCAN_PHASES,
)


def test_phase_total_matches_scan_phases_length() -> None:
    """PHASE_TOTAL must equal len(SCAN_PHASES). Don't let them drift."""
    assert PHASE_TOTAL == len(SCAN_PHASES)


def test_phase_numbers_are_sequential_1_to_total() -> None:
    """Each phase's ``num`` field must be its 1-indexed position so
    UI rendering ``phase / phase_total`` always stays within bounds."""
    for i, phase in enumerate(SCAN_PHASES, start=1):
        assert phase["num"] == i, (
            f"Phase at index {i} has num={phase['num']}; expected {i}"
        )


def test_each_phase_has_required_fields() -> None:
    """Every phase entry must carry the four fields the UI + status
    file rely on."""
    required = {"num", "name", "desc", "eta_s"}
    for phase in SCAN_PHASES:
        missing = required - set(phase.keys())
        assert not missing, (
            f"Phase {phase.get('num')} ({phase.get('name')}) "
            f"missing fields: {missing}"
        )


def test_phase_names_are_unique() -> None:
    """Phase names index the UI's progress dot list and the bot-side
    audit trail. Duplicates would silently overwrite."""
    names = [p["name"] for p in SCAN_PHASES]
    assert len(names) == len(set(names)), (
        f"Duplicate phase names in SCAN_PHASES: {names}"
    )


def test_phase_total_covers_post_manifest_phases() -> None:
    """The fix-of-record: the four post-Phase-4 passes (file_stamp,
    layer_classify, reconcile, coherence_pass_a) must be declared so
    their phase numbers don't exceed PHASE_TOTAL.

    Before the fix: SCAN_PHASES had 4 entries, PHASE_TOTAL = 4, and
    Phase 7 wrote 'phase 7 of 5' (PHASE_TOTAL + 1 as a fudge factor).
    After: 8 entries, PHASE_TOTAL = 8, Phase 7 writes 'phase 7 of 8'.
    """
    assert PHASE_TOTAL >= 8, (
        f"PHASE_TOTAL is {PHASE_TOTAL}; expected at least 8 to cover "
        f"file_stamp + layer_classify + reconcile + coherence_pass_a "
        f"phases that run after manifest generation."
    )
    names = [p["name"] for p in SCAN_PHASES]
    for required in ("file_stamp", "layer_classify",
                     "reconcile", "coherence_pass_a"):
        assert required in names, (
            f"SCAN_PHASES missing entry for '{required}' — the "
            f"corresponding _write_status call in scanner.py would "
            f"emit a phase number outside the [1, PHASE_TOTAL] range."
        )


def test_phase_etas_are_positive_ints() -> None:
    """ETAs feed the UI's 'remaining time' calc. Negative or zero
    would render '~0s remaining' permanently."""
    for phase in SCAN_PHASES:
        eta = phase.get("eta_s")
        assert isinstance(eta, int) and eta > 0, (
            f"Phase {phase.get('num')} ({phase.get('name')}): "
            f"eta_s={eta!r}; expected positive int"
        )
