"""
spec_jobs.py — background worker for the Create App spec wizard.

## Why this exists

The wizard's ``Generate Spec →`` step used to stream events over SSE
directly inside the Flask request handler. That made the browser tab
load-bearing: if the operator closed the tab, switched to a different
app, or even just let the laptop sleep mid-generation, the connection
dropped and the work was lost. For Power-tier (Opus) generations that
can take 2-5 minutes, that's a real failure mode.

Diagnosed 2026-06-05 during a real operator walkthrough: the first
"Generate Spec" attempt died with "network error" mid-stream. Even
after the SSE keepalive fix (PR #2181 — Anthropic ping pass-through),
the underlying architecture still required the tab to stay open for
the duration.

## What this module does

Runs spec generation in a background thread, persisting progress to
the on-disk session file as it goes. The HTTP request handlers return
session_id immediately; the wizard frontend polls
``GET /api/specs/<session_id>`` every ~2s and renders progress from
``session.generation.*``.

The tab can close, sleep, switch — work survives. When the operator
opens the wizard later, the polling resumes and (if generation has
completed) immediately shows the draft for review.

## Threading model

One background thread per active generation. Threads are tracked in
``_active_workers`` so a cancel request can flip a flag the worker
checks between events. Threads are NOT joined — they're daemon
threads that exit when the process exits. The on-disk session file is
the source of truth; the in-memory thread registry is just for
cancellation routing.

Concurrency cap: the spec-routes registration helper enforces a max of
``MAX_ACTIVE_WORKERS`` simultaneous workers across the admin-ui
process. Beyond that, new requests get a 503 response instructing the
operator to wait or cancel one of the active jobs. The cap is
deliberately low (default 4) because each worker is a long-running
HTTP request to Anthropic that costs real money — bounded concurrency
limits the blast radius of a runaway request loop.

## Persistence + atomicity

Every progress update calls ``save_session`` (atomic temp-file +
rename in spec_session.py). A reader observing the session mid-write
sees either the prior state or the new state, never a torn write.

The worker saves on every Anthropic event (delta, tokens, phase) so
the polling client gets near-real-time progress. That's many small
writes per second during fast generations — acceptable because the
session JSON is small (~2-20 KB) and the writes are local.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Iterator

from .http_errors import log_request_error

# Maximum simultaneous spec-generation workers across the admin-ui
# process. Each worker holds an open HTTP request to Anthropic. The
# cap is intentionally low — see module docstring for rationale.
MAX_ACTIVE_WORKERS = 4


# Active workers, keyed by session_id. Each entry is the worker
# Thread + a cancel flag (a one-element list so the worker can mutate
# it via closure). Module-level state — accessed under _workers_lock.
_active_workers: dict[str, dict] = {}
_workers_lock = threading.Lock()


def active_worker_count() -> int:
    """Return how many spec-generation workers are currently running.

    Used by the route handlers to enforce ``MAX_ACTIVE_WORKERS``.
    Locks briefly; safe to call from any thread.
    """
    with _workers_lock:
        return sum(
            1 for entry in _active_workers.values()
            if entry["thread"].is_alive()
        )


def cancel_worker(session_id: str) -> bool:
    """Request cancellation of a running worker.

    Sets the worker's cancel flag; the worker checks it between events
    and exits cleanly the next chance it gets. Returns True if a
    worker was found (regardless of whether it was already finishing).
    Returns False if no worker is registered for that session.

    Does NOT block waiting for the worker to actually stop —
    cancellation propagates on the next event boundary inside the
    worker's loop (usually within a second).
    """
    with _workers_lock:
        entry = _active_workers.get(session_id)
        if not entry:
            return False
        entry["cancel"][0] = True
        return True


def _cleanup_finished_workers() -> None:
    """Reap completed thread entries from the active-workers registry.

    Called opportunistically by ``register_worker`` so the registry
    doesn't grow unbounded. Holds the lock briefly to scan + remove
    dead entries.
    """
    with _workers_lock:
        dead = [
            sid for sid, entry in _active_workers.items()
            if not entry["thread"].is_alive()
        ]
        for sid in dead:
            del _active_workers[sid]


def register_worker(
    session_id: str,
    target: Callable[[list], None],
) -> bool:
    """Spawn a daemon thread for the given session and register it.

    The target callable receives a one-element list ``cancel_flag``
    (mutable so it can be set from another thread via
    ``cancel_worker``). Target reads ``cancel_flag[0]`` periodically;
    when True, target should clean up and exit.

    Returns True on success. Returns False if the concurrency cap
    would be exceeded — the caller should respond 503 to the HTTP
    client in that case.

    The thread is started as ``daemon=True`` so it does NOT block
    interpreter shutdown. Long-running generations that haven't yet
    completed when admin-ui restarts are simply abandoned — the
    on-disk session file reads as ``generation.status == "running"``
    forever in that case. The startup-recovery sweep (see
    ``reap_orphaned_sessions``) detects this and marks them as
    ``failed`` with an explanatory error.
    """
    _cleanup_finished_workers()

    if active_worker_count() >= MAX_ACTIVE_WORKERS:
        return False

    cancel_flag = [False]
    thread = threading.Thread(
        target=target,
        args=(cancel_flag,),
        name=f"spec-worker-{session_id}",
        daemon=True,
    )
    with _workers_lock:
        _active_workers[session_id] = {
            "thread": thread,
            "cancel": cancel_flag,
        }
    thread.start()
    return True


def reap_orphaned_sessions(shared_dir: Path) -> int:
    """Find sessions whose ``generation.status == "running"`` but no
    worker is registered (e.g. admin-ui restarted mid-generation) and
    flip them to ``generation.status = "failed"``.

    Called at admin-ui startup. Without this, the wizard would poll
    such a session forever, showing "Generating…" with no actual work
    happening. Returns the number of sessions reaped.

    Idempotent: re-running with no orphans is a no-op.
    """
    from ..applications.spec_session import (
        list_sessions, save_session,
    )
    from ..applications.ids import now_iso

    sessions = list_sessions(shared_dir)
    reaped = 0
    with _workers_lock:
        active_ids = set(_active_workers.keys())
    for session in sessions:
        gen = session.generation or {}
        if gen.get("status") != "running":
            continue
        if session.session_id in active_ids:
            continue
        # Worker died (process restart) — mark as failed.
        gen["status"] = "failed"
        gen["error"] = (
            "Spec generation was interrupted (admin-ui restarted "
            "mid-generation). Retry from the wizard to start over."
        )
        gen["completed_at"] = now_iso()
        session.generation = gen
        save_session(session, shared_dir)
        reaped += 1
    return reaped


def run_generation_in_background(
    *,
    session_id: str,
    shared_dir: Path,
    events_factory: Callable[[], Iterator[dict]],
    on_draft: Callable[[dict], None],
    target_version: int,
    save_every_n_events: int = 4,
) -> bool:
    """Spawn a background worker that drives spec generation.

    ``events_factory`` is a thunk that returns the event iterator —
    typically ``lambda: _build_draft_events(description=..., ...)``.
    We defer the call until inside the worker thread so the actual
    Anthropic HTTP request happens on the worker's stack, not the
    request handler's.

    ``on_draft`` is called (still on the worker thread) with the final
    draft dict when generation completes successfully. The caller is
    responsible for updating ``session.drafts`` + ``session.status``
    from that callback — this module doesn't know the session's
    business rules (e.g. whether to flip to "draft" vs "iterating").

    ``target_version`` is recorded in ``generation.version`` so the
    polling client knows which draft round this generation is
    producing (helpful when multiple iterate rounds are happening
    in quick succession).

    Returns True if the worker started. False if the concurrency cap
    would have been exceeded — caller should 503.
    """
    from ..applications.spec_session import (
        load_session, save_session,
    )
    from ..applications.ids import now_iso

    def _worker(cancel_flag: list) -> None:
        """Runs on the background thread. All updates to the session
        go through reload-modify-save to tolerate concurrent edits
        (e.g. a cancel POST flipping the cancel flag while we're
        mid-event)."""

        def _update(mutate: Callable[[dict], None]) -> None:
            """Read session, mutate generation dict, save. Safe under
            concurrent reads via the atomic save in spec_session.py.
            """
            sess = load_session(session_id, shared_dir)
            if sess is None:
                return
            gen = dict(sess.generation or {})
            mutate(gen)
            sess.generation = gen
            save_session(sess, shared_dir)

        # Initial state
        _update(lambda g: g.update({
            "status": "running",
            "phase": "context",
            "message": "Starting generation…",
            "version": target_version,
            "started_at": now_iso(),
            "partial_chars": 0,
            "partial_tokens": 0,
            "input_tokens": 0,
        }))

        partial_chars = 0
        partial_tokens = 0
        input_tokens = 0
        draft: dict | None = None
        events_seen = 0
        last_saved_at_events = 0
        terminal_error: str | None = None

        try:
            for evt in events_factory():
                events_seen += 1
                if cancel_flag[0]:
                    terminal_error = "cancelled"
                    break

                evt_type = evt.get("type")
                if evt_type == "phase":
                    phase = evt.get("phase") or ""
                    message = evt.get("message") or ""
                    model_full = evt.get("model") or ""
                    tier = evt.get("tier") or ""

                    def _apply_phase(g: dict) -> None:
                        g["phase"] = phase
                        g["message"] = message
                        if model_full:
                            g["model_full"] = model_full
                        if tier:
                            g["tier"] = tier
                    _update(_apply_phase)
                elif evt_type == "delta":
                    text = evt.get("text") or ""
                    partial_chars += len(text)
                    # Save every N delta events to avoid hammering disk
                    if events_seen - last_saved_at_events >= save_every_n_events:
                        last_saved_at_events = events_seen
                        local_pc = partial_chars
                        _update(lambda g: g.update({"partial_chars": local_pc}))
                elif evt_type == "tokens":
                    input_tokens = int(evt.get("input") or 0)
                    partial_tokens = int(evt.get("output") or 0)
                    local_in = input_tokens
                    local_out = partial_tokens
                    _update(lambda g: g.update({
                        "input_tokens": local_in,
                        "partial_tokens": local_out,
                    }))
                elif evt_type == "draft":
                    draft = evt.get("draft")
                elif evt_type == "keepalive":
                    # Keepalive events from the SSE pass-through layer
                    # (PR #2181) — no-op for the worker since we don't
                    # need to keep any TCP connection alive here.
                    pass
                elif evt_type == "error":
                    terminal_error = evt.get("message") or "Unknown error"
                    break

            if cancel_flag[0]:
                _update(lambda g: g.update({
                    "status": "cancelled",
                    "completed_at": now_iso(),
                }))
                return

            if terminal_error:
                err = terminal_error
                _update(lambda g: g.update({
                    "status": "failed",
                    "error": err,
                    "completed_at": now_iso(),
                }))
                return

            if draft is None:
                _update(lambda g: g.update({
                    "status": "failed",
                    "error": "Spec generation produced no draft.",
                    "completed_at": now_iso(),
                }))
                return

            # Hand the draft to the caller's on_draft hook (which
            # updates session.drafts / session.status appropriately
            # for this round — gathering→draft for first generation,
            # iterating-already→iterating for iterate rounds, etc.).
            try:
                on_draft(draft)
            except Exception as exc:
                # Full traceback → admin log only; the operator-visible job
                # status carries just the message, never a trace. Bind the
                # message now: `exc` is unbound once the except block exits, so
                # the lambda must close over `err`, not `exc`.
                log_request_error(exc)
                err = f"Draft post-processing failed: {exc}"
                _update(lambda g: g.update({
                    "status": "failed",
                    "error": err,
                    "completed_at": now_iso(),
                }))
                return

            _update(lambda g: g.update({
                "status": "completed",
                "phase": "done",
                "message": "Spec ready for review.",
                "partial_chars": partial_chars,
                "partial_tokens": partial_tokens,
                "input_tokens": input_tokens,
                "completed_at": now_iso(),
            }))
        except Exception as exc:
            # Full traceback → admin log only (see the on_draft handler above);
            # bind `err` now so the lambda doesn't close over the soon-unbound `exc`.
            log_request_error(exc)
            err = f"Generation worker crashed: {exc}"
            _update(lambda g: g.update({
                "status": "failed",
                "error": err,
                "completed_at": now_iso(),
            }))

    return register_worker(session_id, _worker)
