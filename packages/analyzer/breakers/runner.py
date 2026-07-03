"""breakers.runner — Phase 5: auto-trip from detector → store.

Spec: docs/spec-circuit-breakers-2026-05-21.md §8 Phase 5.

Per-cycle work:

  1. For each bot in the pod, load recent turn JSONLs (eval window
     + a baseline-load buffer).
  2. Compute the rolling 7-day baseline as of "now".
  3. Evaluate the activity-shape detector against the most recent
     evaluation window (default 1 hour).
  4. For every (bot, decision) pair:
     - Emit a structured runner-log entry when the decision is
       ACTIONABLE (a trip is recommended) or CHANGED vs the bot's
       prior cycle — feeds the "what would have tripped"
       calibration loop. The steady-state observe-only
       ``trip:false`` no-op (the ~95% bulk, written every 10 min
       per bot) is suppressed at the source so the log is not a
       write-only, never-read, never-pruned leak (footprint
       F-5-A). Set ``breakers.runner_log_full_verbosity`` in
       network.json to opt the full per-cycle stream back in for a
       calibration soak.
     - If the decision is ``trip`` AND no active trip already
       exists for this (bot, "cost") AND the auto_trip feature
       flag is on:
         a. Call breakers.store.trip() to write state.
         b. Call breakers_enforce.enforce_trip() to actually
            block plugin-side traffic (and bootout for "full",
            though the rule currently only fires "cost").
         c. Notify the sysadmin via alerts.dispatcher.send()
            with the rule's reason + the bot.
         d. If a previous auto-trip on this (bot, type) cleared
            within the last 48h, observe a
            ``recurrent_breaker_trip`` Signal so the operator
            sees the pattern rather than just one more trip.

The feature flag (``breakers.auto_trip_enabled`` in network.json,
default ``false``) gates step 4b-d only. The detector still runs
and runner-log entries are still written when the flag is off
(subject to the step-4 source-cut above) — that's the observe-only
mode used during calibration soak.

Idempotent: re-runs against the same window won't double-trip
because step 4 short-circuits on "active trip already exists."
Safe to run on overlapping cron cadences if needed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path
from evolve_util import now_iso_micro as _now_iso

from breakers.baseline import compute_baseline
from breakers.detector import DEFAULT_CONFIG, DetectorConfig, Decision, evaluate_window


log = logging.getLogger(__name__)


# Default evaluation window. The detector evaluates "what happened in
# the last EVAL_WINDOW_HOURS." 1 hour is short enough for the
# rate-spike prong to react during an active incident and long enough
# to clear the min_auto_turns_gate on a normal cron cadence.
DEFAULT_EVAL_WINDOW_HOURS: float = 1.0

# How far back to read turn data when computing each cycle. Covers the
# 7-day baseline plus a small buffer for time-zone edges.
DEFAULT_LOAD_DAYS: int = 8

# Window within which a re-trip on the same (bot, type) is "recurrent."
# Per spec §9 decision 3.
RECURRENT_TRIP_WINDOW_HOURS: int = 48

# Only L1 (cost) currently has a runner-driven trip path. L2 (full
# halt) auto-trips come from the security_warden (per spec §5.1.4),
# not the activity-shape detector here. Documented separately so the
# constant is easy to grep.
RUNNER_BREAKER_TYPE: str = "cost"


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class RunnerAction:
    """What the runner did for one bot in one cycle."""

    bot_id: str
    decision: dict[str, Any]            # serialized Decision
    actioned: bool                       # True iff trip+enforce+notify ran
    skip_reason: str = ""                # populated when actioned=False
    trip_id: str | None = None
    enforce_ok: bool | None = None       # None if not attempted
    notified: bool = False
    recurrent: bool = False              # True iff recurrent_breaker_trip
                                         # signal was also observed


@dataclass
class RunnerResult:
    """One cycle's full output. Returned by run_once() for testability."""

    started_at: str
    finished_at: str
    auto_trip_enabled: bool
    actions: list[RunnerAction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "auto_trip_enabled": self.auto_trip_enabled,
            "actions": [asdict(a) for a in self.actions],
        }


# ── Config + path helpers ────────────────────────────────────────────────────


def _read_auto_trip_enabled(network: dict | None) -> bool:
    """Read ``breakers.auto_trip_enabled`` from network.json.

    Default ``False``. Until the operator explicitly flips this on,
    the runner is observe-only — detector runs, decisions are logged,
    no real trips happen. Calibration step lives here: run for a few
    days, inspect the runner log, tune DetectorConfig, then flip
    this on.
    """
    if not isinstance(network, dict):
        return False
    breakers_cfg = network.get("breakers") or {}
    return bool(breakers_cfg.get("auto_trip_enabled", False))


