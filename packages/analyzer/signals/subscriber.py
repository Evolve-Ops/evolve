"""signals.subscriber — Event-driven dispatch of generators on Signal arrival.

The daily ``generator_runner`` sweep is the safety net; this module is the
*latency* layer. When an acute Signal lands in ``{shared_dir}/signals/firing/``,
the subscriber detects it within seconds and invokes the observe() path of any
generator whose charter declared ``subscribes_to: [<type>, ...]``.

Two pieces:

  - :func:`build_subscription_map` reads every charter once and returns
    ``{signal_type: [generator_id, ...]}``. Empty for the common case where
    no generator subscribes to a given Signal type.
  - :func:`dispatch_signal` looks up subscribers for a Signal, then for each
    subscriber checks the ledger and invokes the generator via
    ``generator_runner.run_one_generator`` with cadence skipped. Idempotent.

The ledger lives at ``{shared_dir}/signal_subscribers/ledger.jsonl`` and is
append-only. Each line is one (generator_id, signal_id, fired_at) record.
The daily sweep uses the same ledger to avoid re-firing for Signals already
handled event-driven (and vice versa).

Design notes:

  - Polling, not fsnotify. ``watchdog`` is not in the venv; adding it would
    require a pip install on the mini for a 5-second latency target that
    polling at 1s comfortably meets. Filesystem ``os.scandir`` over the
    firing/ dir is cheap (tens of small JSON files, typically <50).
  - Per-generator semaphore: a flood of Signals (e.g. cost_watchdog
    re-observing the same cluster every hour) cannot stampede the same
    generator. Each dispatch checks the ledger first; observed already →
    skip.
  - Failure isolation: a broken generator must not prevent dispatch of the
    next Signal to a different generator. All invocations are wrapped in
    try/except with logging.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from signals.store import (
    iter_signals,
    load_signal_file,
    signal_path,
    signals_root,
)

logger = logging.getLogger("evolve_admin.signal_subscriber")


# ─────────────────────────────────────────────────────────────────────────────
# Subscription map
# ─────────────────────────────────────────────────────────────────────────────


def build_subscription_map(
    *,
    generators_code_dir: Path,
    records_dir: Path,
) -> dict[str, list[str]]:
    """Read every active generator's charter and build a ``type → [gen_id]`` map.

    Inactive (paused/quarantined) generators are skipped — a quarantined
    generator should not be event-dispatched any more than it should be
    swept-dispatched. The map is rebuilt on each daemon iteration so a
    charter change picked up by the registry takes effect on the next tick
    (within seconds), no daemon restart required.
    """
    # Import locally so this module stays import-light when imported by
    # consumers that don't need the registry (e.g. unit tests that build
    # the map by hand).
    from registry.registry import Registry

    try:
        registry = Registry(
            generators_code_dir=generators_code_dir,
            records_dir=records_dir,
        )
        registry.load_all(strict=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("subscriber: registry load failed: %s", exc)
        return {}

    out: dict[str, list[str]] = {}
    for gen_id, lg in registry.active_generators().items():
        for sig_type in lg.charter.subscribes_to:
            out.setdefault(sig_type, []).append(gen_id)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Ledger
# ─────────────────────────────────────────────────────────────────────────────


def subscriber_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "signal_subscribers"


def ledger_path(shared_dir: Path) -> Path:
    return subscriber_root(shared_dir) / "ledger.jsonl"


def _ensure_subscriber_dir(shared_dir: Path) -> None:
    """Create the per-shared signal_subscribers dir if absent.

    Idempotent. Owned by the ``evolve`` user since this module runs in the
    same daemon as the rest of analyzer; no /tmp staging required.
    """
    root = subscriber_root(shared_dir)
    root.mkdir(parents=True, exist_ok=True)


def has_fired(shared_dir: Path, *, generator_id: str, signal_id: str) -> bool:
    """Return True if ``(generator_id, signal_id)`` already appears in the ledger.

    Linear scan — the ledger is tiny in practice (one line per dispatch,
    pruned to the last N records on a daily rotation). For an absurd ledger
    size we would index by signal_id, but that's an optimization for later.
    A missing ledger is a no-op (returns False).
    """
    path = ledger_path(shared_dir)
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    rec.get("generator_id") == generator_id
                    and rec.get("signal_id") == signal_id
                ):
                    return True
    except OSError:
        return False
    return False


def record_fire(
    shared_dir: Path,
    *,
    generator_id: str,
    signal_id: str,
    signal_type: str,
    proposals_ingested: int,
    fired_at: str | None = None,
) -> None:
    """Append a dispatch record to the ledger.

    Single-line JSON < PIPE_BUF is atomic under POSIX, so no temp-file
    dance is required for append-only audit data. Failures are swallowed:
    a missing ledger entry means at worst that the daily sweep will
    re-fire (which is itself idempotent — the next dispatch sees the
    signal still firing, runs observe again, the arbiter dedupes the
    resulting Proposal). Belt and suspenders.
    """
    _ensure_subscriber_dir(shared_dir)
    path = ledger_path(shared_dir)
    rec = {
        "generator_id": generator_id,
        "signal_id": signal_id,
        "signal_type": signal_type,
        "proposals_ingested": int(proposals_ingested),
        "fired_at": fired_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning("subscriber: ledger append failed: %s", exc)


# Ledger retention: keep one week of entries on disk. That is more than
# enough to cover the daily-sweep dedup window (24h) plus a generous safety
# margin. Prune is idempotent; runs at most once per daemon iteration.
LEDGER_RETENTION_SECONDS = 7 * 86400


def prune_ledger(shared_dir: Path, *, now: datetime | None = None) -> int:
    """Drop ledger entries older than :data:`LEDGER_RETENTION_SECONDS`.

    Returns the number of entries kept. Safe to call concurrently with
    :func:`record_fire` only because record_fire is append-only and prune
    rewrites via temp+rename — the worst case is one lost append, which
    the next dispatch's record_fire will re-create (the Signal will still
    be firing, the ledger check will miss, observe will fire again, and
    the new record lands). Acceptable.
    """
    path = ledger_path(shared_dir)
    if not path.exists():
        return 0
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - LEDGER_RETENTION_SECONDS
    kept: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("fired_at")
                if not ts:
                    continue
                try:
                    when = datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    continue
                if when >= cutoff:
                    kept.append(line)
    except OSError:
        return 0
    # Rewrite atomically. Bounded by retention so the file is small.
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("subscriber: ledger prune rewrite failed: %s", exc)
    return len(kept)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DispatchResult:
    """Per-generator outcome of dispatching one Signal."""

    generator_id: str
    signal_id: str
    fired: bool
    proposals_ingested: int = 0
    skipped_reason: str | None = None
    error: str | None = None


# Type of the runner callable. Made a parameter (not an import) so tests can
# inject a stub and skip the full generator_runner stack.
RunOneGenerator = Callable[..., int]


def dispatch_signal(
    shared_dir: Path,
    *,
    signal_id: str,
    subscription_map: dict[str, list[str]],
    network_config: dict,
    run_one_generator: RunOneGenerator | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> list[DispatchResult]:
    """Look up subscribers for the Signal at ``signal_id`` and invoke each.

    The Signal must be in the firing/ subdir — if it has already transitioned
    to snoozed or archived (operator action, sweep, etc.) the dispatch is a
    no-op for that Signal.

    ``run_one_generator`` defaults to ``generator_runner.run_one_generator``.
    Tests may pass a stub.

    ``log_fn`` defaults to the module logger; passing a callable routes
    output through the daemon's log file the same way the rest of the
    analyzer stack does.
    """
    if log_fn is None:
        log_fn = logger.info

    if run_one_generator is None:
        from generator_runner import run_one_generator as _real_runner
        run_one_generator = _real_runner

    sig_file = signal_path(shared_dir, signal_id, subdir="firing")
    signal = load_signal_file(sig_file)
    if signal is None:
        # Signal may have transitioned out of firing/ between detection and
        # dispatch (operator action, sweep resolve, etc.). Not an error.
        log_fn(f"[signal_subscriber] {signal_id}: no longer in firing/, skip")
        return []

    subscribers = subscription_map.get(signal.type, [])
    if not subscribers:
        return []

    results: list[DispatchResult] = []
    for gen_id in subscribers:
        if has_fired(shared_dir, generator_id=gen_id, signal_id=signal_id):
            results.append(
                DispatchResult(
                    generator_id=gen_id,
                    signal_id=signal_id,
                    fired=False,
                    skipped_reason="already_fired",
                )
            )
            continue

        try:
            ingested = run_one_generator(
                shared_dir,
                network_config,
                gen_id,
                bot_id=signal.bot_id,
                log_fn=log_fn,
            )
        except Exception as exc:  # noqa: BLE001 — one generator's failure
            # must never stop dispatch of subsequent subscribers.
            log_fn(
                f"[signal_subscriber] {gen_id} dispatch raised on "
                f"signal {signal_id} ({signal.type}): {exc}"
            )
            results.append(
                DispatchResult(
                    generator_id=gen_id,
                    signal_id=signal_id,
                    fired=False,
                    error=str(exc),
                )
            )
            # Record the fire anyway so a perpetually-broken generator does
            # not get retried every poll for the lifetime of the Signal.
            # The daily sweep will eventually drop the Signal when the
            # underlying condition clears (resolves_when_silent) — that is
            # the right place to surface the broken-generator issue, via
            # generator_load_error / monitor_coverage.
            record_fire(
                shared_dir,
                generator_id=gen_id,
                signal_id=signal_id,
                signal_type=signal.type,
                proposals_ingested=0,
            )
            continue

        record_fire(
            shared_dir,
            generator_id=gen_id,
            signal_id=signal_id,
            signal_type=signal.type,
            proposals_ingested=int(ingested),
        )
        results.append(
            DispatchResult(
                generator_id=gen_id,
                signal_id=signal_id,
                fired=True,
                proposals_ingested=int(ingested),
            )
        )
        log_fn(
            f"[signal_subscriber] {gen_id}: dispatched on "
            f"signal {signal_id} ({signal.type}, bot={signal.bot_id}); "
            f"ingested={ingested}"
        )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────


def find_new_signal_ids(
    shared_dir: Path,
    *,
    seen: set[str],
    types_of_interest: Iterable[str] | None = None,
) -> list[str]:
    """Return Signal IDs present in firing/ that are not in ``seen``.

    ``types_of_interest`` filters server-side so we don't pay the JSON-load
    cost on Signals nobody subscribes to. Pass the keys of the subscription
    map. ``None`` means "load every firing Signal" — fine for tests.

    Mutates ``seen`` in place by adding every Signal id observed (including
    those that did not match the type filter — once we've classified a
    Signal id once, no point re-loading it).
    """
    firing_dir = signals_root(shared_dir) / "firing"  # store-access-lint: store-internal 1Hz scandir (perf — skips JSON parse on uninteresting signals)
    if not firing_dir.exists():
        return []

    interest = set(types_of_interest) if types_of_interest is not None else None
    new_ids: list[str] = []
    try:
        with os.scandir(firing_dir) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                name = entry.name
                if not name.endswith(".json"):
                    continue
                # Skip atomic-write temp files (.<name>.<rand>.tmp).
                if name.startswith("."):
                    continue
                signal_id = name[:-5]  # strip .json
                if signal_id in seen:
                    continue
                if interest is not None:
                    sig = load_signal_file(Path(entry.path))
                    if sig is None or sig.type not in interest:
                        seen.add(signal_id)
                        continue
                seen.add(signal_id)
                new_ids.append(signal_id)
    except OSError as exc:
        logger.warning("subscriber: scandir failed: %s", exc)
    return new_ids


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────


def bootstrap_seen_set(shared_dir: Path) -> set[str]:
    """On daemon start, mark every currently-firing Signal as already seen.

    Without this, the first iteration after daemon start would dispatch
    every firing Signal — including ones that have been firing for days
    that prior sweeps already handled. The bootstrap pass is "consider
    *new* Signals from now on, the existing backlog is the daily sweep's
    job" (and the ledger backs us up if a Signal we already processed
    re-fires).

    Returns the set of currently-firing Signal IDs. Caller passes this
    into the polling loop.
    """
    seen: set[str] = set()
    for sig in iter_signals(shared_dir, subdirs=("firing",)):
        seen.add(sig.id)
    return seen


# ─────────────────────────────────────────────────────────────────────────────
# Polling loop entry point
# ─────────────────────────────────────────────────────────────────────────────


# How often (seconds) to refresh the subscription map from the registry.
# Charters rarely change in production; once a minute is plenty.
SUBSCRIPTION_REFRESH_SECONDS = 60.0

# How often (seconds) to prune the ledger. Daily is plenty given retention
# is a week — but we run it on the same heartbeat as subscription refresh
# so a single uptime tick covers everything cheap.
LEDGER_PRUNE_INTERVAL_SECONDS = 3600.0


@dataclass
class _LoopState:
    """Per-iteration scratch state for :func:`run_loop`."""

    seen: set[str] = field(default_factory=set)
    subscription_map: dict[str, list[str]] = field(default_factory=dict)
    last_subscription_refresh: float = 0.0
    last_ledger_prune: float = 0.0


def run_loop(
    shared_dir: Path,
    *,
    network_config: dict,
    generators_code_dir: Path,
    records_dir: Path,
    poll_interval_seconds: float = 1.0,
    stop_after_seconds: float | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    now_fn: Callable[[], float] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> int:
    """Main poll loop. Returns the total number of dispatches that succeeded.

    The poll interval is the producer-to-dispatch latency floor. Default
    1s comfortably meets the spec's 5s upper bound. ``stop_after_seconds``
    is the test hook — production passes ``None`` to run forever.

    ``sleep_fn``/``now_fn`` are test hooks so a unit test can advance the
    clock without actually sleeping.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep
    if now_fn is None:
        now_fn = time.monotonic
    if log_fn is None:
        log_fn = logger.info

    state = _LoopState()
    state.seen = bootstrap_seen_set(shared_dir)
    state.subscription_map = build_subscription_map(
        generators_code_dir=generators_code_dir,
        records_dir=records_dir,
    )
    state.last_subscription_refresh = now_fn()
    state.last_ledger_prune = now_fn()

    log_fn(
        f"[signal_subscriber] started: poll={poll_interval_seconds}s, "
        f"subscribed_types={sorted(state.subscription_map.keys())}, "
        f"already_firing={len(state.seen)}"
    )

    total_dispatched = 0
    started_at = now_fn()

    while True:
        # Periodic refresh of the subscription map (cheap: registry load).
        if now_fn() - state.last_subscription_refresh >= SUBSCRIPTION_REFRESH_SECONDS:
            new_map = build_subscription_map(
                generators_code_dir=generators_code_dir,
                records_dir=records_dir,
            )
            if new_map != state.subscription_map:
                log_fn(
                    f"[signal_subscriber] subscription map updated: "
                    f"{sorted(new_map.keys())}"
                )
                state.subscription_map = new_map
            state.last_subscription_refresh = now_fn()

        # Periodic ledger prune.
        if now_fn() - state.last_ledger_prune >= LEDGER_PRUNE_INTERVAL_SECONDS:
            try:
                kept = prune_ledger(shared_dir)
                log_fn(f"[signal_subscriber] ledger pruned: kept={kept}")
            except Exception as exc:  # noqa: BLE001
                log_fn(f"[signal_subscriber] ledger prune failed: {exc}")
            state.last_ledger_prune = now_fn()

        # Discover new firing Signals of interest.
        types = state.subscription_map.keys() if state.subscription_map else None
        if types:
            new_ids = find_new_signal_ids(
                shared_dir,
                seen=state.seen,
                types_of_interest=types,
            )
        else:
            new_ids = []  # nothing subscribed, no point scanning

        for sid in new_ids:
            results = dispatch_signal(
                shared_dir,
                signal_id=sid,
                subscription_map=state.subscription_map,
                network_config=network_config,
                log_fn=log_fn,
            )
            total_dispatched += sum(1 for r in results if r.fired)

        # Sweep ``seen`` of IDs that have left firing/ so the set doesn't
        # grow unbounded on long-running pods. A Signal that re-opens (per
        # the store's reopen_window) will write a NEW signal id, so this is
        # safe — we are not at risk of missing a re-open by trimming seen.
        _trim_seen(shared_dir, state.seen)

        if stop_after_seconds is not None and (now_fn() - started_at) >= stop_after_seconds:
            log_fn(
                f"[signal_subscriber] stop_after_seconds reached "
                f"({stop_after_seconds}s); exit (total_dispatched={total_dispatched})"
            )
            return total_dispatched

        sleep_fn(poll_interval_seconds)


def _trim_seen(shared_dir: Path, seen: set[str]) -> None:
    """Drop ids from ``seen`` whose Signal is no longer in firing/.

    Done in-place. Cheap: one scandir + set difference. Without this, a
    long-running daemon's ``seen`` would grow O(N) in lifetime Signal
    count — fine for a week, wasteful for months. Trimming is a
    correctness no-op (we still consult the on-disk ledger before
    dispatching) and just bounds memory.
    """
    firing_dir = signals_root(shared_dir) / "firing"  # store-access-lint: store-internal 1Hz scandir (perf — skips JSON parse on uninteresting signals)
    if not firing_dir.exists():
        seen.clear()
        return
    on_disk: set[str] = set()
    try:
        with os.scandir(firing_dir) as it:
            for entry in it:
                name = entry.name
                if entry.is_file() and name.endswith(".json") and not name.startswith("."):
                    on_disk.add(name[:-5])
    except OSError:
        return
    seen.intersection_update(on_disk)
