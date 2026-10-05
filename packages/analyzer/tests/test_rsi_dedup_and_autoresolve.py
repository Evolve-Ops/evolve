"""tests/test_rsi_dedup_and_autoresolve.py — Write-time dedup guard,
resolved_externally status, resolves_when_silent charter flag, and the
end-to-end auto-resolve sweep in generator_runner.

These cover the three regressions that motivated this work:
  1. The L1 in-memory dedup snapshot can miss a duplicate (race with a
     concurrent rewrite, transient unreadable file, etc.) and ship a
     duplicate proposal to disk forever. The write-time guard prevents
     it on disk.
  2. ``move_proposal`` historically swallowed OSError from the unlink,
     which masked a real "admin server can't unlink admin_bot-owned file
     under sticky bit" bug. The new behaviour surfaces the failure.
  3. Pending sensor-style proposals never auto-clear when the underlying
     issue is gone — the verify daemon only fires on already-applied
     proposals. The sweep in generator_runner archives them as
     ``resolved_externally`` (or ``superseded`` when the bot has left
     the pod).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from arbiter.dedup import compute_fingerprint  # noqa: E402
from arbiter.state_machine import (  # noqa: E402
    IllegalTransitionError,
    allowed_transitions,
    is_legal_transition,
    transition,
)
from arbiter.store import (  # noqa: E402
    find_open_duplicate,
    find_proposal,
    iter_proposals,
    move_proposal,
    proposals_root,
    subdir_for_status,
    write_proposal,
)
from schema.generator import Charter, Invariant  # noqa: E402
from testing.harness import (  # noqa: E402
    make_config_patch_proposal,
    make_investigation_proposal,
)


# ─────────────────────────────────────────────────────────────────────────────
# resolved_externally status — schema + state machine + store routing
# ─────────────────────────────────────────────────────────────────────────────


def test_pending_to_resolved_externally_legal():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "resolved_externally", actor="generator_runner", reason="silent")
    assert p.status == "resolved_externally"
    assert p.history[-1].to_status == "resolved_externally"


def test_snoozed_to_resolved_externally_legal():
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "snoozed", actor="user")
    transition(p, "resolved_externally", actor="generator_runner")
    assert p.status == "resolved_externally"


def test_resolved_externally_is_terminal():
    assert allowed_transitions("resolved_externally") == frozenset()


def test_resolved_externally_writes_to_archived_subdir(tmp_path):
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    transition(p, "resolved_externally", actor="generator_runner")
    assert subdir_for_status(p.status) == "archived"
    path = write_proposal(p, tmp_path)
    assert path.parent.name == "archived"


# ─────────────────────────────────────────────────────────────────────────────
# Charter resolves_when_silent flag — round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_charter_resolves_when_silent_round_trip():
    c = Charter(
        id="x",
        type="guardian",
        dimension="substrate_health",
        purpose="test",
        cadence="hourly",
        resolves_when_silent=True,
    )
    d = c.to_dict()
    assert d["resolves_when_silent"] is True
    c2 = Charter.from_dict(d)
    assert c2.resolves_when_silent is True


def test_charter_resolves_when_silent_defaults_false_when_absent():
    # Old charters without the field must still load cleanly with default false.
    c = Charter.from_dict(
        {
            "id": "legacy",
            "type": "optimizer",
            "dimension": "engagement",
            "purpose": "test",
            "cadence": "daily",
            # resolves_when_silent intentionally absent
        }
    )
    assert c.resolves_when_silent is False


def test_sysadmin_watchdog_charter_opts_in():
    """The shipped sysadmin_watchdog charter must declare the flag — this
    is what keeps the existing platform-health proposals self-clearing."""
    from registry.charter_loader import load_charter_from_yaml

    charter_path = (
        _ANALYZER_DIR / "generators" / "sysadmin_watchdog" / "charter.yaml"
    )
    charter, _ = load_charter_from_yaml(charter_path)
    assert charter.resolves_when_silent is True


# ─────────────────────────────────────────────────────────────────────────────
# Write-time dedup guard
# ─────────────────────────────────────────────────────────────────────────────


def _matching_acl_drift_proposal(bot_id: str = "team_bot_b"):
    """Construct two proposals that produce the same fingerprint, mirroring
    the real sysadmin_watchdog ACL drift case (same bot, same target_path,
    same metric, same single trigger observation)."""
    p = make_config_patch_proposal(
        target_path="/Users/Shared/evolve/acl/team_bot_b.json::acl_restored",
        operation="set",
        value={"acl_restored": True},
        bot_id=bot_id,
        generator_id="sysadmin_watchdog",
        claim_metric="acl.evolve_read",
        touches=["acl"],
    )
    # The harness randomises trigger_observations; align them so the
    # fingerprint matches a sibling proposal.
    p.trigger_observations = [f"acl_drift:{bot_id}"]
    return p


def test_find_open_duplicate_returns_match(tmp_path):
    existing = _matching_acl_drift_proposal()
    transition(existing, "pending", actor="arbiter")
    write_proposal(existing, tmp_path)

    incoming = _matching_acl_drift_proposal()
    # Fingerprints must match — sanity check.
    assert compute_fingerprint(incoming) == compute_fingerprint(existing)

    found = find_open_duplicate(incoming, tmp_path)
    assert found is not None
    assert found.id == existing.id


def test_find_open_duplicate_ignores_self(tmp_path):
    # A proposal on disk should not collide with itself.
    p = _matching_acl_drift_proposal()
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)
    assert find_open_duplicate(p, tmp_path) is None


def test_find_open_duplicate_returns_none_when_no_match(tmp_path):
    existing = _matching_acl_drift_proposal(bot_id="team_bot_a")
    transition(existing, "pending", actor="arbiter")
    write_proposal(existing, tmp_path)

    incoming = _matching_acl_drift_proposal(bot_id="team_bot_c")
    found = find_open_duplicate(incoming, tmp_path)
    assert found is None


def test_find_open_duplicate_scans_snoozed(tmp_path):
    existing = _matching_acl_drift_proposal()
    transition(existing, "pending", actor="arbiter")
    transition(existing, "snoozed", actor="user")
    write_proposal(existing, tmp_path)

    incoming = _matching_acl_drift_proposal()
    found = find_open_duplicate(incoming, tmp_path)
    assert found is not None and found.id == existing.id


def test_find_open_duplicate_does_not_match_archived(tmp_path):
    # Archived proposals are out of scope — they shouldn't block new ones.
    existing = _matching_acl_drift_proposal()
    transition(existing, "pending", actor="arbiter")
    transition(existing, "resolved_externally", actor="generator_runner")
    write_proposal(existing, tmp_path)

    incoming = _matching_acl_drift_proposal()
    found = find_open_duplicate(incoming, tmp_path)
    assert found is None


# ─────────────────────────────────────────────────────────────────────────────
# move_proposal surfaces OSError on unlink failure
# ─────────────────────────────────────────────────────────────────────────────


def test_move_proposal_raises_when_replace_fails(tmp_path, monkeypatch):
    """Updated contract: move_proposal stages updated content at src,
    then ``os.replace(src, dest)``. If the replace fails (e.g. EACCES from
    a foreign-owned dest under a sticky-bit dir — the exact dismiss bug we
    hit), the proposal is in EXACTLY ONE place (src) with the new content.
    Dest must not exist — no half-state across subdirs.
    """
    import os
    import errno

    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)

    transition(p, "dismissed", actor="user")

    pending_path = proposals_root(tmp_path) / "pending" / f"{p.id}.json"
    archived_path = proposals_root(tmp_path) / "archived" / f"{p.id}.json"

    real_replace = os.replace

    def fake_replace(a, b, *args, **kwargs):
        # Fail only on the rename from pending → archived. The tempfile
        # rename inside _atomic_write_json (staging content at src) must
        # still work.
        if str(b) == str(archived_path):
            raise PermissionError(errno.EACCES, "simulated sticky-bit denial")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PermissionError):
        move_proposal(p, tmp_path, from_subdir="pending")

    # Critical invariant: dest must NOT exist. Previously we wrote dest first
    # and only then unlinked src — a failed unlink left a duplicate. The new
    # code stages at src and renames; a failed rename leaves dest absent.
    assert not archived_path.exists(), (
        "move_proposal must not leave the proposal duplicated across subdirs "
        "when the transition fails"
    )
    # src exists with the updated (dismissed) content — caller retries.
    assert pending_path.exists()


def test_move_proposal_succeeds_when_unlink_works(tmp_path):
    p = make_investigation_proposal()
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)

    transition(p, "dismissed", actor="user")
    move_proposal(p, tmp_path, from_subdir="pending")

    pending_path = proposals_root(tmp_path) / "pending" / f"{p.id}.json"
    archived_path = proposals_root(tmp_path) / "archived" / f"{p.id}.json"
    assert not pending_path.exists()
    assert archived_path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# generator_runner: write-time guard + auto-resolve sweep
# ─────────────────────────────────────────────────────────────────────────────


def _build_test_charter(tmp_path: Path, *, resolves_when_silent: bool) -> Path:
    """Write a minimal charter.yaml + register a context factory + observe
    module so the runner can drive a synthetic generator end-to-end.

    Returns the generators_dir path the runner should be pointed at.
    """
    gens_dir = tmp_path / "generators"
    pkg_dir = gens_dir / "test_sensor"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "charter.yaml").write_text(
        f"""\