def _read_runner_log_verbose(network: dict | None) -> bool:
    """Read ``breakers.runner_log_full_verbosity`` from network.json.

    Default ``False``. When off (the default), the runner-log records only
    ACTIONABLE decisions (a trip is recommended) and decision CHANGES vs the
    prior cycle for each bot — the steady-state observe-only ``trip:false``
    no-op (~95% of volume on a quiet pod, written every 10 min per bot) is
    suppressed at the source. That re-emission had zero production readers and
    no retention, so it was pure write-only sediment (footprint F-5-A).

    When on, every cycle writes a full per-bot record — the original
    full-verbosity stream, kept as an opt-in for a calibration soak where the
    complete decision history is wanted. Off by default.
    """
    if not isinstance(network, dict):
        return False
    breakers_cfg = network.get("breakers") or {}
    return bool(breakers_cfg.get("runner_log_full_verbosity", False))


def _runner_log_dir(shared_dir: Path) -> Path:
    p = Path(shared_dir) / "breakers" / "runner-log"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _last_decision_path(shared_dir: Path) -> Path:
    """Sidecar tracking each bot's last-observed decision signature.

    Lets the source-cut suppress steady-state no-op re-emissions while still
    logging every state change exactly once. A dotfile under ``runner-log/`` —
    invisible to the ``*.jsonl`` retention glob and rewritten in place (never
    accumulated)."""
    return _runner_log_dir(shared_dir) / ".last-decision.json"


