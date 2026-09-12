"""tests/test_track_record.py — Track-record bump helper + authority discount."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.ranking import (  # noqa: E402
    AUTHORITY_MAX,
    AUTHORITY_MIN,
    ITERATION_DISCOUNT,
    compute_authority,
)
from arbiter.refine import RefineResult, apply_refinement  # noqa: E402
from arbiter.track_record import (  # noqa: E402
    bump_for_status_transition,
    bump_proposals_emitted,
)
from schema.generator import (  # noqa: E402
    Charter,
    GeneratorRecord,
    TrackRecord,
)
from testing.harness import make_investigation_proposal  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# compute_authority — iteration discount
# ──────────────────────────────────────────────────────────────────────────


def test_authority_unchanged_for_legacy_callers_without_iteration():
    """No succeeded_after_iteration provided → original formula."""
    a_legacy = compute_authority(verified_success=10, verified_failed=0)
    a_explicit = compute_authority(
        verified_success=10, verified_failed=0, succeeded_after_iteration=0
    )
    assert a_legacy == a_explicit
    # Spec formula: 1.0 + 0.3 * (10-0)/10 = 1.30 (under the AUTHORITY_MAX cap of 1.5)
    assert abs(a_explicit - 1.30) < 1e-9


def test_authority_discounts_iterated_successes():
    """A generator with all-iterated wins scores lower than one with all
    first-shot wins. Same totals, different proportions."""
    # 10 wins, 0 losses, all first-shot → max authority
    a_first_shot = compute_authority(
        verified_success=10, verified_failed=0, succeeded_after_iteration=0
    )
    # 10 wins, 0 losses, all iterated (5 wins effectively at 0.5×) → lower
    a_iterated = compute_authority(
        verified_success=10, verified_failed=0, succeeded_after_iteration=10
    )
    assert a_first_shot > a_iterated
    # Effective wins = 10 - 0.5*10 = 5; n = 10; raw = 1.0 + 0.3 * (5/10) = 1.15
    assert abs(a_iterated - 1.15) < 1e-9


def test_authority_clamps_iteration_count_to_success_count():
    """If after_iteration > verified_success (impossible in practice), the
    formula clamps so we don't get nonsensical negative effective_wins."""
    a = compute_authority(
        verified_success=5, verified_failed=0, succeeded_after_iteration=999
    )
    # Clamped to 5; effective_wins = 5 - 0.5*5 = 2.5; n=5; raw = 1.0 + 0.3 * (2.5/5) = 1.15
    assert abs(a - 1.15) < 1e-9


def test_authority_with_mixed_first_shot_and_iterated():
    """Realistic case: 6 first-shot, 4 iterated, 0 losses.
    effective = 6 + 0.5*4 = 8; n=10; raw = 1.0 + 0.3 * 0.8 = 1.24"""
    a = compute_authority(
        verified_success=10, verified_failed=0, succeeded_after_iteration=4
    )
    assert abs(a - 1.24) < 1e-9


def test_authority_iterated_still_beats_failed():
    """Even all-iterated wins beat all-failed losses."""
    a_iterated = compute_authority(
        verified_success=10, verified_failed=0, succeeded_after_iteration=10
    )
    a_failed = compute_authority(verified_success=0, verified_failed=10)
    assert a_iterated > a_failed
    # Spec formula: 1.0 + 0.3 * (0-10)/10 = 0.70 (above the AUTHORITY_MIN floor of 0.5)
    assert abs(a_failed - 0.70) < 1e-9


def test_iteration_discount_constant_is_half():
    assert ITERATION_DISCOUNT == 0.5


# ──────────────────────────────────────────────────────────────────────────
# bump_for_status_transition — end to end with a real GeneratorRecord
# ──────────────────────────────────────────────────────────────────────────


def _setup_generator_in_tmpdir(
    tmp_path: Path, generator_id: str = "test_gen"
) -> Path:
    """Lay out a minimal shared_dir with one GeneratorRecord on disk so
    the bump helper has something to update. Returns the shared_dir."""
    shared = tmp_path
    records_dir = shared / "generators"
    records_dir.mkdir(parents=True, exist_ok=True)

    record = GeneratorRecord(
        id=generator_id,
        charter_fingerprint="test_fp",
        track_record=TrackRecord(),
    )
    (records_dir / f"{generator_id}.json").write_text(
        json.dumps(record.to_dict())
    )

    # The Registry wants charter.yaml on disk too for load_all to succeed.
    # Lay down a minimal one matching the fingerprint stored above.
    code_dir = _ANALYZER_DIR / "generators" / generator_id
    if not code_dir.exists():
        # We can't write into the analyzer source tree, but Registry.load_all
        # is called with strict=False — missing charter just skips that gen.
        # The bump helper calls update_track_record(generator_id, ...) which
        # uses the in-memory registry; if the gen wasn't loaded, this raises.
        # Workaround: monkeypatch the loader. Instead, we'll use one of the
        # real generator IDs that already has a charter on disk.
        pass
    return shared


def test_bump_succeeded_first_shot_increments_first_shot_counter(tmp_path: Path):
    """A succeeded transition on a proposal with no revisions bumps
    first_shot, not after_iteration."""
    # Use a real generator id that has a charter shipped in code, so the
    # registry can load it. Pick one with no per-bot factory dependency.
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    # Read the existing charter to get the right fingerprint
    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    # Pick the first available generator
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    # Now bump
    proposal = make_investigation_proposal()
    proposal.generator_id = gid
    assert not proposal.revisions  # fresh, no iterations

    bump_for_status_transition(shared, proposal, to_status="succeeded")

    # Reload via a fresh registry to see the persisted change
    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.proposals_verified_success == 1
    assert tr.proposals_succeeded_first_shot == 1
    assert tr.proposals_succeeded_after_iteration == 0


