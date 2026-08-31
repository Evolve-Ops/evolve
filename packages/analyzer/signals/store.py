"""signals.store — Filesystem-backed Signal persistence.

Spec: internal/spec-alerts-signal-store-2026-05-07.md §7.

Signals live under ``{shared_dir}/signals/{subdir}/{signal_id}.json``
where ``subdir`` is one of:

  - ``firing``    — actively firing
  - ``snoozed``   — deferred until ``snoozed_until``
  - ``archived``  — terminal (resolved | dismissed)

The state field on the Signal JSON is authoritative; the subdir is a
physical index for efficient iteration.

Public entry points:

  - :func:`observe` — find-or-create with dedup by signature, plus
    re-open of recently-resolved signals within a configurable window
  - :func:`iter_active` — yield firing/snoozed signals, optional filter
  - :func:`find_signal` — locate one by id across subdirs
  - :func:`apply_transition` — wrap state_machine.transition() with
    file move
  - :func:`sweep_resolve` — bulk auto-resolve for sweep-style monitors
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso_offset as _utc_now_iso

from schema.signal import (
    Category,
    Delivery,
    Flavor,
    Remediation,
    Scope,
    Severity,
    Signal,
    State,
    StateTransition,
    new_signal_id,
)
from signals.state_machine import IllegalTransitionError, transition
from store_lock import StoreLockTimeout, locked as _flock  # noqa: F401 — re-exported


Subdir = Literal["firing", "snoozed", "archived"]

# Outcome of an observe() call — which find-or-create branch was taken.
# Consumers (e.g. the audit poller) branch on this to decide whether a
# re-emission carries new forensic value:
#   created        → a fresh firing Signal was minted (novel)
#   reopened       → a recently-resolved Signal was re-opened (state change)
#   changed        → an already-firing Signal was bumped AND its severity
#                    moved (an escalation/de-escalation worth keeping)
#   unchanged      → an already-firing Signal was bumped with NO severity
#                    change (a pure dedup-hit — no new information)
#   dismissed_bump → a dismissed Signal's count was bumped in place
#                    (operator said "don't tell me again"; no new firing)
ObserveOutcome = Literal[
    "created", "reopened", "changed", "unchanged", "dismissed_bump"
]

_STATE_TO_SUBDIR: dict[State, Subdir] = {
    "firing": "firing",
    "snoozed": "snoozed",
    "resolved": "archived",
    "dismissed": "archived",
}

_ALL_SUBDIRS: tuple[Subdir, ...] = ("firing", "snoozed", "archived")
_ACTIVE_SUBDIRS: tuple[Subdir, ...] = ("firing", "snoozed")

DEFAULT_REOPEN_WINDOW_SECONDS = 3600  # spec §6

# mtime floor for the dismissed-signature lookup in observe(). Matches
# the archive retention window (signals.retention prunes archived/ at
# 90 days), so the prefilter never changes which entries are findable —
# it only skips JSON-loading files that retention is about to delete.
DISMISSED_LOOKUP_WINDOW_DAYS = 90

# Title hygiene. Titles render in the Alerts page summary row alongside
# severity dot, producer chip, scope chip, state badge, and timestamp —
# anything over ~80 chars wraps awkwardly and pushes the action chips
# off-screen on narrow viewports. Hard cap at 120 with truncation so a
# misbehaving producer can't make the row unusable. The original is kept
# in ``details["full_title"]`` so the expanded row can still show it.
#
# Reference offender (pre-fix): cost_watchdog.detect_config_drift embedded
# JSON-dumped before/after values inline — a 6-entry model fallback list
# change blew past 300 chars.
TITLE_SOFT_LIMIT = 80
TITLE_HARD_LIMIT = 120

_log = logging.getLogger(__name__)


def _clamp_title(title: str, payload: dict[str, Any], *, producer: str) -> str:
    """Truncate ``title`` to TITLE_HARD_LIMIT; stash the original on the payload.

    Mutates ``payload`` in place (adds ``full_title`` when truncated). Logs
    a warning at TITLE_SOFT_LIMIT so producers see the noise during dev
    even before the hard cap trips. No-op when ``title`` is empty.
    """
    if not title:
        return title
    if len(title) > TITLE_SOFT_LIMIT:
        _log.warning(
            "signals.observe: title from producer=%s exceeds soft limit "
            "(%d > %d chars). Move structured payload to details/body. "
            "Title: %r",
            producer,
            len(title),
            TITLE_SOFT_LIMIT,
            title[:200],
        )
    if len(title) <= TITLE_HARD_LIMIT:
        return title
    payload["full_title"] = title
    return title[: TITLE_HARD_LIMIT - 1].rstrip() + "…"


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────


def signals_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "signals"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-process locking (spec-state-store-and-deploy-resilience §1.4)
# ─────────────────────────────────────────────────────────────────────────────
#
# Every find-then-act / read-modify-write sequence in this module holds
# the store lock; plain readers stay lock-free (per-file atomic writes
# mean they never see torn JSON). retention.py and backfill.py
# deliberately do NOT take the lock: retention's mtime pruning can't
# overlap the 1h reopen window, backfill is a one-shot manual tool, and
# the worst interleaving (resurrecting a being-pruned dismissed bump)
# is benign and self-heals on the next retention pass.
#
# Lock ordering invariant: proposals → signals only. arbiter.store
# calls into this module (signal backrefs) while holding its own lock;
# this module must NEVER call into arbiter.store.
#
# The bulk sweeps (sweep_resolve, wake_due_snoozes) deliberately lock
# PER ITEM via apply_transition's internal lock, not across the whole
# iteration: bounded hold times, and the per-item CAS reload detects
# any item that a concurrent actor transitioned mid-sweep. Don't "fix"
# this by wrapping the loops.

LOCK_FILE_NAME = ".store.lock"


def lock_path(shared_dir: Path) -> Path:
    return signals_root(shared_dir) / LOCK_FILE_NAME


def locked(shared_dir: Path):
    """Public critical-section guard for callers whose check-then-write
    spans multiple store calls. Reentrant — the store's own locking
    nests inside it."""
    return _flock(lock_path(shared_dir))


def signal_path(shared_dir: Path, signal_id: str, *, subdir: Subdir) -> Path:
    return signals_root(shared_dir) / subdir / f"{signal_id}.json"


def subdir_for_state(state: State) -> Subdir:
    return _STATE_TO_SUBDIR[state]


def feedback_log_path(shared_dir: Path) -> Path:
    """Path to the rejected-proposal feedback log (spec §9)."""
    return signals_root(shared_dir) / "feedback.jsonl"


def state_change_log_path(shared_dir: Path, *, day: str | None = None) -> Path:
    """Path to the append-only state-change log for ``day`` (spec §7).

    ``day`` is an ISO date string (``YYYY-MM-DD``); defaults to UTC today.
    External tooling — e.g. ``scripts/evolve_liveness_external.py`` — uses
    the mtime of today's file as the liveness signal for the signal store.
    """
    if day is None:
        day = _utc_now().date().isoformat()
    return signals_root(shared_dir) / "log" / f"{day}.jsonl"


def _append_state_change_log(
    shared_dir: Path,
    *,
    signal: Signal,
    from_state: State | None,
    to_state: State,
    actor: str,
    reason: str,
    at: str | None = None,
) -> None:
    """Append one state-change record to ``signals/log/<YYYY-MM-DD>.jsonl``.

    Spec §7 storage layout. The record is self-contained (carries producer,
    signature, scope, bot_id) so the log is useful without joining back to
    the Signal file — important once Signals get pruned out of ``archived/``.

    Single-line JSON appends < PIPE_BUF are atomic under POSIX, so no
    temp-file dance is needed. Failures are swallowed: the audit log is
    secondary to the live store, and a disk-full / ENOSPC condition will
    already surface via the rest of the pipeline.
    """
    record = {
        "at": at or _utc_now_iso(),
        "signal_id": signal.id,
        "signature": signal.signature,
        "producer": signal.producer,
        "type": signal.type,
        "scope": signal.scope,
        "bot_id": signal.bot_id,
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "reason": reason,
    }
    path = state_change_log_path(shared_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Read / write primitives
# ─────────────────────────────────────────────────────────────────────────────


def write_signal(
    signal: Signal,
    shared_dir: Path,
    *,
    subdir: Subdir | None = None,
) -> Path:
    """Write a Signal to its state-derived subdir.

    Pass ``subdir`` to override (rare — used by :func:`apply_transition`
    when moving across subdirs).
    """
    target: Subdir = subdir or subdir_for_state(signal.state)
    path = signal_path(shared_dir, signal.id, subdir=target)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mode=0o644: Signals are written by many producers (cost_watchdog,
    # permission_monitor, etc., often running as different user contexts)
    # and consumed by the admin UI, alert notifier, and sweep daemons.
    # The default 0o600 from ``tempfile.mkstemp`` silently breaks the
    # consumers — see ``arbiter.store`` for the same cross-user-read
    # rationale.
    _atomic_write_json(path, signal.to_dict(), mode=0o644)
    return path


def load_signal_file(path: Path) -> Signal | None:
    """Load a Signal from a specific path, or None if unreadable."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Signal.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def find_signal(
    shared_dir: Path, signal_id: str
) -> tuple[Signal, Path, Subdir] | None:
    """Locate a Signal by id across subdirs."""
    for sd in _ALL_SUBDIRS:
        path = signal_path(shared_dir, signal_id, subdir=sd)
        sig = load_signal_file(path)
        if sig is not None:
            return sig, path, sd
    return None


