"""
forge_runaway_watcher.py — Guard B for forge dispatches.

A short-lived background thread that polls one forge dispatch's
``trajectory.jsonl`` file, counts how many ``model.completed`` events
OpenClaw has emitted, and kills the dispatch process group when the
count crosses the per-bot ``forge.message_cap`` ceiling.

The 2026-06-03 incident — 312 turns accumulated in a single
``openclaw agent`` invocation, the final one rewriting 8.8M cache_write
tokens for $33.65 — is the canonical shape this guards against. PR #2036
originally bound the cap via OC's ``--max-turns N`` CLI flag, but OC
2026.6.1 doesn't recognise that flag (PR #2197 reverted the wiring).
OC also exposes no equivalent config knob on ``openclaw.json`` and no
``--budget-usd`` style flag, so the dispatcher has to enforce the cap
from outside the process — by watching the on-disk trajectory log that
OpenClaw writes during the run.

How it's wired (see ``bot_forge._dispatch_agent``):

  1. The dispatcher generates a fresh UUIDv4 session id and passes it to
     ``openclaw agent --session-id <uuid>`` so the trajectory file path
     is fully determined before ``Popen``.
  2. The dispatcher constructs a :class:`ForgeRunawayWatcher` pointed at
     that file with the resolved per-bot ``message_cap`` and the
     dispatcher's local ``_kill_pg`` callback, then calls ``start()``.
  3. The dispatcher polls for the outbox file as usual. The watcher
     polls the trajectory file every 2s in parallel.
  4. When the watcher sees ``model.completed`` count ≥ ``message_cap``,
     it invokes the kill callback (which falls back to ``sudo /bin/kill
     -9 -<pgid>`` to defeat cross-user EPERM — see ``oc_cli._kill_pg``
     for the reference implementation), then exits its loop. The
     dispatcher's poll loop notices the dead subprocess and reads the
     watcher's ``verdict()`` to decide whether to emit the
     ``forge_session_message_cap_exceeded`` Signal.
  5. The dispatcher's ``finally`` block always calls
     ``watcher.stop()`` + ``watcher.join(timeout=…)`` so the thread is
     reaped even on outbox-arrived or exception paths.

Design notes:

* **Substring match, not JSON parse.** Each trajectory line is one JSON
  record; the type-of-record is signalled by ``"type":"model.completed"``
  appearing literally in the line. Counting via substring match is
  ~100× faster than ``json.loads`` per line and the string is specific
  enough that false positives don't happen in practice — the trajectory
  schema doesn't put arbitrary user content into ``"type"`` and the
  string never appears in another field's value in the OC source.

* **Tail semantics.** The watcher remembers the byte offset it has
  already counted and re-reads only the appended bytes each tick. A
  trajectory file for a long-running dispatch can grow to many MB; we
  don't want to re-scan from byte 0 every poll.

* **File-not-found is normal.** ``openclaw agent`` takes 100–500ms
  between ``Popen`` returning and ``session.started`` being persisted
  to disk. The watcher tolerates a missing file silently and just
  retries on the next tick.

* **Verdict states:**
    - ``"cap_exceeded"`` — cap reached, kill callback invoked, watcher exited.
    - ``"stopped"``      — external ``stop()`` arrived before the cap was hit.
    - ``"never_ran"``    — ``start()`` was never called, or the worker
                           thread never entered its loop body.

  ``"stopped"`` is the happy-path outcome for every dispatch that
  finishes under the cap — the dispatcher always calls ``stop()`` after
  the outbox arrives (or the subprocess otherwise terminates). Only
  ``"cap_exceeded"`` is signal-worthy.

* **Idempotent kill.** The kill callback may be invoked at most once
  per watcher lifetime; once fired, the worker exits its loop and
  ``stop()`` is a no-op.

* **No I/O outside the watcher.** The watcher does not emit Signals,
  does not write to disk, does not log to the admin journal — those
  surfaces all belong to the dispatcher. This keeps the watcher pure
  data + a kill callback, which makes it trivial to test with a fake
  trajectory file and a list-appending callback stub.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional


_log = logging.getLogger(__name__)


# The literal substring we count in each trajectory line. The OC trajectory
# schema wraps the event-type discriminator in JSON like
#   {"type":"model.completed", ...}
# so this substring identifies one and only one event class.
_MODEL_COMPLETED_TOKEN = '"model.completed"'


class ForgeRunawayWatcher:
    """Background-thread guardian for a single forge dispatch.

    See module docstring for the full design. Public API:

      * ``start()``           — spawn the worker thread (idempotent).
      * ``stop()``            — signal the worker to exit at next poll.
      * ``join(timeout=...)`` — wait for the worker to finish.
      * ``verdict()``         — terminal state after the worker exits.
      * ``observed_count()``  — count of ``model.completed`` events seen.

    Construction takes:
      * ``trajectory_path``   — absolute path to the dispatch's
                                ``<sessionId>.trajectory.jsonl``. The
                                file does not need to exist yet at
                                construction time.
      * ``message_cap``       — integer cap. ``<= 0`` disables the
                                watcher — ``start()`` is a no-op and
                                ``verdict()`` stays ``"never_ran"``.
                                Useful for test isolation.
      * ``kill_pg``           — zero-arg callable invoked when the cap
                                is exceeded. Errors are caught and
                                logged; the watcher still records
                                ``"cap_exceeded"`` so the dispatcher
                                emits its Signal regardless of whether
                                the kill landed cleanly.
      * ``poll_interval``     — seconds between ticks. 2.0 in
                                production; tests use 0.01.
    """

    def __init__(
        self,
        trajectory_path: Path,
        message_cap: int,
        kill_pg: Callable[[], None],
        *,
        poll_interval: float = 2.0,
    ) -> None:
        self._trajectory_path = Path(trajectory_path)
        self._message_cap = int(message_cap)
        self._kill_pg = kill_pg
        self._poll_interval = max(0.0, float(poll_interval))

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._verdict = "never_ran"
        self._observed = 0
        self._last_offset = 0
        # Carry-over buffer for the final partial line between reads —
        # the trajectory file may be flushed mid-line (it's append+fsync
        # in OC's writer but our read can land between bytes). We hold
        # the trailing partial-line in memory and prepend it on the
        # next tick so the substring match doesn't miss tokens that
        # straddled the read boundary.
        self._carry = ""
        # _lock guards _verdict + _observed for thread-safe reads from
        # the dispatcher after stop()/join(). The worker thread is the
        # only writer; readers are the dispatcher (post-join) and tests.
        self._lock = threading.Lock()

    # ── Public surface ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the worker thread. No-op if cap is non-positive or
        ``start()`` was already called.

        Caps of 0 or negative disable the guard entirely (verdict stays
        ``never_ran``). This lets the dispatcher hand the watcher a
        cap loaded from config without a pre-check — if an operator
        sets ``message_cap: 0`` to disable Guard B for a bot, the
        watcher is constructed but inert.
        """
        if self._message_cap <= 0:
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"forge-watcher:{self._trajectory_path.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to exit at the next poll boundary.

        Safe to call from any thread, any number of times. Does not
        block — pair with ``join()`` when shutdown ordering matters.
        """
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for the worker to finish.

        Idempotent — joining a watcher that was never started or has
        already finished is a no-op.
        """
        if self._thread is None:
            return
        self._thread.join(timeout=timeout)

    def verdict(self) -> str:
        """Terminal state after the worker exits.

        One of ``"cap_exceeded"`` / ``"stopped"`` / ``"never_ran"``.
        Reading before the worker exits returns whatever transient
        state has been recorded so far; in production this is always
        called post-``join``.
        """
        with self._lock:
            return self._verdict

    def observed_count(self) -> int:
        """Best-known count of ``model.completed`` events the worker
        observed in the trajectory file before exiting. Useful for the
        Signal body so the operator sees how far the runaway ran before
        being killed.
        """
        with self._lock:
            return self._observed

    # ── Worker ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Worker thread main loop.

        Polls the trajectory file at ``poll_interval`` boundaries.
        Reads bytes appended since the last tick, counts new
        ``model.completed`` lines, and fires the kill callback if the
        cumulative count crosses ``message_cap``. Exits on either
        cap-hit or external ``stop()``.
        """
        try:
            while not self._stop_event.is_set():
                self._tick()
                with self._lock:
                    if self._observed >= self._message_cap:
                        self._verdict = "cap_exceeded"
                        break
                # Wait honours stop_event so external stop() is
                # noticed within the poll interval, not on a wall-clock
                # boundary.
                if self._stop_event.wait(timeout=self._poll_interval):
                    # External stop arrived during the wait window. Do
                    # one final tick before exiting so any
                    # model.completed lines appended between the
                    # previous tick and the stop signal are counted.
                    # Without this, a fast-writer + immediate-stop
                    # sequence (subprocess writes N lines then dies in
                    # <poll_interval seconds) would miss every line
                    # past the first tick — the watcher would record
                    # verdict=stopped even when the cap was clearly
                    # exceeded by the file on disk.
                    self._tick()
                    with self._lock:
                        if self._observed >= self._message_cap:
                            self._verdict = "cap_exceeded"
                    break
            else:
                # while-loop exited because stop_event was set before
                # the first tick — record as stopped.
                pass

            with self._lock:
                if self._verdict == "never_ran":
                    self._verdict = "stopped"
                if self._verdict == "cap_exceeded":
                    # Fire the kill callback OUTSIDE the lock — the
                    # callback in production calls subprocess.run which
                    # can block briefly. We hold no state the dispatcher
                    # needs synchronised access to during the kill.
                    fire_kill = True
                else:
                    fire_kill = False
        except Exception as exc:
            # The watcher must never propagate exceptions out of the
            # thread — there's no one to catch them. Log and record
            # the worker as stopped so the dispatcher proceeds with
            # its normal failure handling (the outbox poll will hit
            # its deadline and raise on its own).
            _log.warning(
                "forge_runaway_watcher: worker crashed: %s", exc,
            )
            with self._lock:
                if self._verdict == "never_ran":
                    self._verdict = "stopped"
            return

        if fire_kill:
            try:
                self._kill_pg()
            except Exception as exc:
                # Kill failure (e.g. sudo grant missing, process
                # already dead) is non-fatal — the dispatcher still
                # sees verdict=cap_exceeded and emits the Signal.
                # OC's own --timeout will eventually reap the runaway
                # if our kill didn't land.
                _log.warning(
                    "forge_runaway_watcher: kill callback failed: %s", exc,
                )

    def _tick(self) -> None:
        """Read appended bytes since the last tick and count
        ``model.completed`` lines.

        File-not-found and partial-read errors are silently tolerated
        — the trajectory file lifecycle is owned by openclaw and the
        watcher must not race ahead of it. The next tick will retry.
        """
        try:
            # Open per-tick. The trajectory file is OC's, not ours,
            # and keeping the FD open across many seconds invites
            # FD leaks if OC rotates or relocates the file. Per-tick
            # open + close is cheap (microseconds) versus the 2s
            # poll interval.
            with open(self._trajectory_path, "rb") as fh:
                fh.seek(self._last_offset)
                chunk = fh.read()
                self._last_offset = fh.tell()
        except FileNotFoundError:
            return
        except OSError as exc:
            _log.debug(
                "forge_runaway_watcher: read failed at %s: %s",
                self._trajectory_path, exc,
            )
            return

        if not chunk:
            return

        # Decode best-effort — trajectory lines are JSON, hence UTF-8.
        # Use errors='replace' so a stray byte never crashes the tick.
        text = self._carry + chunk.decode("utf-8", errors="replace")

        # Tail semantics: if the final byte isn't a newline, the
        # chunk ended mid-line. Hold the trailing fragment so the
        # next tick prepends it before counting — otherwise a token
        # that straddles a flush boundary would be missed.
        if text.endswith("\n"):
            lines = text.split("\n")
            # Last element is the empty string after the trailing \n;
            # discard it so we don't count it as a line.
            lines = lines[:-1]
            self._carry = ""
        else:
            lines = text.split("\n")
            self._carry = lines[-1]
            lines = lines[:-1]

        new_hits = sum(1 for line in lines if _MODEL_COMPLETED_TOKEN in line)
        if new_hits:
            with self._lock:
                self._observed += new_hits
