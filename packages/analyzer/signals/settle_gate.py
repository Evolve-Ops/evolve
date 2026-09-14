"""signals.settle_gate — fresh-pod bring-up "settle window" for monitors.

Spec: internal/spec-pod-bringup-settle-2026-06-23.md

A freshly deployed pod cycles through ``harden → ACL-reassert`` states during
bring-up. Steady-state monitors (security audit, sysadmin watchdog, infra
audit) fire immediately on the new pod and page the operator with *transient*
setup-state findings — most prominently "evolve cannot read .openclaw" — that
the deploy self-heals seconds later.

This module is the single seam those producers consult **at their emission
point** to withhold non-critical, transient bring-up findings until the pod has
settled. Genuinely critical findings (severity ``alert``) are never withheld.

Two markers under ``{shared_dir}`` define the window:

  * ``pod-bringup.json``  — written at the START of a fresh (non-repair) wizard
    run, before daemons are installed. Presence + recency ⇒ "bring-up in
    progress".
  * ``pod-settled.json``  — written as the genuinely-LAST fresh-wizard step
    (after the final evolve-access re-assert), and idempotently by
    ``ensure_pod_perms``. Presence ⇒ "first full deploy + access-verify done".

``is_pod_settled`` resolves to True when the settled marker exists, OR the
bring-up marker is absent (backward-compat / non-fresh paths fail OPEN to
firing), OR the bring-up marker is older than ``SETTLE_WINDOW_SECONDS`` (a cap
so a wizard that dies before writing the settled marker can never suppress
findings forever).

Leaf module by design: imports only stdlib + ``evolve_util`` so every producer
(analyzer-side and admin-side) can import it with no circular-import risk.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from evolve_util import atomic_write_json, now_iso

_log = logging.getLogger(__name__)

# Cap on how long the unsettled window can last when keyed off the bring-up
# start marker alone (i.e. the settled marker never got written because the
# wizard died mid-run). 30 minutes comfortably covers a full fresh deploy +
# the harden/ACL-reassert cycle while bounding worst-case suppression.
SETTLE_WINDOW_SECONDS = 1800

_BRINGUP_MARKER = "pod-bringup.json"
_SETTLED_MARKER = "pod-settled.json"

Severity = Literal["info", "warn", "alert"]


# ─────────────────────────────────────────────────────────────────────────────
# Writers (called by the deploy/wizard path; runs as root or evolve)
# ─────────────────────────────────────────────────────────────────────────────


def mark_bringup_started(shared_dir: Path | str, *, trigger: str = "fresh-wizard") -> None:
    """Stamp the start of a fresh bring-up. Write-once: a no-op if the marker
    already exists (so a re-run of a fresh wizard doesn't reset the clock).

    Best-effort: any write error is swallowed — the gate fails OPEN (treats the
    pod as settled) when markers can't be read, so a failed write never causes
    findings to be suppressed."""
    root = Path(shared_dir)
    path = root / _BRINGUP_MARKER
    if _exists_safe(path):
        return
    try:
        atomic_write_json(
            path,
            {"started_at": now_iso(), "trigger": trigger},
            mode=0o644,
        )
    except OSError as e:
        # Best-effort: a failed write means no settle window (gate fails OPEN
        # to firing), which is the safe direction — log, don't crash bring-up.
        _log.warning("settle_gate: could not write bring-up marker %s: %s", path, e)


def mark_settled(shared_dir: Path | str, *, trigger: str = "fresh-wizard") -> None:
    """Stamp the pod as settled — first full deploy + access-verify succeeded.

    Idempotent and write-once: a no-op if the settled marker already exists.
    Best-effort (see :func:`mark_bringup_started`)."""
    root = Path(shared_dir)
    path = root / _SETTLED_MARKER
    if _exists_safe(path):
        return
    try:
        atomic_write_json(
            path,
            {"settled_at": now_iso(), "trigger": trigger},
            mode=0o644,
        )
    except OSError as e:
        # Best-effort (see mark_bringup_started). A missing settled marker just
        # means the 30-min cap governs the window end instead.
        _log.warning("settle_gate: could not write pod-settled marker %s: %s", path, e)


# ─────────────────────────────────────────────────────────────────────────────
# Predicate + gate (consulted by producers at emission)
# ─────────────────────────────────────────────────────────────────────────────


def is_pod_settled(shared_dir: Path | str, *, now: datetime | None = None) -> bool:
    """Return True when the pod is past its bring-up settle window.

    See module docstring for the three-rule resolution. Fails OPEN (returns
    True) on any read error or unparseable marker — an unreadable settle state
    must never silently suppress monitoring."""
    root = Path(shared_dir)

    if _exists_safe(root / _SETTLED_MARKER):
        return True

    started_at = _read_iso_ts(root / _BRINGUP_MARKER, "started_at")
    if started_at is None:
        # No bring-up marker (existing pod, upgrade/repair path, or unreadable)
        # → not a fresh bring-up → settled.
        return True

    now = now or datetime.now(timezone.utc)
    return (now - started_at) >= timedelta(seconds=SETTLE_WINDOW_SECONDS)


def pod_first_seen(shared_dir: Path | str) -> datetime | None:
    """Best estimate of when this pod was first brought up.

    The bring-up marker (``pod-bringup.json::started_at``) is write-once — it
    is stamped at the start of the *first* fresh wizard and never reset — so it
    is the pod's durable birth timestamp. Falls back to the settled marker
    (``pod-settled.json::settled_at``) when the bring-up marker is absent (it is
    written seconds-to-minutes later, so it slightly post-dates birth but is a
    fine lower bound).

    Returns ``None`` when neither marker exists — an established pod that
    upgraded in from before the settle markers shipped, whose age is
    undeterminable. Callers (e.g. :mod:`signals.maturity_gate`) must treat
    unknown age as "old enough" (fail OPEN to mature) rather than withholding
    content.

    This is the single "pod age" anchor shared across the fresh-pod alert
    protection mechanisms (settle window + maturity gate)."""
    root = Path(shared_dir)
    started = _read_iso_ts(root / _BRINGUP_MARKER, "started_at")
    if started is None:
        started = _read_iso_ts(root / _SETTLED_MARKER, "settled_at")
    return started


def pod_age(shared_dir: Path | str, *, now: datetime | None = None) -> timedelta | None:
    """Age of the pod since :func:`pod_first_seen`. ``None`` when undeterminable
    (no markers). See :func:`pod_first_seen` for the fail-open contract."""
    first = pod_first_seen(shared_dir)
    if first is None:
        return None
    now = now or datetime.now(timezone.utc)
    return now - first


def should_withhold(
    shared_dir: Path | str,
    *,
    severity: Severity,
    transient: bool,
    now: datetime | None = None,
) -> bool:
    """Return True iff this finding should be WITHHELD during bring-up.

    A finding is withheld iff it is producer-marked ``transient`` AND its
    severity is not ``alert`` AND the pod is not yet settled. ``alert``-severity
    (genuinely critical) findings always fire."""
    if not transient:
        return False
    if severity == "alert":
        return False
    return not is_pod_settled(shared_dir, now=now)


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


def _exists_safe(path: Path) -> bool:
    """``Path.exists()`` that returns False (not raises) on EACCES.

    Python 3.12's ``Path.exists()`` raises on a permission error mid-path
    instead of returning False (see the EACCES gotchas documented for the
    deploy checkout). Probe via stat and treat any OSError as absent."""
    try:
        path.stat()
        return True
    except OSError:
        return False


def _read_iso_ts(path: Path, key: str) -> datetime | None:
    """Parse an ISO-8601 timestamp under ``key`` from a marker file. None if
    the file is absent/unreadable/malformed or the key is missing/non-string."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get(key)
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_started_at(path: Path) -> datetime | None:
    """Back-compat alias for the bring-up marker's ``started_at`` parse."""
    return _read_iso_ts(path, "started_at")