def test_bump_succeeded_after_iteration_increments_after_counter(tmp_path: Path):
    """A succeeded transition on a proposal with revisions bumps
    after_iteration, not first_shot."""
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    proposal = make_investigation_proposal()
    proposal.generator_id = gid
    # Add a revision via apply_refinement
    apply_refinement(
        proposal,
        RefineResult(
            ok=True,
            new_problem="revised",
            new_admin_surface_summary="rev",
            new_action_context="ctx",
        ),
        feedback="be more concise",
        actor="user",
    )
    assert len(proposal.revisions) == 1

    bump_for_status_transition(shared, proposal, to_status="succeeded")

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.proposals_verified_success == 1
    assert tr.proposals_succeeded_first_shot == 0
    assert tr.proposals_succeeded_after_iteration == 1


def test_bump_failure_increments_failed_counter(tmp_path: Path):
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    proposal = make_investigation_proposal()
    proposal.generator_id = gid

    bump_for_status_transition(shared, proposal, to_status="failed_flagged")

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.proposals_verified_failed == 1
    assert tr.proposals_verified_success == 0


def test_bump_dismiss_increments_rejected_human_counter(tmp_path: Path):
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    proposal = make_investigation_proposal()
    proposal.generator_id = gid

    bump_for_status_transition(shared, proposal, to_status="dismissed")

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.proposals_rejected_human == 1


def test_bump_success_stamps_last_verification_at(tmp_path: Path):
    """Successful verification stamps the timestamp so the UI's
    "Last verif." column reflects when this generator was last judged."""
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    proposal = make_investigation_proposal()
    proposal.generator_id = gid

    bump_for_status_transition(shared, proposal, to_status="succeeded")

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.last_verification_at is not None
    assert tr.last_verification_at.endswith("+00:00")


def test_bump_failure_stamps_last_verification_at(tmp_path: Path):
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    proposal = make_investigation_proposal()
    proposal.generator_id = gid

    bump_for_status_transition(shared, proposal, to_status="failed_flagged")

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.last_verification_at is not None


def test_bump_human_rejection_does_not_stamp_last_verification_at(tmp_path: Path):
    """Human rejection is not a verification outcome — the field should
    stay None so "Last verif." means "last time the verify daemon judged"."""
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    loaded = next(iter(reg.all_loaded().values()))
    gid = loaded.charter.id

    proposal = make_investigation_proposal()
    proposal.generator_id = gid

    bump_for_status_transition(shared, proposal, to_status="dismissed")

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    tr = reg2.get(gid).record.track_record
    assert tr.proposals_rejected_human == 1
    assert tr.last_verification_at is None


def test_bump_unknown_generator_id_does_not_raise(tmp_path: Path):
    """Bumping for a generator that isn't in the registry must be silent —
    proposal lifecycle takes precedence over bookkeeping."""
    proposal = make_investigation_proposal()
    proposal.generator_id = "no_such_generator_xyzzy"
    # Should not raise
    bump_for_status_transition(tmp_path, proposal, to_status="succeeded")


# ──────────────────────────────────────────────────────────────────────────
# bump_proposals_emitted — Phase 6c promotion-path bump
# ──────────────────────────────────────────────────────────────────────────


def test_bump_proposals_emitted_increments_counter(tmp_path: Path):
    """Promotion path bump grows the source coach's emitted counter."""
    shared = tmp_path
    (shared / "generators").mkdir(parents=True, exist_ok=True)

    from registry.registry import Registry

    code_dir = _ANALYZER_DIR / "generators"
    reg = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg.load_all(strict=False)
    gid = next(iter(reg.all_loaded().values())).charter.id

    bump_proposals_emitted(shared, gid)
    bump_proposals_emitted(shared, gid)

    reg2 = Registry(generators_code_dir=code_dir, records_dir=shared / "generators")
    reg2.load_all(strict=False)
    assert reg2.get(gid).record.track_record.proposals_emitted == 2


def test_bump_proposals_emitted_unknown_generator_does_not_raise(tmp_path: Path):
    """Unknown generator_id is logged and swallowed — the candidate promotion
    must succeed regardless."""
    bump_proposals_emitted(tmp_path, "no_such_generator_xyzzy")


# ──────────────────────────────────────────────────────────────────────────
# TrackRecord schema roundtrip
# ──────────────────────────────────────────────────────────────────────────


def test_track_record_new_fields_roundtrip():
    tr = TrackRecord(
        proposals_verified_success=10,
        proposals_succeeded_first_shot=7,
        proposals_succeeded_after_iteration=3,
    )
    blob = tr.to_dict()
    tr2 = TrackRecord.from_dict(blob)
    assert tr2.proposals_succeeded_first_shot == 7
    assert tr2.proposals_succeeded_after_iteration == 3
    assert tr2.proposals_verified_success == 10


def test_track_record_old_blobs_default_iteration_fields_to_zero():
    """A pre-step5 record loaded from disk gets zeros for the new fields,
    not crashes."""
    legacy_blob = {
        "proposals_emitted": 5,
        "proposals_verified_success": 3,
        # No iteration fields
    }
    tr = TrackRecord.from_dict(legacy_blob)
    assert tr.proposals_succeeded_first_shot == 0
    assert tr.proposals_succeeded_after_iteration == 0
    assert tr.proposals_verified_success == 3
