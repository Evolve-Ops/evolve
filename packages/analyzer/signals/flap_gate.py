"""signals.flap_gate — self-healing flap hysteresis for transient-prone signals.

Spec: internal/spec-transient-signal-suppression-2026-06-23.md (the umbrella
contract; the bring-up half lives in :mod:`signals.settle_gate`).

The sibling of :mod:`signals.settle_gate`. Where the settle gate quiets the
*one-time* bring-up flurry, this gate quiets *recurring* flaps: a condition
that fires → clears → fires every cycle because something is re-clamping and
auto-restoring it. The canonical case is the ``evolve``-read ACL being
re-clamped hourly (an OpenClaw gateway re-harden) and auto-restored by the
deploy/watchdog seconds later — the SAME ``acl_drift`` / ``pod_perms_drift``
condition pages every hour, and the meta-alert ``signal_notifier is on
repeat`` fires because the operator is paged 5× in 24h for noise.

Hysteresis rule
===============

A transient-prone condition must be observed on **N≥2 consecutive monitor
cycles** before it is allowed to page. A condition that clears before the
dwell elapses is logged but never reaches the Signal store. Once a condition
*has* paged (its Signal is firing), re-observation pages immediately — a
genuinely persistent condition is never delayed beyond the first dwell, and
its Signal's existing dedup / reopen lifecycle is untouched.

Integration with the Signal store
==================================

This gate sits in FRONT of ``signals.store.observe`` and reuses the very
``signature`` the store dedups on as the dwell key. It NEVER hand-edits
Signal JSON. The dwell counter lives in a tiny per-signature ledger under
``{shared_dir}/signals/pending/<sig-hash>.json``; once a condition clears the
dwell we simply stop withholding and let the producer's normal
``store.observe(**spec)`` call run, so find-or-create / reopen / sweep_resolve
all behave exactly as before.

Producer contract (per monitor cycle, for a transient-prone signature)
======================================================================

  * condition ABSENT  → call :func:`note_cleared` (resets the dwell counter)
  * condition PRESENT → call :func:`note_observed`; emit the Signal only when
    it returns ``True`` (paged), withhold when ``False`` (still dwelling).
  * a paired Proposal (e.g. the ACL-restore fix) gates on
    :func:`signal_is_active` so it fires in lock-step with its Signal.

Fail-open by design (matching settle_gate): any ledger read/write error
PAGES rather than risk swallowing a persistent condition. Leaf-ish module —
stdlib + ``evolve_util`` at import time; ``signals.store`` is imported lazily
inside the two functions that need the firing-signal lookup, so producers
that only write the ledger never pull the store in.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from evolve_util import atomic_write_json

_log = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    """UTC ISO stamp (``...Z``) for an explicit datetime — mirrors
    ``evolve_util.now_iso`` but honors an injected clock so the dwell ledger
    is deterministic under test."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Consecutive cycles a transient-prone condition must persist before paging.
# N=2 ⇒ "seen on two back-to-back monitor ticks." A single-tick flap that
# clears before the next tick never reaches the Signal store.
DEFAULT_DWELL_CYCLES = 2

# Pending entries not re-observed within this window are reaped as stale (the
# condition cleared, but the producer never called note_cleared — e.g. it
# crashed, or stopped running that detector). Sized to span a couple of the
# slowest (hourly) monitor cadences so a legitimate 2-cycle dwell is never
# reset out from under a producer.
PENDING_STALE_SECONDS = 3 * 3600


# Signal ``type`` fragments that mark a condition as transient-prone
# (flap-eligible). Matched case-insensitively as substrings. Producers can
# always force the decision via the ``transient`` override on the public
# helpers; this registry is the default for callers that only pass a type.
_TRANSIENT_TYPE_FRAGMENTS: tuple[str, ...] = (
    "acl",                  # acl_drift, evolve_read_acl, …
    "perm",                 # perms_drift, pod_perms_drift, perm_*
    "workspace_unreadable",
    "unreadable",
    "file_mode",
    "filemode",
    "mode_drift",
    "secret_mode",
)


@dataclass
class DwellVerdict:
    page: bool          # True ⇒ emit the Signal now
    count: int          # consecutive-observation count incl. this one
    dwell_cycles: int
    reason: str         # short, for logging/tests


# ─────────────────────────────────────────────────────────────────────────────
# Transient-prone registry
# ─────────────────────────────────────────────────────────────────────────────


def is_transient_prone(type_: str | None, *, override: bool | None = None) -> bool:
    """Whether a Signal ``type`` is flap-eligible (dwell hysteresis applies).

    ``override`` short-circuits the registry — pass ``True``/``False`` when the
    producer knows the answer for this specific condition (pod_perms_drift and
    acl_drift both pass ``transient=True`` explicitly)."""
    if override is not None:
        return override
    if not type_:
        return False
    t = type_.lower()
    return any(frag in t for frag in _TRANSIENT_TYPE_FRAGMENTS)