def _read_last_decisions(shared_dir: Path) -> dict[str, str]:
    """Load the per-bot last-decision signatures; {} on any read/parse error
    (a missing or corrupt sidecar just means every bot logs this cycle)."""
    try:
        raw = json.loads(_last_decision_path(shared_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _write_last_decisions(shared_dir: Path, sigs: dict[str, str]) -> None:
    """Atomically persist the per-bot signatures for the next cycle's
    change-detection. Best-effort: a write failure just means the next cycle
    re-logs (a harmless duplicate), never a lost trip."""
    path = _last_decision_path(shared_dir)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(sigs, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("breakers.runner: last-decision write failed: %s", exc)


def _decision_signature(action: "RunnerAction") -> str:
    """The categorical decision fingerprint used to detect cycle-over-cycle
    state changes. Deliberately EXCLUDES the per-cycle ``metrics`` (the
    underlying counts shift every tick) — only the actionable shape matters:
    did we act, was a trip recommended, and (if skipped) why."""
    trip = bool(action.decision.get("trip"))
    return f"{action.actioned}|{trip}|{action.skip_reason}"


def _runner_log_path(shared_dir: Path, when: datetime | None = None) -> Path:
    when = when or datetime.now(timezone.utc)
    return _runner_log_dir(shared_dir) / f"{when.strftime('%Y-%m-%d')}.jsonl"


def _append_runner_log(shared_dir: Path, record: dict[str, Any]) -> None:
    """Append one runner record to today's log. O_APPEND atomic for
    small JSON. Same pattern as breakers.store audit log."""
    path = _runner_log_path(shared_dir)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        log.warning("breakers.runner: log append failed: %s", exc)


# ── Recurrent-trip detection ─────────────────────────────────────────────────


def _was_recently_auto_tripped(
    shared_dir: Path, bot_id: str, breaker_type: str,
    *, window_hours: int = RECURRENT_TRIP_WINDOW_HOURS,
    now: datetime | None = None,
) -> bool:
    """Did this (bot, type) get auto-tripped AND cleared in the last
    ``window_hours`` hours?

    Reads the breakers audit log directly to avoid an import dep on
    breakers.store (and to make the check trivially testable in
    isolation). Looks for any ``trip`` or ``retrip`` entry with
    ``initiated_by == "auto"`` followed by a clear (``reset`` or
    ``auto_recover``) within the window.

    Conservative: if the audit log is unreadable, returns False so a
    glitch doesn't suppress a real recurrent-trip signal.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    # Audit log layout: {shared_dir}/breakers/log/<YYYY-MM-DD>.jsonl.
    # Span at most window_hours/24 + 1 days back.
    log_dir = Path(shared_dir) / "breakers" / "log"
    if not log_dir.is_dir():
        return False

    days_to_check = max(2, (window_hours // 24) + 2)
    saw_auto_trip = False
    for d_offset in range(days_to_check):
        day = now - timedelta(days=d_offset)
        path = log_dir / f"{day.strftime('%Y-%m-%d')}.jsonl"
        if not path.is_file():
            continue
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
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("scope") != bot_id:
                        continue
                    if rec.get("type") != breaker_type:
                        continue
                    # Parse timestamp; skip on parse error.
                    ts_str = rec.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(
                            ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        continue
                    if ts < cutoff:
                        continue
                    action = rec.get("action")
                    initiated_by = rec.get("initiated_by", "")
                    if action in ("trip", "retrip") and initiated_by == "auto":
                        saw_auto_trip = True
        except OSError:
            continue
    return saw_auto_trip


# ── Core cycle ───────────────────────────────────────────────────────────────


def _read_turns_for_bot(
    shared_dir: Path, bot_id: str,
    *, since: datetime, until: datetime,
) -> list[dict]:
    """Read turn JSONLs for one bot in [since, until). Delegates to
    the existing backtest harness's reader so we share semantics."""
    from breakers.backtest import read_turns
    return read_turns(shared_dir, bot_id, since=since, until=until)


def run_once(
    *,
    shared_dir: Path,
    network: dict,
    detector_config: DetectorConfig = DEFAULT_CONFIG,
    eval_window_hours: float = DEFAULT_EVAL_WINDOW_HOURS,
    load_days: int = DEFAULT_LOAD_DAYS,
    now: datetime | None = None,
    # Injection points (testing) — default to the real implementations.
    trip_fn: Callable[..., Any] | None = None,
    enforce_trip_fn: Callable[..., Any] | None = None,
    read_trip_fn: Callable[..., Any] | None = None,
    list_active_fn: Callable[..., Any] | None = None,
    dispatcher_send_fn: Callable[..., Any] | None = None,
    signal_observe_fn: Callable[..., Any] | None = None,
) -> RunnerResult:
    """Run one detector cycle. Returns a RunnerResult.

    Emits a runner-log entry per bot only when the decision is
    actionable (a trip is recommended) or changed vs the bot's prior
    cycle — the steady-state observe-only no-op is suppressed at the
    source (footprint F-5-A; opt the full stream back in via
    ``breakers.runner_log_full_verbosity``). The decision is recorded
    regardless of whether auto-trip is on, so calibration can run
    without flipping the auto-trip flag.

    The function takes injection points for store/enforce/dispatcher/
    signals so tests can run end-to-end without I/O. Production
    callers leave them None and get the real implementations.
    """
    now = now or datetime.now(timezone.utc)
    started_at = now.isoformat()
    eval_end = now
    eval_start = now - timedelta(hours=eval_window_hours)
    load_start = now - timedelta(days=load_days)

    auto_trip_enabled = _read_auto_trip_enabled(network)

    # Resolve target bots. The breakers store treats "pod" as a
    # reserved scope; here we only auto-trip per-bot (the detector is
    # bot-local), so we iterate the bot list directly.
    bots_cfg = network.get("bots") or {}
    bot_ids = sorted(bots_cfg.keys())

    # Lazy resolution of real impls — keep the signature clean for
    # callers that just want the defaults.
    if trip_fn is None or enforce_trip_fn is None or read_trip_fn is None or list_active_fn is None:
        from breakers import store as _store
        if trip_fn is None:
            trip_fn = _store.trip
        if read_trip_fn is None:
            read_trip_fn = _store.read_trip
        if list_active_fn is None:
            list_active_fn = _store.list_active
    if enforce_trip_fn is None:
        # Real enforce lives in admin; we import lazily and only when
        # auto-trip is actually going to fire. Allows runner to be
        # importable from slim contexts where evolve_admin isn't
        # importable. observe-only callers never need this.
        def _lazy_enforce(*, scope, breaker_type, network, **kwargs):
            from evolve_admin import breakers_enforce as _be
            return _be.enforce_trip(
                scope=scope, breaker_type=breaker_type, network=network, **kwargs
            )
        enforce_trip_fn = _lazy_enforce
    if dispatcher_send_fn is None:
        def _lazy_send(**kwargs):
            from evolve_admin.alerts import dispatcher as _disp
            return _disp.send(**kwargs)
        dispatcher_send_fn = _lazy_send
    if signal_observe_fn is None:
        from signals import store as _sigs
        signal_observe_fn = _sigs.observe

    # Source-cut state (footprint F-5-A): suppress the steady-state
    # observe-only no-op re-emission. `last_sigs` is each bot's decision
    # signature from the prior cycle; we log only actionable decisions and
    # changes. `runner_log_verbose` opts the full per-cycle stream back in.
    runner_log_verbose = _read_runner_log_verbose(network)
    last_sigs = _read_last_decisions(shared_dir)
    new_sigs: dict[str, str] = {}

    actions: list[RunnerAction] = []
    for bot_id in bot_ids:
        try:
            action = _process_one_bot(
                shared_dir=shared_dir,
                network=network,
                bot_id=bot_id,
                detector_config=detector_config,
                eval_start=eval_start,
                eval_end=eval_end,
                load_start=load_start,
                auto_trip_enabled=auto_trip_enabled,
                now=now,
                trip_fn=trip_fn,
                enforce_trip_fn=enforce_trip_fn,
                read_trip_fn=read_trip_fn,
                list_active_fn=list_active_fn,
                dispatcher_send_fn=dispatcher_send_fn,
                signal_observe_fn=signal_observe_fn,
            )
        except Exception as exc:  # noqa: BLE001 — safety net
            log.exception("breakers.runner: %s outer guard", bot_id)
            action = RunnerAction(
                bot_id=bot_id,
                decision={"trip": False, "reason": f"runner exception: {exc}"},
                actioned=False,
                skip_reason=f"unhandled: {type(exc).__name__}: {exc}",
            )
        actions.append(action)

        # Decide whether this record is worth a log line. Always record an
        # ACTIONABLE decision (a trip is recommended — the calibration signal
        # we exist to capture, whether or not auto-trip actually fired) and any
        # CHANGE vs this bot's prior cycle; suppress the unchanged steady-state
        # no-op. The signature feeds the next cycle's change-detection and is
        # written for every bot — even suppressed ones — so a later transition
        # is logged exactly once.
        sig = _decision_signature(action)
        new_sigs[bot_id] = sig
        trip_recommended = bool(action.decision.get("trip"))
        changed = last_sigs.get(bot_id) != sig
        if runner_log_verbose or trip_recommended or changed:
            _append_runner_log(shared_dir, {
                "timestamp": _now_iso(),
                "bot_id": bot_id,
                "auto_trip_enabled": auto_trip_enabled,
                **asdict(action),
            })

    _write_last_decisions(shared_dir, new_sigs)

    finished_at = datetime.now(timezone.utc).isoformat()
    return RunnerResult(
        started_at=started_at,
        finished_at=finished_at,
        auto_trip_enabled=auto_trip_enabled,
        actions=actions,
    )


def _process_one_bot(
    *,
    shared_dir: Path,
    network: dict,
    bot_id: str,
    detector_config: DetectorConfig,
    eval_start: datetime,
    eval_end: datetime,
    load_start: datetime,
    auto_trip_enabled: bool,
    now: datetime,
    trip_fn: Callable[..., Any],
    enforce_trip_fn: Callable[..., Any],
    read_trip_fn: Callable[..., Any],
    list_active_fn: Callable[..., Any],
    dispatcher_send_fn: Callable[..., Any],
    signal_observe_fn: Callable[..., Any],
) -> RunnerAction:
    """Evaluate one bot and (if conditions hold) act on the decision."""
    # 1) Load turn data for the bot.
    turns = _read_turns_for_bot(
        shared_dir, bot_id, since=load_start, until=eval_end,
    )

    # 2) Compute baseline + evaluate window.
    baseline = compute_baseline(bot_id=bot_id, turns=turns, as_of=eval_end)
    decision = evaluate_window(
        bot_id=bot_id, turns=turns,
        window_start=eval_start, window_end=eval_end,
        baseline=baseline, config=detector_config,
    )

    serialized_decision = {
        "trip": decision.trip,
        "reason": decision.reason,
        "window_start": decision.window_start.isoformat(),
        "window_end": decision.window_end.isoformat(),
        "metrics": decision.metrics,
    }

    # 3) If no trip recommended, we're done.
    if not decision.trip:
        return RunnerAction(
            bot_id=bot_id,
            decision=serialized_decision,
            actioned=False,
            skip_reason="detector did not recommend trip",
        )

    # 4) Trip recommended. Three guards before we actually fire:
    #    a) auto_trip_enabled must be on (observe-only otherwise).
    #    b) No active trip for this (bot, RUNNER_BREAKER_TYPE).
    #    c) (No further guards — we DO allow immediate re-trip after
    #       a clear, per spec §9 decision 3, but observe a recurrent
    #       signal in that case.)
    if not auto_trip_enabled:
        return RunnerAction(
            bot_id=bot_id,
            decision=serialized_decision,
            actioned=False,
            skip_reason="observe-only mode (breakers.auto_trip_enabled is false)",
        )

    existing = read_trip_fn(shared_dir, bot_id, RUNNER_BREAKER_TYPE)
    if existing is not None:
        return RunnerAction(
            bot_id=bot_id,
            decision=serialized_decision,
            actioned=False,
            skip_reason="already tripped — idempotent skip",
            trip_id=existing.trip_id,
        )

    # 5) Recurrent check — read audit log for prior auto-trips on
    #    this (bot, type) within the last 48h.
    recurrent = _was_recently_auto_tripped(
        shared_dir, bot_id, RUNNER_BREAKER_TYPE, now=now,
    )

    # 6) Trip.
    record = trip_fn(
        shared_dir=shared_dir,
        scope=bot_id,
        breaker_type=RUNNER_BREAKER_TYPE,
        duration=timedelta(hours=24),
        initiated_by="auto",
        reason=decision.reason,
    )

    # 7) Enforce — for cost this is a no-op until Phase 3b's plugin
    #    reads the file, but the call shape is identical and the
    #    result is recorded so the runner-log is honest.
    enforce_result = enforce_trip_fn(
        scope=bot_id, breaker_type=RUNNER_BREAKER_TYPE, network=network,
        reason=record.reason, expires_at_iso=record.expires_at,
    )

    # 8) Notify the sysadmin via the alerts catalog event. Best-effort:
    #    a notification failure must never roll back the trip. Routes
    #    through `cost.breaker_tripped` so it (a) lands as a CRITICAL-
    #    severity message with a ⚡ + bold formatting that the operator
    #    can't miss in Telegram, (b) respects the operator's catalog
    #    subscription (default IMMEDIATE / enabled), and (c) records the
    #    breaker trip in the alerts subscription/audit pipeline. The
    #    earlier text-only WARNING-severity dispatch landed as just
    #    another paragraph in the chat — this is the visibility fix from
    #    the 2026-05-28 Security_bot trip incident.
    notified = False
    try:
        from evolve_admin.alerts.dispatcher import Severity as _Severity
        # Build the payload that cost.breaker_tripped's body_template
        # renders. Keep keys aligned with the catalog sample_payload.
        expires_iso = record.expires_at or ""
        expires_short = _format_expires_short(expires_iso)
        duration_hours = _trip_duration_hours(record)
        payload = {
            "bot_id": bot_id,
            "breaker_type": RUNNER_BREAKER_TYPE,
            "reason": decision.reason or "",
            "expires_at_short": expires_short,
            "duration_hours": duration_hours,
            "trip_id_short": record.trip_id[:8],
        }
        outcome = dispatcher_send_fn(
            shared_dir=shared_dir,
            network=network,
            source="breakers_runner",
            severity=_Severity.CRITICAL,
            catalog_event="cost.breaker_tripped",
            payload=payload,
            dedup_key=(
                f"breakers_runner:{bot_id}:{RUNNER_BREAKER_TYPE}:"
                f"{record.trip_id}"
            ),
        )
        # DispatchOutcome uses `result == SENT` rather than `ok`.
        from evolve_admin.alerts.dispatcher import DispatchResult as _DR
        notified = getattr(outcome, "result", None) == _DR.SENT
    except Exception as exc:  # noqa: BLE001
        log.warning("breakers.runner: dispatcher.send failed: %s", exc)

    # 9) Recurrent signal — observe AFTER the trip is recorded so the
    #    causal arrow points back to a real trip_id the admin can find.
    if recurrent:
        try:
            signature = f"breakers_runner:recurrent_breaker_trip:{bot_id}:{RUNNER_BREAKER_TYPE}"
            signal_observe_fn(
                shared_dir,
                signature=signature,
                producer="breakers_runner",
                type="recurrent_breaker_trip",
                flavor="cost",
                scope="bot",
                bot_id=bot_id,
                title=f"Recurrent auto-trip on {bot_id}/{RUNNER_BREAKER_TYPE}",
                body=(
                    f"This is the second auto-trip on {bot_id}/{RUNNER_BREAKER_TYPE} "
                    f"within {RECURRENT_TRIP_WINDOW_HOURS}h. The trip is in effect, "
                    f"but the recurring pattern suggests a root cause that needs "
                    f"investigation rather than another reset. "
                    f"Latest reason: {decision.reason}"
                ),
                details={
                    "bot_id": bot_id,
                    "breaker_type": RUNNER_BREAKER_TYPE,
                    "trip_id": record.trip_id,
                    "window_hours": RECURRENT_TRIP_WINDOW_HOURS,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("breakers.runner: recurrent signal observe failed: %s", exc)

    return RunnerAction(
        bot_id=bot_id,
        decision=serialized_decision,
        actioned=True,
        trip_id=record.trip_id,
        enforce_ok=bool(getattr(enforce_result, "ok", False)),
        notified=notified,
        recurrent=recurrent,
    )


def _format_expires_short(expires_at_iso: str) -> str:
    """Render an ISO-8601 expiry into a human-friendly Telegram-safe short form.

    Used in the cost.breaker_tripped catalog payload as ``{expires_at_short}``.
    Falls back to ``"indefinite"`` on parse failure or empty input — the
    catalog body template appends "UTC" after the placeholder so the time
    zone is unambiguous either way.
    """
    if not expires_at_iso:
        return "indefinite"
    try:
        dt = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "indefinite"
    # YYYY-MM-DD HH:MM — explicit date + minute precision. Trips routinely
    # span a UTC date boundary so "tomorrow at HH:MM" would be ambiguous;
    # the absolute form is unambiguous and fits the message line.
    return dt.strftime("%Y-%m-%d %H:%M")


def _trip_duration_hours(record: Any) -> int:
    """Compute hours between trip-record's tripped_at and expires_at.

    Used in the cost.breaker_tripped catalog payload as ``{duration_hours}``.
    Falls back to 24 on any parse/missing-field failure — that's the
    runner's default trip duration and is the right operator-facing
    value to surface when the exact field isn't readable.
    """
    try:
        tripped_at = getattr(record, "tripped_at", None) or _now_iso()
        expires_at = getattr(record, "expires_at", None) or ""
        if not expires_at:
            return 24
        t0 = datetime.fromisoformat(str(tripped_at).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        delta = t1 - t0
        return max(1, round(delta.total_seconds() / 3600))
    except (ValueError, AttributeError, TypeError):
        return 24


# ── CLI ──────────────────────────────────────────────────────────────────────


def _load_network(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"breakers.runner: network.json read failed: {exc}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="breakers.runner",
        description=(
            "Run the activity-shape detector against the pod's recent "
            "turn data. Logs decisions to {shared_dir}/breakers/runner-log/. "
            "Acts on trips ONLY when network.json::breakers.auto_trip_enabled "
            "is true (default: false — observe-only)."
        ),
    )
    parser.add_argument(
        "--shared-dir", type=Path, default=CANONICAL_SHARED_DIR,
        help="Pod shared dir (default: platform-keyed canonical shared dir)",
    )
    parser.add_argument(
        "--network", type=Path, default=resolve_network_path(),
        help="Path to network.json (default: platform-keyed canonical path)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single cycle and exit (default).",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run continuously, sleeping --interval-seconds between cycles.",
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=600,
        help="Sleep between cycles in daemon mode (default 600 = 10 min).",
    )
    parser.add_argument(
        "--window-hours", type=float, default=DEFAULT_EVAL_WINDOW_HOURS,
        help="Evaluation window in hours (default 1.0).",
    )
    args = parser.parse_args(argv)

    network = _load_network(args.network)

    def _one_cycle() -> None:
        result = run_once(
            shared_dir=args.shared_dir,
            network=network,
            eval_window_hours=args.window_hours,
        )
        # Brief stderr summary so cron mail / launchd logs are informative.
        n_actioned = sum(1 for a in result.actions if a.actioned)
        n_would_trip = sum(
            1 for a in result.actions
            if a.decision.get("trip") and not a.actioned
        )
        print(
            f"[breakers.runner] auto_trip_enabled="
            f"{result.auto_trip_enabled} "
            f"actioned={n_actioned} would_trip={n_would_trip} "
            f"bots={len(result.actions)}",
            file=sys.stderr,
        )

    if args.daemon:
        import time
        while True:
            try:
                _one_cycle()
            except Exception as exc:  # noqa: BLE001
                print(f"[breakers.runner] cycle error: {exc}", file=sys.stderr)
            time.sleep(args.interval_seconds)
    else:
        _one_cycle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