def delete_signal(
    shared_dir: Path, signal_id: str, *, subdir: Subdir
) -> bool:
    """Remove a Signal file from a subdir."""
    path = signal_path(shared_dir, signal_id, subdir=subdir)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Iteration
# ─────────────────────────────────────────────────────────────────────────────


def iter_signals(
    shared_dir: Path,
    *,
    subdirs: tuple[Subdir, ...] = _ACTIVE_SUBDIRS,
) -> Iterator[Signal]:
    """Yield every readable Signal from the given subdirs (sorted by id)."""
    root = signals_root(shared_dir)
    for sd in subdirs:
        dir_path = root / sd
        if not dir_path.exists():
            continue
        for path in sorted(dir_path.glob("*.json")):
            sig = load_signal_file(path)
            if sig is not None:
                yield sig


# Severity ordering for ``min_severity`` filter — higher index = more urgent.
_SEVERITY_RANK: dict[str, int] = {"info": 0, "warn": 1, "alert": 2}


def iter_active(
    shared_dir: Path,
    *,
    producer: str | None = None,
    flavor: Flavor | None = None,
    severity: Severity | None = None,
    min_severity: Severity | None = None,
    scope: Scope | None = None,
    bot_id: str | None = None,
    state: State | None = None,
    category: Category | None = None,
) -> Iterator[Signal]:
    """Yield Signals in firing/snoozed subdirs, optionally filtered.

    A filter argument of ``None`` means "no constraint on this field."
    Use this from contextual UI surfaces, e.g.::

        signals.iter_active(scope="integration", bot_id="admin_bot")

    ``severity`` is exact match (info | warn | alert). ``min_severity``
    is a floor — ``min_severity="warn"`` returns warn + alert and hides
    info. The Alerts page uses min_severity="warn" by default so info-tier
    advisories don't take up screen space until the operator toggles them
    in. See producer_severity.py for the per-producer policy.
    """
    floor: int | None = (
        _SEVERITY_RANK.get(min_severity) if min_severity is not None else None
    )
    for sig in iter_signals(shared_dir, subdirs=_ACTIVE_SUBDIRS):
        if producer is not None and sig.producer != producer:
            continue
        if flavor is not None and sig.flavor != flavor:
            continue
        if severity is not None and sig.severity != severity:
            continue
        if floor is not None and _SEVERITY_RANK.get(sig.severity, 0) < floor:
            continue
        if scope is not None and sig.scope != scope:
            continue
        if bot_id is not None and sig.bot_id != bot_id:
            continue
        if state is not None and sig.state != state:
            continue
        if category is not None and sig.category != category:
            continue
        yield sig


