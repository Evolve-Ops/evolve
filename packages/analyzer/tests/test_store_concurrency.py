"""tests/test_store_concurrency.py — 7.1 Phase A proof artifacts.

Spec: internal/spec-state-store-and-deploy-resilience-2026-06-10.md §1.4
(roadmap Phase 7 row 7.1 — "a concurrent-observe() race test passes").

Real processes, not threads: the bug class is cross-process (admin-ui,
heal, audit, verify, subscriber, evo gateway all mutate the same store
dirs). Workers are plain ``subprocess`` interpreters running a small
inline script, synchronized through a filesystem barrier (every worker
touches a ready-file, then spins until the parent drops a go-file) —
deliberately NOT ``multiprocessing``: spawn re-imports the pytest test
module by name in the child, which is fragile across pytest import
modes / CI layouts (it broke on Linux CI while passing on macOS), and
file-barrier subprocesses have no pickling or module-name dependence
at all.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from signals import store as signals_store  # noqa: E402
from arbiter import store as arbiter_store  # noqa: E402
from arbiter.state_machine import transition as proposal_transition  # noqa: E402
from store_lock import StoreLockTimeout, locked  # noqa: E402
from testing.harness import make_investigation_proposal  # noqa: E402

# Generous bounds for 2-core CI runners: N python interpreters can take
# a few seconds each to start and import the stores.
WORKER_TIMEOUT = 120

SIGNATURE = "race_test:duplicate_observe:pod"

# Every worker script gets the same prelude: analyzer dir on sys.path,
# then ready/go barrier. argv: [script, analyzer_dir, work_dir, idx, ...]
# Setup and barrier are separate pieces so a test can inject per-worker
# state-loading BEFORE the worker reports ready — anything that must see
# pre-race store state has to run there, because once the barrier drops
# there is no ordering between workers and a late-scheduled worker would
# observe the winner's completed write instead of racing it.
_PRELUDE_SETUP = """\
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
work = Path(sys.argv[2])
idx = sys.argv[3]
"""

_PRELUDE_BARRIER = """\
(work / f"ready-{idx}").touch()
deadline = time.monotonic() + 60
while not (work / "go").exists():
    if time.monotonic() > deadline:
        raise SystemExit("barrier timed out")
    time.sleep(0.002)
