"""ForgeRunawayWatcher — Guard B's sidecar polls trajectory.jsonl and
kills the dispatch when ``model.completed`` count crosses the per-bot
``message_cap``.

These tests exercise the watcher in isolation against a fake trajectory
file that the test writes to. The kill callback is a list-appending
stub. No subprocesses, no openclaw, no signals — those edges are wired
in ``bot_forge._dispatch_agent`` and covered by integration tests.

Background: PR #2036 wired Guard B as ``openclaw agent --max-turns N``;
OC 2026.6.1 doesn't recognise the flag (PR #2197 reverted). The watcher
enforces the cap from outside the process by reading the trajectory log
OC writes during the run. See ``forge_runaway_watcher.py``.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications.forge_runaway_watcher import (  # noqa: E402
    ForgeRunawayWatcher,
)

# The whole module drives a real background polling thread and uses real
# sub-second sleeps to let it observe appended lines. Opt the entire file out
# of conftest's sleep cap — capped sleeps would race the watcher thread.
pytestmark = pytest.mark.real_sleep


# Real OC trajectory event lines (sampled from atlas's session log,
# 2026-06-05). Each is one JSONL record — type is the discriminator we
# count on (``model.completed``). The other types should NOT be counted
# even though they share schema scaffolding with model.completed.
_LINE_SESSION_STARTED = (
    '{"traceSchema":"openclaw-trajectory","schemaVersion":1,'
    '"type":"session.started","ts":"2026-06-05T20:46:53.766Z",'
    '"sessionId":"b475ccf4","data":{"trigger":"forge"}}'
)
_LINE_PROMPT_SUBMITTED = (
    '{"traceSchema":"openclaw-trajectory","schemaVersion":1,'
    '"type":"prompt.submitted","ts":"2026-06-05T20:46:54.000Z",'
    '"sessionId":"b475ccf4"}'
)
_LINE_MODEL_COMPLETED = (
    '{"traceSchema":"openclaw-trajectory","schemaVersion":1,'
    '"type":"model.completed","ts":"2026-06-05T20:46:55.000Z",'
    '"sessionId":"b475ccf4","data":{"durationMs":1234}}'
)
_LINE_TRACE_METADATA = (
    '{"traceSchema":"openclaw-trajectory","schemaVersion":1,'
    '"type":"trace.metadata","ts":"2026-06-05T20:46:53.778Z",'
    '"sessionId":"b475ccf4"}'
)


def _make_kill_recorder():
    """Returns (callback, calls_list). The callback is the kill_pg the
    watcher invokes when the cap is breached; calls_list records every
    invocation as () tuples so tests can count how often it fired."""
    calls: list[tuple] = []

    def _cb():
        calls.append(())

    return _cb, calls


def _append_lines(path: Path, *lines: str) -> None:
    """Append the given JSONL lines (one per arg) to ``path``. Each
    line gets its own ``\\n`` terminator — mirrors how OC's writer
    flushes records."""
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
            fh.flush()


# ── Verdict matrix ───────────────────────────────────────────────────────────


def test_watcher_fires_kill_when_cap_exceeded(tmp_path):
    """Trajectory file grows past ``message_cap`` model.completed lines;
    watcher invokes the kill callback exactly once and ``verdict()``
    returns ``cap_exceeded``."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")  # empty file exists from the start

    kill_cb, kill_calls = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=3,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # Write enough model.completed lines to trip the cap. Interleave a
    # few non-model.completed lines to confirm only the right type is
    # counted.
    _append_lines(
        traj,
        _LINE_SESSION_STARTED,
        _LINE_TRACE_METADATA,
        _LINE_PROMPT_SUBMITTED,
        _LINE_MODEL_COMPLETED,
        _LINE_MODEL_COMPLETED,
        _LINE_MODEL_COMPLETED,
    )

    w.join(timeout=2.0)
    assert w.verdict() == "cap_exceeded"
    assert w.observed_count() == 3
    assert len(kill_calls) == 1, (
        f"kill callback must fire exactly once, fired {len(kill_calls)}"
    )