# ─────────────────────────────────────────────────────────────────────────────
# Pending-ledger paths
# ─────────────────────────────────────────────────────────────────────────────
#
# One small JSON file per dwelling signature under
# ``{shared_dir}/signals/pending/<sig-hash>.json``. Per-signature files keep
# the read-modify-write lock-free: a signature uniquely identifies one
# producer's one condition, and the signal-emitting monitors are
# single-instance, so only one process writes a given file at a time. (The
# Signal store's own observe() still takes the store lock once a condition
# promotes — that is where cross-process find-or-create races actually live.)


def _pending_dir(shared_dir: Path | str) -> Path:
    return Path(shared_dir) / "signals" / "pending"


def _pending_path(shared_dir: Path | str, signature: str) -> Path:
    digest = hashlib.sha256(signature.encode()).hexdigest()[:24]
    return _pending_dir(shared_dir) / f"{digest}.json"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def note_cleared(shared_dir: Path | str, signature: str) -> None:
    """Condition absent this cycle → drop any pending dwell entry.

    Idempotent and best-effort: a no-op if nothing is pending. Calling this on
    the absent branch is what makes a single-cycle flap reset to zero instead
    of accumulating toward the dwell threshold across unrelated cycles."""
    path = _pending_path(shared_dir, signature)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _log.debug("flap_gate.note_cleared: unlink failed for %s: %s", path, exc)


def note_cleared_absent(
    shared_dir: Path | str,
    present_signatures: Iterable[str],
    *,
    type_filter: str | None = None,
    signature_filter: Callable[[str], bool] | None = None,
) -> list[str]:
    """Reset the dwell for every TRACKED signature NOT in ``present_signatures``.

    The dynamic-signature-set analogue of :func:`note_cleared`. A sweep-style
    producer like the audit signal-mirror does not have one fixed signature to
    clear on the absent branch — it enumerates the conditions PRESENT this run
    and must reset the dwell counter for any previously-dwelling condition that
    has since cleared, so a single-cycle flap never carries a count across a gap
    toward the threshold (the "N≥2 *consecutive* cycles" contract).

    ``type_filter`` restricts the sweep to pending entries whose stored ``type``
    equals it. The pending dir is shared across producers, so a producer must
    only clear its OWN dwell entries — audit passes ``"audit_perm_flap"`` so it
    never resets ``pod_perms_drift`` / ``acl_drift`` counters living in the same
    dir. When ``None`` every absent entry is eligible (single-producer dirs /
    tests).

    ``signature_filter`` further restricts the sweep to entries whose signature
    the predicate accepts. ``type_filter`` answers "is this entry MINE?"; this
    answers "did the run that is sweeping actually LOOK for it?" — a producer
    that ran only a subset of its checks (audit's ``--bot`` / ``--category``)
    must not read the absence of a condition it never examined as evidence that
    it cleared. The predicate is opaque on purpose: callers own their signature
    key shape, and this module never learns it.

    Best-effort; returns the cleared signatures. A read/unlink error on one
    entry skips it rather than aborting the sweep.
    """
    present = set(present_signatures)
    pdir = _pending_dir(shared_dir)
    if not pdir.exists():
        return []
    cleared: list[str] = []
    for path in pdir.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if type_filter is not None and entry.get("type") != type_filter:
            continue
        sig = entry.get("signature")
        if not isinstance(sig, str) or sig in present:
            continue
        if signature_filter is not None and not signature_filter(sig):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.debug(
                "flap_gate.note_cleared_absent: unlink failed for %s: %s", path, exc
            )
            continue
        cleared.append(sig)
    return cleared


def signal_is_active(shared_dir: Path | str, signature: str) -> bool:
    """True if a firing/snoozed Signal already exists for ``signature``.

    Used by paired-Proposal detectors to fire in lock-step with their Signal:
    while the Signal is still dwelling there is no active Signal, so the
    Proposal withholds too. Fails OPEN (returns True) on any store error so a
    lookup failure never silently swallows the proposal — for the proposal
    gate, withholding the autonomous fix is the worse error."""
    active = _active_signal_exists(shared_dir, signature)
    return True if active is None else active


def _active_signal_exists(shared_dir: Path | str, signature: str) -> bool | None:
    """Guarded firing/snoozed lookup. Returns True/False, or None on a store
    error so each caller can pick its own fail direction."""
    try:
        from signals.store import find_active_by_signature

        return find_active_by_signature(Path(shared_dir), signature) is not None
    except Exception as exc:  # noqa: BLE001
        _log.warning("flap_gate: active-signal lookup failed for %s: %s", signature, exc)
        return None