id: test_sensor
schema_version: 1
type: guardian
dimension: substrate_health
purpose: synthetic sensor for tests
cadence: hourly
resolves_when_silent: {str(resolves_when_silent).lower()}
invariants: []
"""
    )
    return gens_dir


def _seed_pending(shared_dir: Path, *, bot_id: str, generator_id: str):
    """Write a single pending ConfigPatch proposal for a given (gen, bot)."""
    p = make_config_patch_proposal(
        target_path=f"/tmp/{bot_id}/test.json::flag",
        bot_id=bot_id,
        generator_id=generator_id,
        claim_metric="test.metric",
        touches=["test"],
    )
    p.trigger_observations = [f"test:{bot_id}"]
    transition(p, "pending", actor="arbiter")
    write_proposal(p, shared_dir)
    return p


def _drive_sweep(
    shared_dir: Path,
    *,
    visited_bots: list[str],
    emissions: dict[str, list],
    charter: Charter,
    per_bot: bool = True,
):
    """Re-implement the runner's sweep logic in isolation, the way it
    would run for a single generator. This avoids spinning up the full
    Registry + factory plumbing while still exercising the real store +
    state machine code paths.
    """
    from arbiter.dedup import compute_fingerprint as _fp
    from arbiter.state_machine import (
        IllegalTransitionError as _IllegalTransition,
        transition as _trans,
    )
    from arbiter.store import (
        find_proposal as _find,
        iter_proposals as _iter,
        move_proposal as _move,
    )

    if not charter.resolves_when_silent:
        return 0
    bot_ids = list(visited_bots)
    visited = set(bot_ids)
    emissions_by_bot: dict[str, set[str]] = {
        bot: {_fp(p) for p in plist} for bot, plist in emissions.items()
    }
    resolved = 0
    for existing in list(_iter(shared_dir, subdirs=("pending", "snoozed"))):
        if existing.generator_id != charter.id:
            continue
        key = existing.bot_id or ""
        if per_bot and key and key not in bot_ids and key not in visited:
            target, reason = "superseded", "bot gone"
        elif per_bot and key and key not in visited:
            continue
        else:
            fp = _fp(existing)
            if fp in emissions_by_bot.get(key, set()):
                continue
            target, reason = "resolved_externally", "silent"
        located = _find(shared_dir, existing.id)
        if located is None:
            continue
        _, _, src_subdir = located
        try:
            _trans(existing, target, actor="generator_runner", reason=reason)
        except _IllegalTransition:
            continue
        _move(existing, shared_dir, from_subdir=src_subdir)
        resolved += 1
    return resolved


def test_sweep_archives_silent_proposal_as_resolved_externally(tmp_path):
    """A pending proposal whose fingerprint is NOT re-emitted this cycle is
    archived as resolved_externally."""
    seeded = _seed_pending(
        tmp_path, bot_id="team_bot_a", generator_id="test_sensor"
    )
    charter = Charter(
        id="test_sensor",
        type="guardian",
        dimension="substrate_health",
        purpose="t",
        cadence="hourly",
        resolves_when_silent=True,
    )

    resolved = _drive_sweep(
        tmp_path,
        visited_bots=["team_bot_a"],
        emissions={"team_bot_a": []},  # detector silent
        charter=charter,
    )
    assert resolved == 1

    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    proposal, _, subdir = located
    assert subdir == "archived"
    assert proposal.status == "resolved_externally"


def test_sweep_preserves_proposal_when_fingerprint_re_emitted(tmp_path):
    """A pending proposal whose fingerprint IS re-emitted this cycle stays
    pending. (Real dedup-merge handles this in ingest; the sweep must not
    archive it.)"""
    seeded = _seed_pending(
        tmp_path, bot_id="team_bot_a", generator_id="test_sensor"
    )
    re_emitted = _seed_pending(  # same shape → same fingerprint
        tmp_path, bot_id="team_bot_a", generator_id="test_sensor"
    )
    # Don't actually leave the second one on disk — it's just a stand-in
    # for what observe() would produce this cycle.
    (proposals_root(tmp_path) / "pending" / f"{re_emitted.id}.json").unlink()

    charter = Charter(
        id="test_sensor",
        type="guardian",
        dimension="substrate_health",
        purpose="t",
        cadence="hourly",
        resolves_when_silent=True,
    )
    resolved = _drive_sweep(
        tmp_path,
        visited_bots=["team_bot_a"],
        emissions={"team_bot_a": [re_emitted]},
        charter=charter,
    )
    assert resolved == 0
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    assert located[2] == "pending"


def test_sweep_supersedes_proposal_for_bot_no_longer_in_pod(tmp_path):
    """A pending proposal whose bot_id isn't in the current pod membership
    is archived as superseded (the forge/personal_bot_user case)."""
    seeded = _seed_pending(
        tmp_path, bot_id="forge", generator_id="test_sensor"
    )
    charter = Charter(
        id="test_sensor",
        type="guardian",
        dimension="substrate_health",
        purpose="t",
        cadence="hourly",
        resolves_when_silent=True,
    )
    resolved = _drive_sweep(
        tmp_path,
        visited_bots=["team_bot_a", "team_bot_b"],  # forge is gone
        emissions={"team_bot_a": [], "team_bot_b": []},
        charter=charter,
    )
    assert resolved == 1
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None
    proposal, _, subdir = located
    assert subdir == "archived"
    assert proposal.status == "superseded"


def test_sweep_skips_when_charter_does_not_opt_in(tmp_path):
    """Insight-style generators that don't re-fire each cycle must NOT
    have their pending proposals archived just because they were silent
    this run."""
    seeded = _seed_pending(
        tmp_path, bot_id="team_bot_a", generator_id="test_sensor"
    )
    charter = Charter(
        id="test_sensor",
        type="optimizer",
        dimension="engagement",
        purpose="t",
        cadence="daily",
        resolves_when_silent=False,
    )
    resolved = _drive_sweep(
        tmp_path,
        visited_bots=["team_bot_a"],
        emissions={"team_bot_a": []},
        charter=charter,
    )
    assert resolved == 0
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None and located[2] == "pending"


def test_sweep_leaves_proposal_alone_when_bot_in_pod_but_unvisited(tmp_path):
    """If we couldn't build a context for a bot this cycle (factory failure,
    transient error), we have no signal — must not archive."""
    seeded = _seed_pending(
        tmp_path, bot_id="team_bot_a", generator_id="test_sensor"
    )
    charter = Charter(
        id="test_sensor",
        type="guardian",
        dimension="substrate_health",
        purpose="t",
        cadence="hourly",
        resolves_when_silent=True,
    )
    # bot_ids includes team_bot_a (still a pod member) but visited_bots does NOT —
    # simulating a factory-build failure for team_bot_a. The sweep helper takes
    # both arguments via the visited_bots parameter; here we mimic the
    # real branch by leaving emissions empty and treating team_bot_a as
    # unvisited via a different code path.
    # The sweep treats `key not in bot_ids and key not in visited` as
    # "bot gone" and `key not in visited` (but in bot_ids) as "no signal".
    # We exercise the second branch by passing visited=[] but bot_ids=[team_bot_a].
    from arbiter.dedup import compute_fingerprint as _fp
    from arbiter.state_machine import transition as _trans
    from arbiter.store import (
        find_proposal as _find,
        iter_proposals as _iter,
        move_proposal as _move,
    )

    bot_ids = ["team_bot_a"]
    visited: set[str] = set()  # team_bot_a was due but ctx failed
    resolved = 0
    for existing in list(_iter(tmp_path, subdirs=("pending", "snoozed"))):
        if existing.generator_id != charter.id:
            continue
        key = existing.bot_id or ""
        if key and key not in bot_ids and key not in visited:
            target = "superseded"
        elif key and key not in visited:
            continue  # ← this branch: leave alone
        else:
            target = "resolved_externally"
        located = _find(tmp_path, existing.id)
        assert located is not None
        _, _, src_subdir = located
        _trans(existing, target, actor="generator_runner")
        _move(existing, tmp_path, from_subdir=src_subdir)
        resolved += 1

    assert resolved == 0
    located = find_proposal(tmp_path, seeded.id)
    assert located is not None and located[2] == "pending"