def test_watcher_records_stopped_when_under_cap(tmp_path):
    """Trajectory stays under cap; watcher is stopped externally
    (mirrors the happy-path dispatch where outbox arrives before the
    cap fires). Verdict is ``stopped``; kill callback never invoked."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, kill_calls = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=10,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # Two model.completed events: under cap.
    _append_lines(
        traj, _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
    )
    # Give the watcher a tick to read.
    time.sleep(0.1)

    w.stop()
    w.join(timeout=2.0)

    assert w.verdict() == "stopped"
    assert w.observed_count() == 2
    assert kill_calls == []


def test_watcher_records_never_ran_when_cap_nonpositive(tmp_path):
    """``message_cap=0`` (operator opt-out) disables the watcher
    entirely: ``start()`` is a no-op, no thread is spawned, verdict
    stays ``never_ran``."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, kill_calls = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=0,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # Write would-be-runaway content; watcher should ignore it.
    _append_lines(
        traj,
        _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
        _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
    )
    time.sleep(0.1)
    w.stop()
    w.join(timeout=2.0)

    assert w.verdict() == "never_ran"
    assert w.observed_count() == 0
    assert kill_calls == []


def test_watcher_tolerates_missing_file(tmp_path):
    """Trajectory file does not exist for the first ~tick — common in
    production because OC takes 100-500ms to write session.started.
    Watcher must NOT crash; it should poll until the file appears."""
    traj = tmp_path / "not-yet.trajectory.jsonl"
    # Deliberately do NOT create the file.

    kill_cb, kill_calls = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=2,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # Let the watcher poll a few times with the file absent.
    time.sleep(0.1)
    assert w.verdict() == "never_ran"  # nothing observed yet
    assert kill_calls == []

    # Now create the file and write past the cap.
    traj.write_text("")
    _append_lines(
        traj, _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
    )
    w.join(timeout=2.0)

    assert w.verdict() == "cap_exceeded"
    assert w.observed_count() == 2
    assert len(kill_calls) == 1


# ── Substring counting correctness ───────────────────────────────────────────


def test_watcher_only_counts_model_completed_events(tmp_path):
    """``model.completed`` is the only event type the watcher
    increments on. Other types in the trajectory schema
    (session.started, prompt.submitted, trace.metadata, etc.) must NOT
    contribute to ``observed_count``."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, _ = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=100,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # Many non-counting lines plus exactly 4 model.completed.
    _append_lines(
        traj,
        _LINE_SESSION_STARTED,
        _LINE_TRACE_METADATA,
        _LINE_PROMPT_SUBMITTED,
        _LINE_MODEL_COMPLETED,
        _LINE_TRACE_METADATA,
        _LINE_PROMPT_SUBMITTED,
        _LINE_MODEL_COMPLETED,
        _LINE_PROMPT_SUBMITTED,
        _LINE_MODEL_COMPLETED,
        _LINE_SESSION_STARTED,
        _LINE_MODEL_COMPLETED,
    )
    time.sleep(0.1)
    w.stop()
    w.join(timeout=2.0)

    assert w.observed_count() == 4
    assert w.verdict() == "stopped"


# ── Tail semantics ───────────────────────────────────────────────────────────


def test_watcher_tail_counts_incrementally(tmp_path):
    """The watcher remembers its byte offset between ticks. Lines
    appended after an earlier read are counted exactly once, never
    re-counted from the start of the file. This is the property that
    keeps poll cost O(new bytes), not O(total bytes), on long
    dispatches."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, kill_calls = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=5,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # First batch: 2 hits.
    _append_lines(traj, _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED)
    time.sleep(0.08)
    assert w.observed_count() == 2

    # Second batch: 2 more, total 4. Still under cap.
    _append_lines(traj, _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED)
    time.sleep(0.08)
    assert w.observed_count() == 4
    assert kill_calls == []

    # Third batch crosses cap.
    _append_lines(traj, _LINE_MODEL_COMPLETED)
    w.join(timeout=2.0)
    assert w.observed_count() == 5
    assert w.verdict() == "cap_exceeded"
    assert len(kill_calls) == 1


def test_watcher_final_tick_counts_writes_arriving_during_stop_window(tmp_path):
    """Regression for the fast-writer + immediate-stop race: a producer
    can write many ``model.completed`` lines AFTER the last tick but
    BEFORE the external ``stop()`` arrives. The watcher must do one
    final tick on the stop path so the file's true byte count is
    observed before the verdict is committed.

    Without the final-tick fix, a runaway that completed in
    <poll_interval seconds and then died would record
    ``verdict=stopped`` even when the on-disk trajectory clearly
    exceeded the cap — the dispatcher would then misattribute the
    death to "agent exited without outbox" rather than the runaway.
    """
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, kill_calls = _make_kill_recorder()
    # Long poll interval — the test forces the race by appending lines
    # AFTER start() but BEFORE the watcher's second tick would fire.
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=4,
        kill_pg=kill_cb,
        poll_interval=10.0,  # long enough that stop() arrives first
    )
    w.start()

    # Tiny delay so the watcher's FIRST tick has already run (it ticks
    # immediately upon entering the loop) and is now sitting in the
    # 10-second wait window. Observed should be 0.
    time.sleep(0.05)
    assert w.observed_count() == 0

    # Now append 6 model.completed lines — all happen during the wait.
    _append_lines(
        traj,
        _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
        _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
        _LINE_MODEL_COMPLETED, _LINE_MODEL_COMPLETED,
    )

    # Signal stop. The watcher must do one final tick before exiting
    # and see all 6 lines, exceeding the cap of 4.
    w.stop()
    w.join(timeout=2.0)

    assert w.observed_count() == 6
    assert w.verdict() == "cap_exceeded"
    assert len(kill_calls) == 1