def find_active_by_signature(
    shared_dir: Path, signature: str
) -> Signal | None:
    """Return the firing-or-snoozed Signal with this signature, if any."""
    for sig in iter_signals(shared_dir, subdirs=_ACTIVE_SUBDIRS):
        if sig.signature == signature:
            return sig
    return None


def _find_dismissed_by_signature(
    shared_dir: Path, signature: str
) -> Signal | None:
    """Return the most-recently-dismissed Signal with this signature, if any.

    Companion to :func:`_find_recent_resolved_by_signature` for the
    dismissed branch. No time window: the operator's "don't tell me
    again" is permanent unless they re-open the Signal from the UI.

    Returning the most recent dismissed entry lets ``observe()`` bump
    its ``last_observed_at`` (so the Alerts page can show "still
    observed — dismissed N times since first dismissal") without
    creating a fresh sibling Signal that the notifier would page on.

    Cost bound: this scan runs inside the store lock on every
    new-signature observe, so it's stat-prefiltered to the retention
    window (a live dismissed entry gets its mtime bumped on every
    observe; anything older than the archive retention is about to be
    pruned anyway) and loads newest-first with early exit on match —
    the match case touches one file instead of the whole archive.
    """
    archived_dir = signals_root(shared_dir) / "archived"
    if not archived_dir.exists():
        return None
    cutoff_mtime = (
        _utc_now() - timedelta(days=DISMISSED_LOOKUP_WINDOW_DAYS)
    ).timestamp()
    candidates: list[tuple[float, Path]] = []
    for path in archived_dir.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_mtime:
            continue
        candidates.append((mtime, path))
    for _mtime, path in sorted(candidates, reverse=True):
        sig = load_signal_file(path)
        if sig is None or sig.signature != signature:
            continue
        if sig.state != "dismissed":
            continue
        return sig
    return None