def _read_pending(path: Path, *, now: datetime) -> dict[str, Any] | None:
    """Read a pending ledger entry. None if absent, malformed, or STALE.

    A stale entry (last observed > ``PENDING_STALE_SECONDS`` ago) is a residue
    from a producer that stopped observing without calling :func:`note_cleared`
    (crash / disabled detector). Treating it as absent means a fresh incident
    re-dwells from zero instead of instant-promoting off a months-old count."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("last_observed_at")
    try:
        last_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return data  # missing/bad timestamp → keep the count rather than lose it
    if (now.timestamp() - last_dt.timestamp()) > PENDING_STALE_SECONDS:
        return None
    return data


def note_observed(
    shared_dir: Path | str,
    *,
    signature: str,
    dwell_cycles: int = DEFAULT_DWELL_CYCLES,
    transient: bool | None = None,
    type: str | None = None,
    now: datetime | None = None,
) -> DwellVerdict:
    """Record one observation of a present condition; decide whether to page.

    Returns a :class:`DwellVerdict`. ``page=True`` means the caller should emit
    the Signal now; ``page=False`` means it is still dwelling and the Signal
    must be withheld this cycle.

    Pages immediately (no dwell) when the condition is NOT transient-prone, or
    when a Signal for this signature is already firing (re-observation of an
    active incident is continuation, not a flap). Otherwise the
    consecutive-observation count is bumped and the verdict pages once it
    reaches ``dwell_cycles``.

    Fail-open: any ledger write error pages rather than risk swallowing a
    persistent condition."""
    now = now or datetime.now(timezone.utc)

    if not is_transient_prone(type, override=transient):
        return DwellVerdict(True, 1, dwell_cycles, "not-transient")

    if dwell_cycles <= 1:
        return DwellVerdict(True, 1, dwell_cycles, "no-dwell-configured")

    # Already-firing: a Signal for this signature is live, so re-observing is
    # continuation of a paged incident — page immediately and let the store's
    # observation_count bump / dedup run. Fail CLOSED here (a store-lookup error
    # → keep dwelling, not page) so a transient read blip can't turn a flap into
    # an immediate page; that is the whole job of this gate. (signal_is_active,
    # used by the proposal gate, fails OPEN for the opposite reason.)
    if _active_signal_exists(shared_dir, signature) is True:
        return DwellVerdict(True, dwell_cycles, dwell_cycles, "already-firing")

    path = _pending_path(shared_dir, signature)
    prior = _read_pending(path, now=now)  # None if absent/malformed/stale

    count = int(prior.get("count", 0)) + 1 if prior else 1
    first_seen = (prior or {}).get("first_observed_at") or _iso(now)
    record = {
        "signature": signature,
        "type": type,
        "count": count,
        "dwell_cycles": dwell_cycles,
        "first_observed_at": first_seen,
        "last_observed_at": _iso(now),
    }
    try:
        _pending_dir(shared_dir).mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, record, mode=0o644)
    except Exception as exc:  # noqa: BLE001 — fail loud, don't swallow the signal
        # Fail open (matching settle_gate): if we can't track the dwell, page
        # rather than swallow a possibly-persistent condition.
        _log.warning(
            "flap_gate.note_observed: ledger write failed for %s (%s); paging",
            signature,
            exc,
        )
        return DwellVerdict(True, count, dwell_cycles, "ledger-write-failed")

    if count >= dwell_cycles:
        # Promoted. Deliberately LEAVE the ledger in place: the already-firing
        # short-circuit handles later cycles, and if the producer's
        # store.observe() fails THIS cycle, next cycle re-promotes immediately
        # (the count keeps climbing) instead of restarting the dwell. The entry
        # is reaped by note_cleared (absent branch) or sweep_stale.
        return DwellVerdict(True, count, dwell_cycles, f"dwell-met({count}/{dwell_cycles})")
    return DwellVerdict(False, count, dwell_cycles, f"dwelling({count}/{dwell_cycles})")


def sweep_stale(
    shared_dir: Path | str,
    *,
    max_age_seconds: int = PENDING_STALE_SECONDS,
    now: datetime | None = None,
) -> list[str]:
    """Reap pending entries last observed more than ``max_age_seconds`` ago.

    Backstop for producers that stop observing a condition without calling
    :func:`note_cleared` (crash, detector disabled). Returns dropped
    signatures. Cheap to call at the end of any monitor run."""
    now = now or datetime.now(timezone.utc)
    pdir = _pending_dir(shared_dir)
    if not pdir.exists():
        return []
    cutoff = now.timestamp() - max_age_seconds
    dropped: list[str] = []
    for path in pdir.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        last = entry.get("last_observed_at")
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if last_dt.timestamp() < cutoff:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _log.debug("flap_gate.sweep_stale: unlink failed for %s: %s", path, exc)
                continue
            sig = entry.get("signature")
            if isinstance(sig, str):
                dropped.append(sig)
    return dropped