"""

_PRELUDE = _PRELUDE_SETUP + _PRELUDE_BARRIER


def _run_barrier_workers(work_dir: Path, script_body: str, n: int,
                         extra_args: "list[list[str]] | None" = None,
                         setup_body: str = ""):
    """Start n workers running the prelude + script_body, release the
    barrier once all are ready, and return their CompletedProcess-like
    results (returncode, stdout, stderr). ``setup_body`` runs before the
    worker reports ready, i.e. before the racing section."""
    work_dir.mkdir(parents=True, exist_ok=True)
    script = _PRELUDE_SETUP + setup_body + _PRELUDE_BARRIER + script_body
    procs = []
    for i in range(n):
        args = [sys.executable, "-c", script,
                str(_ANALYZER_DIR), str(work_dir), str(i)]
        if extra_args:
            args += extra_args[i]
        procs.append(subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))

    # Barrier: wait for every worker, then release them simultaneously.
    deadline = time.monotonic() + WORKER_TIMEOUT
    while True:
        ready = sum(1 for i in range(n) if (work_dir / f"ready-{i}").exists())
        if ready == n:
            break
        if time.monotonic() > deadline:
            for p in procs:
                p.kill()
            outs = [p.communicate() for p in procs]
            raise AssertionError(
                f"only {ready}/{n} workers reached the barrier; "
                f"stderr: {[e for _, e in outs]}"
            )
        time.sleep(0.005)
    (work_dir / "go").touch()

    results = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            raise AssertionError(f"worker did not finish; stderr: {err}")
        results.append((p.returncode, out, err))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# observe(): the roadmap's proof artifact
# ─────────────────────────────────────────────────────────────────────────────

_OBSERVE_BODY = """\
from signals import store
store.observe(
    Path(sys.argv[4]),
    signature="race_test:duplicate_observe:pod",
    producer="race_test",
    type="duplicate_observe",
    flavor="activity",
    severity="warn",
    scope="pod",
    title="concurrency proof",
)
"""


def test_concurrent_observe_creates_one_signal_and_serializes_bumps(tmp_path):
    """8 processes observe the same signature simultaneously.

    Two claims, both load-bearing:
      - exactly ONE Signal exists afterward (find-or-create deduped
        under the lock instead of all racers missing and all creating);
      - observation_count == 8 (schema default 1 + 7 bumps): the
        read-modify-write bump branch serialized — no lost updates.
    """
    n = 8
    shared = tmp_path / "shared"
    results = _run_barrier_workers(
        tmp_path / "work", _OBSERVE_BODY, n,
        extra_args=[[str(shared)] for _ in range(n)],
    )
    assert [rc for rc, _, _ in results] == [0] * n, \
        f"worker stderr: {[e for _, _, e in results if e]}"

    active = list(signals_store.iter_signals(
        shared, subdirs=("firing", "snoozed")
    ))
    archived = list(signals_store.iter_signals(shared, subdirs=("archived",)))
    assert len(archived) == 0
    assert len(active) == 1, (
        f"expected exactly one Signal for one signature, found "
        f"{len(active)}: {[s.id for s in active]}"
    )
    assert active[0].observation_count == n


# ─────────────────────────────────────────────────────────────────────────────
# write_proposal(): concurrent coalesce
# ─────────────────────────────────────────────────────────────────────────────

_COALESCE_BODY = """\
from arbiter import store
from testing.harness import make_investigation_proposal
p = make_investigation_proposal(bot_id="race-bot", problem=f"finding {idx}")
# Distinct trigger per writer — identical/empty triggers are deliberately
# deduped as re-emissions by _coalesce_into_parent, which would mask the
# race this test exists to catch.
p.trigger_observations = [f"t-{idx}"]
p.coalesce_key = "race-key"
store.write_proposal(p, Path(sys.argv[4]))
"""


def test_concurrent_coalesce_produces_single_parent(tmp_path):
    """6 processes write proposals sharing one coalesce_key.

    Without the lock, several writers miss the pending-parent lookup
    simultaneously and each becomes a parent. With it: one parent,
    n-1 sub_findings, no sibling files.
    """
    n = 6
    shared = tmp_path / "shared"
    results = _run_barrier_workers(
        tmp_path / "work", _COALESCE_BODY, n,
        extra_args=[[str(shared)] for _ in range(n)],
    )
    assert [rc for rc, _, _ in results] == [0] * n, \
        f"worker stderr: {[e for _, _, e in results if e]}"

    pending = list(arbiter_store.iter_proposals(shared, subdirs=("pending",)))
    assert len(pending) == 1, (
        f"expected one coalesced parent, found {len(pending)}"
    )
    parent = pending[0]
    triggers = {sf["trigger_observation"] for sf in parent.sub_findings}
    assert len(parent.sub_findings) == n - 1
    assert parent.trigger_observations[0] not in triggers


# ─────────────────────────────────────────────────────────────────────────────
# apply_transition(): lost races are detected, not clobbered
# ─────────────────────────────────────────────────────────────────────────────

_TRANSITION_BODY = """\
from signals import store
from signals.state_machine import IllegalTransitionError
shared = Path(sys.argv[4])
signal_id = sys.argv[5]
to_state = sys.argv[6]
located = store.find_signal(shared, signal_id)
assert located is not None
sig, _path, _subdir = located
try:
    store.apply_transition(
        sig, to_state, shared, actor="race_test", reason="transition race",
    )
    print("ok")
except IllegalTransitionError:
    print("illegal")
"""


def test_transition_race_one_winner_one_detected_loser(tmp_path):
    """Two processes race firing→resolved vs firing→dismissed.

    apply_transition re-loads under the lock, so exactly one wins and
    the loser sees IllegalTransitionError from the fresh state instead
    of silently overwriting the winner's terminal state. (Pre-fix, both
    would 'succeed' and last-writer-wins erased the first transition.)
    """
    shared = tmp_path / "shared"
    sig = signals_store.observe(
        shared,
        signature=SIGNATURE, producer="race_test", type="duplicate_observe",
        flavor="activity", severity="warn", scope="pod", title="t",
    )
    results = _run_barrier_workers(
        tmp_path / "work", _TRANSITION_BODY, 2,
        extra_args=[[str(shared), sig.id, "resolved"],
                    [str(shared), sig.id, "dismissed"]],
    )
    assert [rc for rc, _, _ in results] == [0, 0], \
        f"worker stderr: {[e for _, _, e in results if e]}"
    outcomes = sorted(out.strip() for _, out, _ in results)
    assert outcomes == ["illegal", "ok"], \
        f"expected one winner + one detected loser, got {outcomes}"

    located = signals_store.find_signal(shared, sig.id)
    assert located is not None
    final, _path, subdir = located
    assert subdir == "archived"
    assert final.state in ("resolved", "dismissed")
    # created + exactly one transition — the loser never wrote.
    assert len(final.state_history) == 2


# ─────────────────────────────────────────────────────────────────────────────
# move_proposal(): no resurrection of an already-moved file
# ─────────────────────────────────────────────────────────────────────────────

# Load + in-memory transition happen pre-barrier: this test pins the
# load→transition→move sequence where both movers hold the same pre-move
# source state (the resurrection race). Loading *after* the barrier made
# the test flaky under full-suite xdist load: a late-scheduled worker
# observed the winner's completed move instead of racing it, and either
# died on a stale-state IllegalTransitionError (dismissed → snoozed) or
# legitimately succeeded sequentially (snoozed → dismissed is a legal
# edge), producing ["ok", "ok"]. Neither interleaving is the race under
# test. Stale-load detection has its own coverage in
# test_transition_race_one_winner_one_detected_loser.
_MOVE_SETUP = """\
from arbiter import store
from arbiter.state_machine import transition
shared = Path(sys.argv[4])
proposal_id = sys.argv[5]
to_status = sys.argv[6]
located = store.find_proposal(shared, proposal_id)
assert located is not None
prop, _path, src_subdir = located
assert src_subdir == "pending", src_subdir
if to_status == "snoozed":
    prop.snoozed_until = "2099-01-01T00:00:00+00:00"