def _find_recent_resolved_by_signature(
    shared_dir: Path,
    signature: str,
    *,
    reopen_window_seconds: int,
) -> Signal | None:
    """Return a resolved Signal with this signature inside the re-open window.

    Dismissed signals are not eligible for re-open — the user said
    "don't tell me again." Resolved signals are: the condition cleared,
    so seeing it return is a continuation of the same incident.
    """
    cutoff = _utc_now() - timedelta(seconds=reopen_window_seconds)
    archived_dir = signals_root(shared_dir) / "archived"
    if not archived_dir.exists():
        return None
    cutoff_mtime = cutoff.timestamp()
    for path in archived_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue
        sig = load_signal_file(path)
        if sig is None or sig.signature != signature:
            continue
        if sig.state != "resolved":
            continue
        resolved_at = _parse_iso(sig.resolved_at)
        if resolved_at is None or resolved_at < cutoff:
            continue
        return sig
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Find-or-create (the main producer entry point)
# ─────────────────────────────────────────────────────────────────────────────


def observe(
    shared_dir: Path,
    *,
    signature: str,
    producer: str,
    type: str,
    flavor: Flavor | None = None,
    severity: Severity | None = None,
    scope: Scope,
    bot_id: str | None = None,
    category: Category | None = None,
    title: str = "",
    body: str = "",
    details: dict[str, Any] | None = None,
    config_hint: dict[str, Any] | None = None,
    remediation: Remediation | None = None,
    incident_key: str | None = None,
    caused_by_signal_id: str | None = None,
    actor: str | None = None,
    reopen_window_seconds: int = DEFAULT_REOPEN_WINDOW_SECONDS,
) -> Signal:
    """Find-or-create a Signal by signature.

    Behavior:

    1. Active Signal (firing|snoozed) with this signature exists →
       bump ``observation_count`` and ``last_observed_at``, merge new
       ``details`` into the existing payload, refresh ``severity`` (a
       producer can escalate by re-observing at higher severity), and
       return the existing Signal.

    2. No active Signal, but a *resolved* one with this signature
       within ``reopen_window_seconds`` → re-open it (resolved →
       firing). Preserves history; treats the recurrence as a
       continuation of the same incident. Dismissed signals are NOT
       re-opened — the user said "don't tell me again."

    2b. No active Signal, no recently-resolved one, but a *dismissed*
        Signal with this signature exists → bump
        ``observation_count`` / ``last_observed_at`` on the dismissed
        entry in place. Do NOT create a new Signal, do NOT change
        state. Honours the operator's "don't tell me again" by
        suppressing the notification that would otherwise come from a
        freshly-minted sibling Signal. The operator regains visibility
        by explicitly re-opening the dismissed Signal from the Alerts
        UI (transition dismissed → firing).

    3. Otherwise → create a fresh firing Signal.

    ``severity`` and ``flavor`` are optional. When omitted, each falls back
    to the central default for the producer (see
    ``producer_severity.default_severity`` / ``default_flavor``). Pass an
    explicit value only when a specific finding diverges from the producer's
    normal level — that pattern lets producers concentrate their
    default-severity/flavor policy in one file instead of sprinkling it
    across every observe() call.

    ``actor`` defaults to ``producer`` for the create/re-open
    transition entry. Pass an explicit value to attribute the write to
    a daemon, a user action, etc.
    """
    if severity is None or flavor is None:
        from .producer_severity import default_flavor, default_severity
        if severity is None:
            severity = default_severity(producer)
        if flavor is None:
            flavor = default_flavor(producer)
    signal, _outcome = observe_with_outcome(
        shared_dir,
        signature=signature,
        producer=producer,
        type=type,
        flavor=flavor,
        severity=severity,
        scope=scope,
        bot_id=bot_id,
        category=category,
        title=title,
        body=body,
        details=details,
        config_hint=config_hint,
        remediation=remediation,
        incident_key=incident_key,
        caused_by_signal_id=caused_by_signal_id,
        actor=actor,
        reopen_window_seconds=reopen_window_seconds,
    )
    return signal