def test_watcher_tail_handles_partial_line_at_boundary(tmp_path):
    """A line that straddles a flush boundary — first half visible on
    tick N, second half visible on tick N+1 — must be counted exactly
    once when the full line is complete, not twice (once per partial
    read) and not zero times. This is the carry-buffer invariant."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, kill_calls = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=3,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.start()

    # Write half of a model.completed line, no newline.
    half_a = _LINE_MODEL_COMPLETED[: len(_LINE_MODEL_COMPLETED) // 2]
    half_b = _LINE_MODEL_COMPLETED[len(_LINE_MODEL_COMPLETED) // 2:]
    with open(traj, "a", encoding="utf-8") as fh:
        fh.write(half_a)
        fh.flush()
    time.sleep(0.08)
    # Cannot count yet — line incomplete.
    assert w.observed_count() == 0

    # Finish the line + terminator.
    with open(traj, "a", encoding="utf-8") as fh:
        fh.write(half_b + "\n")
        fh.flush()
    time.sleep(0.08)
    assert w.observed_count() == 1

    w.stop()
    w.join(timeout=2.0)
    assert kill_calls == []  # under cap


# ── Lifecycle robustness ─────────────────────────────────────────────────────


def test_watcher_stop_before_start_is_safe(tmp_path):
    """Calling ``stop()`` and ``join()`` on a watcher that was never
    started is a no-op — defends the dispatcher's ``finally`` block
    against partial-construction paths."""
    traj = tmp_path / "x.trajectory.jsonl"
    kill_cb, _ = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=5,
        kill_pg=kill_cb,
        poll_interval=0.02,
    )
    w.stop()
    w.join(timeout=0.5)
    assert w.verdict() == "never_ran"


def test_watcher_kill_callback_failure_does_not_propagate(tmp_path):
    """A throwing kill callback must NOT escape the worker thread —
    Guard B's responsibility is to flag the runaway; the dispatcher's
    Signal emission and OC's own --timeout are the safety nets when
    the kill itself fails. The watcher logs and moves on."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    def _boom():
        raise RuntimeError("sudo grant missing")

    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=1,
        kill_pg=_boom,
        poll_interval=0.02,
    )
    w.start()
    _append_lines(traj, _LINE_MODEL_COMPLETED)
    w.join(timeout=2.0)

    # Verdict still records the cap hit so the dispatcher emits its
    # Signal. The kill error is swallowed.
    assert w.verdict() == "cap_exceeded"
    assert w.observed_count() == 1


def test_watcher_observed_count_thread_safe_under_concurrent_read(tmp_path):
    """``observed_count()`` and ``verdict()`` must return stable
    integers/strings even when the worker thread is mid-tick. Smoke
    test — we read these accessors concurrently with file growth and
    confirm no exceptions, no negative counts, no AttributeError."""
    traj = tmp_path / "session.trajectory.jsonl"
    traj.write_text("")

    kill_cb, _ = _make_kill_recorder()
    w = ForgeRunawayWatcher(
        trajectory_path=traj,
        message_cap=200,
        kill_pg=kill_cb,
        poll_interval=0.005,
    )
    w.start()

    stop_reader = threading.Event()
    errors: list[Exception] = []

    def _hammer():
        while not stop_reader.is_set():
            try:
                _ = w.observed_count()
                _ = w.verdict()
            except Exception as exc:
                errors.append(exc)

    reader = threading.Thread(target=_hammer)
    reader.start()
    try:
        for _ in range(20):
            _append_lines(traj, _LINE_MODEL_COMPLETED)
            time.sleep(0.01)
    finally:
        stop_reader.set()
        reader.join(timeout=1.0)
        w.stop()
        w.join(timeout=2.0)

    assert errors == []
    assert w.observed_count() == 20