transition(prop, to_status, actor="race_test", reason="move race")
"""

_MOVE_BODY = """\
try:
    store.move_proposal(prop, shared, from_subdir=src_subdir)
    print("ok")
except OSError as exc:
    # Pin the loss to the new source-vanished check (not some
    # incidental ENOENT shape the pre-fix code could also produce).
    assert "vanished" in str(exc), exc
    print("lost")
"""


def test_move_race_does_not_resurrect_into_two_subdirs(tmp_path):
    """Two processes race pending→snoozed vs pending→dismissed.

    Pre-fix, move_proposal staged its write at the *source* path, so
    the loser re-created the already-moved pending file and renamed it
    into a second subdir — the proposal existed in two places at once.
    With the lock + source-exists check: one mover wins, the loser
    raises, and exactly one file exists afterward.

    Both workers load the pending proposal before the barrier (see
    _MOVE_SETUP), so the racing section is move_proposal alone and the
    loser deterministically hits the vanished check.
    """
    shared = tmp_path / "shared"
    prop = make_investigation_proposal(bot_id="race-bot", problem="move race")
    proposal_transition(prop, "pending", actor="test", reason="seed")
    arbiter_store.write_proposal(prop, shared)

    results = _run_barrier_workers(
        tmp_path / "work", _MOVE_BODY, 2,
        extra_args=[[str(shared), prop.id, "snoozed"],
                    [str(shared), prop.id, "dismissed"]],
        setup_body=_MOVE_SETUP,
    )
    assert [rc for rc, _, _ in results] == [0, 0], \
        f"worker stderr: {[e for _, _, e in results if e]}"
    outcomes = sorted(out.strip() for _, out, _ in results)
    assert outcomes == ["lost", "ok"], \
        f"expected one winner + one loser, got {outcomes}"

    locations = [
        sd for sd in ("pending", "snoozed", "applied", "archived")
        if arbiter_store.proposal_path(shared, prop.id, subdir=sd).exists()
    ]
    assert len(locations) == 1, (
        f"proposal resurrected into multiple subdirs: {locations}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lock primitive
# ─────────────────────────────────────────────────────────────────────────────

_HOLDER_BODY = """\
from store_lock import locked
lock_file = Path(sys.argv[4])
release = work / "release"
with locked(lock_file):
    (work / "held").touch()
    deadline = time.monotonic() + 60
    while not release.exists():
        if time.monotonic() > deadline:
            raise SystemExit("release wait timed out")
        time.sleep(0.002)
"""


def test_lock_is_reentrant_within_a_thread(tmp_path):
    lock_file = tmp_path / ".store.lock"
    with locked(lock_file):
        with locked(lock_file):   # must not deadlock or raise
            with locked(lock_file):
                pass


def test_lock_times_out_against_a_foreign_holder(tmp_path):
    lock_file = tmp_path / ".store.lock"
    work = tmp_path / "work"
    work.mkdir()
    script = _PRELUDE + _HOLDER_BODY
    p = subprocess.Popen(
        [sys.executable, "-c", script,
         str(_ANALYZER_DIR), str(work), "0", str(lock_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        (work / "go").touch()   # holder doesn't need the group barrier
        deadline = time.monotonic() + WORKER_TIMEOUT
        while not (work / "held").exists():
            assert p.poll() is None, f"holder died: {p.communicate()[1]}"
            assert time.monotonic() < deadline, "holder never acquired the lock"
            time.sleep(0.005)
        with pytest.raises(StoreLockTimeout):
            with locked(lock_file, timeout=0.5):
                pass  # pragma: no cover
    finally:
        (work / "release").touch()
        try:
            _, err = p.communicate(timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:  # pragma: no cover
            p.kill()
            _, err = p.communicate()
    assert p.returncode == 0, f"holder stderr: {err}"


def test_lock_released_on_exit(tmp_path):
    lock_file = tmp_path / ".store.lock"
    with locked(lock_file):
        pass
    # Immediately reacquirable — held fd was closed and unlocked.
    with locked(lock_file, timeout=0.5):
        pass