def observe_with_outcome(
    shared_dir: Path,
    *,
    signature: str,
    producer: str,
    type: str,
    flavor: Flavor | None = None,
    severity: Severity | None = None,
    scope: Scope,
    bot_id: str | None = None,
    category: Category | None = None,
    title: str = "",
    body: str = "",
    details: dict[str, Any] | None = None,
    config_hint: dict[str, Any] | None = None,
    remediation: Remediation | None = None,
    incident_key: str | None = None,
    caused_by_signal_id: str | None = None,
    actor: str | None = None,
    reopen_window_seconds: int = DEFAULT_REOPEN_WINDOW_SECONDS,
) -> tuple[Signal, ObserveOutcome]:
    """Same as :func:`observe`, but also returns which find-or-create
    branch was taken (see :data:`ObserveOutcome`).

    Callers that want to suppress redundant work on a pure dedup-hit
    (e.g. the audit poller deciding archive-vs-delete) branch on the
    second element. The Signal write semantics are identical to
    ``observe`` — this only surfaces the branch, it does not change
    what is persisted.
    """
    if severity is None or flavor is None:
        from .producer_severity import default_flavor, default_severity
        if severity is None:
            severity = default_severity(producer)
        if flavor is None:
            flavor = default_flavor(producer)
    actor_str = actor or producer
    payload = dict(details or {})
    title = _clamp_title(title, payload, producer=producer)

    # The full find-or-create sequence holds the store lock: two
    # concurrent observers of one signature must serialize, or both
    # miss the active-signal lookup and create duplicates (and the
    # bump branch's read-modify-write loses counts).
    with _flock(lock_path(shared_dir)):
        return _observe_locked(
            shared_dir,
            signature=signature,
            producer=producer,
            type=type,
            flavor=flavor,
            severity=severity,
            scope=scope,
            bot_id=bot_id,
            category=category,
            title=title,
            body=body,
            payload=payload,
            config_hint=config_hint,
            remediation=remediation,
            incident_key=incident_key,
            caused_by_signal_id=caused_by_signal_id,
            actor_str=actor_str,
            reopen_window_seconds=reopen_window_seconds,
        )


def _observe_locked(
    shared_dir: Path,
    *,
    signature: str,
    producer: str,
    type: str,
    flavor: Flavor,
    severity: Severity,
    scope: Scope,
    bot_id: str | None,
    category: Category | None,
    title: str,
    body: str,
    payload: dict[str, Any],
    config_hint: dict[str, Any] | None,
    remediation: Remediation | None,
    incident_key: str | None,
    caused_by_signal_id: str | None,
    actor_str: str,
    reopen_window_seconds: int,
) -> tuple[Signal, ObserveOutcome]:
    """Body of :func:`observe`. Caller holds the store lock.

    Returns ``(signal, outcome)`` where ``outcome`` names the branch
    taken (see :data:`ObserveOutcome`).
    """
    # 1) Existing active signal — bump
    existing = find_active_by_signature(shared_dir, signature)
    if existing is not None:
        # An already-firing signal: the bump carries NEW information only
        # if the severity moved (escalation/de-escalation). Otherwise this
        # is a pure dedup-hit — same condition, re-observed.
        severity_changed = existing.severity != severity
        existing.last_observed_at = _utc_now_iso()
        existing.observation_count += 1
        existing.severity = severity  # producer can escalate
        if payload:
            existing.details.update(payload)
        if title:
            existing.title = title
            # If the new title fit under the hard cap, drop any stale
            # full_title carried over from a prior over-limit observation.
            if "full_title" not in payload:
                existing.details.pop("full_title", None)
        if body:
            existing.body = body
        if config_hint is not None:
            existing.config_hint = config_hint
        if remediation is not None:
            existing.remediation = remediation
        if incident_key is not None:
            existing.incident_key = incident_key
        if caused_by_signal_id is not None:
            existing.caused_by_signal_id = caused_by_signal_id
        if category is not None:
            existing.category = category
        write_signal(existing, shared_dir)
        return existing, ("changed" if severity_changed else "unchanged")

    # 2) Recently resolved — re-open
    if reopen_window_seconds > 0:
        recent = _find_recent_resolved_by_signature(
            shared_dir, signature, reopen_window_seconds=reopen_window_seconds
        )
        if recent is not None:
            transition(
                recent,
                "firing",
                actor=actor_str,
                reason="reopen within window",
            )
            recent.last_observed_at = _utc_now_iso()
            recent.observation_count += 1
            recent.severity = severity
            if payload:
                recent.details.update(payload)
            if title:
                recent.title = title
                if "full_title" not in payload:
                    recent.details.pop("full_title", None)
            if body:
                recent.body = body
            if config_hint is not None:
                recent.config_hint = config_hint
            if remediation is not None:
                recent.remediation = remediation
            if incident_key is not None:
                recent.incident_key = incident_key
            if caused_by_signal_id is not None:
                recent.caused_by_signal_id = caused_by_signal_id
            if category is not None:
                recent.category = category
            # Was in archived/; write to firing/ then remove the archived copy
            write_signal(recent, shared_dir, subdir="firing")
            old_path = signal_path(shared_dir, recent.id, subdir="archived")
            if old_path.exists():
                old_path.unlink()
            _append_state_change_log(
                shared_dir,
                signal=recent,
                from_state="resolved",
                to_state="firing",
                actor=actor_str,
                reason="reopen within window",
            )
            return recent, "reopened"

    # 2c) Previously dismissed — bump silently, do NOT create a fresh
    # Signal. The operator's "dismiss" used to behave like a reset: the
    # archived dismissed entry was invisible to step 1 (only firing/
    # snoozed are "active") and step 2 (only "resolved" is eligible for
    # re-open), so every subsequent observe() of the same signature
    # created a new Signal id, which signal_notifier paged on as if it
    # were a fresh event. Dismissing thus had the OPPOSITE effect from
    # what the operator wanted — it amounted to "reset and re-page".
    #
    # Now we recognise the dismissed entry and update its last_observed_
    # at + observation_count in place. The Alerts page can show "still
    # observed since dismissal" if it wants to surface that the
    # condition persists; the dispatcher stays silent because no new
    # firing Signal materialises. Operator regains control via the
    # Alerts page's "re-open" action (the existing transition firing
    # ← dismissed path), which is the explicit "I want to hear about
    # this again" gesture.
    dismissed = _find_dismissed_by_signature(shared_dir, signature)
    if dismissed is not None:
        dismissed.last_observed_at = _utc_now_iso()
        dismissed.observation_count += 1
        if payload:
            dismissed.details.update(payload)
        # title/body/severity intentionally NOT refreshed — the dismissed
        # snapshot reflects what the operator chose to dismiss. Re-
        # opening from the UI is the path to "see the latest framing."
        write_signal(dismissed, shared_dir, subdir="archived")
        return dismissed, "dismissed_bump"

    # 3) New
    sig = Signal(
        id=new_signal_id(),
        signature=signature,
        producer=producer,
        type=type,
        flavor=flavor,
        severity=severity,
        scope=scope,
        bot_id=bot_id,
        category=category,  # None → derived from producer in __post_init__
        title=title,
        body=body,
        details=payload,
        config_hint=config_hint,
        remediation=remediation,
        incident_key=incident_key,
        caused_by_signal_id=caused_by_signal_id,
    )
    created_at = _utc_now_iso()
    sig.state_history.append(
        StateTransition(
            from_state=None,
            to_state="firing",
            at=created_at,
            actor=actor_str,
            reason="created",
        )
    )
    write_signal(sig, shared_dir)
    _append_state_change_log(
        shared_dir,
        signal=sig,
        from_state=None,
        to_state="firing",
        actor=actor_str,
        reason="created",
        at=created_at,
    )
    return sig, "created"


# ─────────────────────────────────────────────────────────────────────────────
# Transitions with file move
# ─────────────────────────────────────────────────────────────────────────────


def apply_transition(
    signal: Signal,
    to_state: State,
    shared_dir: Path,
    *,
    actor: str,
    reason: str = "",
    snoozed_until: str | None = None,
) -> Path:
    """Transition a Signal's state and rewrite it to the right subdir.

    For ``firing → snoozed`` the caller must pass ``snoozed_until``
    (ISO8601 UTC) — the helper sets it on the Signal before recording
    the transition. The state-machine module enforces legal
    transitions.

    Concurrency (spec §1.4): the caller's ``signal`` object may be
    stale — it was loaded outside any lock. Under the store lock this
    helper re-loads the Signal by id and applies the transition to the
    fresh on-disk record (the mutation inputs — ``to_state``, ``actor``,
    ``reason``, ``snoozed_until`` — are all arguments, so the caller's
    copy is only identity). A transition that lost a race is therefore
    *detected* (``IllegalTransitionError`` from the fresh state) rather
    than silently clobbering a concurrent update. The caller's object
    is refreshed in place on success.

    Returns the new file path.
    """
    if to_state == "snoozed" and not snoozed_until:
        raise ValueError(
            "apply_transition: snoozed_until is required when to_state='snoozed'"
        )

    with _flock(lock_path(shared_dir)):
        located = find_signal(shared_dir, signal.id)
        if located is not None:
            fresh, _path, _subdir = located
            # Refresh the caller's object so its reference stays valid
            # (plain dataclass — no __slots__).
            vars(signal).update(vars(fresh))

        if to_state == "snoozed":
            signal.snoozed_until = snoozed_until

        from_state: State = signal.state
        from_subdir = subdir_for_state(from_state)
        transition(signal, to_state, actor=actor, reason=reason)
        to_subdir = subdir_for_state(signal.state)

        # Write to new subdir; remove old file if path differs.
        new_path = write_signal(signal, shared_dir, subdir=to_subdir)
        if from_subdir != to_subdir:
            old_path = signal_path(shared_dir, signal.id, subdir=from_subdir)
            if old_path.exists():
                try:
                    old_path.unlink()
                except OSError:
                    pass
        _append_state_change_log(
            shared_dir,
            signal=signal,
            from_state=from_state,
            to_state=signal.state,
            actor=actor,
            reason=reason,
        )
        return new_path


# ─────────────────────────────────────────────────────────────────────────────
# Sweep-resolve (for comprehensive sweep-style monitors)
# ─────────────────────────────────────────────────────────────────────────────


def sweep_resolve(
    shared_dir: Path,
    *,
    producer: str,
    kept_signatures: set[str],
    actor: str | None = None,
    reason: str = "auto-resolve: condition cleared",
    types: set[str] | None = None,
    bot_ids: set[str] | None = None,
) -> list[Signal]:
    """Auto-resolve any active signal from this producer not in the keep set.

    Used by sweep-style monitors (pod_report, audit) that compute every
    finding on each run. After the run, signatures that appeared in the
    output are passed in ``kept_signatures``; this helper resolves the
    rest. Snoozed signals ARE resolved by sweep — if the condition no
    longer holds, the snooze is moot.

    ``types`` (optional): restrict the sweep to Signals whose ``type`` is
    in this set. Lets a partial-coverage runner sweep only the categories
    it actually checked. Default ``None`` keeps the old behavior of
    sweeping every Signal for ``producer``.

    ``bot_ids`` (optional, 2026-05-29): restrict the sweep to Signals
    whose ``bot_id`` is in this set. Required when the caller scanned
    only a subset of bots (e.g. ``--bot team_bot_a``) — without this filter,
    the sweep would mass-resolve every *other* bot's still-firing signals
    because ``kept_signatures`` only carries the scanned bot's
    signatures. Default ``None`` matches the old behavior of sweeping
    every bot for the matching producer.

    Returns the list of Signals that were resolved.
    """
    actor_str = actor or producer
    resolved: list[Signal] = []
    for sig in list(iter_signals(shared_dir, subdirs=_ACTIVE_SUBDIRS)):
        if sig.producer != producer:
            continue
        if types is not None and sig.type not in types:
            continue
        if bot_ids is not None and sig.bot_id not in bot_ids:
            continue
        if sig.signature in kept_signatures:
            continue
        try:
            apply_transition(
                sig,
                "resolved",
                shared_dir,
                actor=actor_str,
                reason=reason,
            )
            resolved.append(sig)
        except IllegalTransitionError:
            # Should not happen for active signals, but be defensive.
            continue
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# Snooze-wake (called by daemon at snoozed_until)
# ─────────────────────────────────────────────────────────────────────────────


def wake_due_snoozes(
    shared_dir: Path,
    *,
    now: datetime | None = None,
) -> list[Signal]:
    """Transition any snoozed Signal whose ``snoozed_until`` has passed.

    Returns the list of waked Signals. Intended to be called
    periodically by a snooze-wake daemon (analogue of arbiter's
    snooze_wake loop).
    """
    cutoff = (now or _utc_now())
    waked: list[Signal] = []
    for sig in list(iter_signals(shared_dir, subdirs=("snoozed",))):
        if not sig.snoozed_until:
            continue
        until = _parse_iso(sig.snoozed_until)
        if until is None or until > cutoff:
            continue
        try:
            apply_transition(
                sig,
                "firing",
                shared_dir,
                actor="timer",
                reason="snooze expired",
            )
            waked.append(sig)
        except IllegalTransitionError:
            continue
    return waked


# ─────────────────────────────────────────────────────────────────────────────
# Delivery audit
# ─────────────────────────────────────────────────────────────────────────────


def _refresh_from_disk(signal: Signal, shared_dir: Path) -> None:
    """Reload ``signal`` from disk in place, if a copy exists.

    Caller must hold the store lock. Used by the small RMW helpers
    (delivery audit, proposal backrefs) so a stale caller-loaded object
    doesn't clobber concurrent updates when rewritten.
    """
    located = find_signal(shared_dir, signal.id)
    if located is not None:
        fresh, _path, _subdir = located
        vars(signal).update(vars(fresh))


def record_delivery(
    signal: Signal,
    shared_dir: Path,
    *,
    channel: str,
    suppressed_reason: str | None = None,
) -> Path:
    """Append a Delivery entry and rewrite the Signal."""
    with _flock(lock_path(shared_dir)):
        _refresh_from_disk(signal, shared_dir)
        signal.deliveries.append(
            Delivery(
                channel=channel,
                at=_utc_now_iso(),
                suppressed_reason=suppressed_reason,
            )
        )
        return write_signal(signal, shared_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-link with Proposals (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────


def attach_proposal(
    signal: Signal,
    shared_dir: Path,
    *,
    proposal_id: str,
) -> Path:
    """Record that a Proposal was motivated by this Signal.

    The canonical link lives on Proposal.motivating_signals[]; this is
    the denormalized mirror for fast UI rendering ("→ N proposals" on
    the Signal detail card).
    """
    with _flock(lock_path(shared_dir)):
        _refresh_from_disk(signal, shared_dir)
        if proposal_id not in signal.motivated_proposals:
            signal.motivated_proposals.append(proposal_id)
        return write_signal(signal, shared_dir)


def detach_proposal(
    signal: Signal,
    shared_dir: Path,
    *,
    proposal_id: str,
) -> Path:
    """Inverse of :func:`attach_proposal`."""
    with _flock(lock_path(shared_dir)):
        _refresh_from_disk(signal, shared_dir)
        if proposal_id in signal.motivated_proposals:
            signal.motivated_proposals.remove(proposal_id)
        return write_signal(signal, shared_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Opik adapter (v1.5-1)
# ─────────────────────────────────────────────────────────────────────────────


def observe_from_opik(
    shared_dir: Path,
    span: Any,
    *,
    type: str,
    flavor: Flavor,
    producer: str | None = None,
    scope: Scope = "bot",
    severity: Severity | None = None,
    signature_suffix: str | None = None,
    title: str | None = None,
    body: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> Signal:
    """Produce a Signal from an :class:`observability.opik_client.OpikSpan`.

    Adapter for the v1.5 observability-first capture pipeline. Capture
    monitors (embedding_monitor, evolve_watchdog, reporter) call this
    instead of building the ``observe()`` kwargs themselves — the
    OpikSpan already carries ``bot_id``, ``producer``, ``error_info``,
    ``model``, etc., so the call site is a one-liner:

        signals_store.observe_from_opik(
            shared_dir, span,
            type="provider_failing",
            flavor="maintenance",
        )

    Field mapping:

      span.producer          → producer (override via ``producer=`` arg)
      span.bot_id            → bot_id
      span.error_info        → folded into details
      span.metadata          → folded into details under ``observability``
      span.attributes        → folded into details under ``observability``
      span.model/.provider   → folded into details
      span.tags              → folded into details under ``tags``

    Signature is ``producer:type:scope_key`` where ``scope_key``
    defaults to ``bot_id`` (or ``"pod"`` when no bot is set). Pass
    ``signature_suffix`` to disambiguate multiple categories of the
    same producer×type per bot (e.g. ``"openai/auth_failed"``).

    Returns the Signal returned by :func:`observe` — same find-or-create
    behavior including reopen-window logic.
    """
    # Import locally to avoid forcing observability import at module load.
    from observability import OpikSpan  # noqa: F401 — type annotation only

    prod = producer or span.producer or "observability"
    scope_key = span.bot_id or "pod"
    if signature_suffix:
        scope_key = f"{scope_key}/{signature_suffix}"

    from schema.signal import make_signature
    sig_str = make_signature(prod, type, scope_key)

    details: dict[str, Any] = {
        "observability": {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "name": span.name,
            "start_time": span.start_time.isoformat() if hasattr(span.start_time, "isoformat") else str(span.start_time),
            "end_time": span.end_time.isoformat() if hasattr(span.end_time, "isoformat") else str(span.end_time),
            "model": span.model,
            "provider": span.provider,
            "tags": list(span.tags or []),
            "metadata": dict(span.metadata or {}),
            "attributes": dict(span.attributes or {}),
        },
    }
    if span.is_error():
        details["error_info"] = dict(span.error_info or {})
    if extra_details:
        details.update(extra_details)

    return observe(
        shared_dir,
        signature=sig_str,
        producer=prod,
        type=type,
        flavor=flavor,
        severity=severity,
        scope=scope,
        bot_id=span.bot_id,
        title=title or _default_title_from_span(span, type),
        body=body or "",
        details=details,
    )


def _default_title_from_span(span: Any, type_: str) -> str:
    """Build a one-line UI title when the caller didn't supply one."""
    label = type_.replace("_", " ").capitalize()
    if span.bot_id:
        return f"{label} on {span.bot_id}"
    if span.provider:
        return f"{label} ({span.provider})"
    return label


def write_feedback(
    shared_dir: Path,
    *,
    signal_id: str,
    signal_signature: str,
    proposal_id: str,
    verdict: Literal["false_positive", "bad_inference", "not_actionable"],
    note: str = "",
) -> None:
    """Append a feedback entry to ``signals/feedback.jsonl`` (spec §9).

    Producers read this stream to tune their detection. The file is
    append-only JSONL; no compaction. Caller is responsible for
    ensuring the parent directory exists (the helper creates it).
    """
    path = feedback_log_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": _utc_now_iso(),
        "signal_id": signal_id,
        "signal_signature": signal_signature,
        "proposal_id": proposal_id,
        "verdict": verdict,
        "note": note,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
