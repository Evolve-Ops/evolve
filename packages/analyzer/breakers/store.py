"""breakers.store — the breaker state store.

Spec: docs/spec-circuit-breakers-2026-05-21.md §5.3

File layout under ``{shared_dir}/breakers/``::

    {shared_dir}/breakers/
    ├── <bot_id>/
    │   ├── cost.json        # L1 cost breaker for this bot
    │   └── full.json        # L2 full halt for this bot
    ├── pod/
    │   ├── cost.json        # pod-wide L1
    │   └── full.json        # pod-wide L2
    └── log/
        └── <YYYY-MM-DD>.jsonl   # append-only audit log

Semantics:
  - **File existence = trip is in effect.** No file → no trip.
  - The ``state`` field in the JSON is informational; existence is
    authoritative. Cleared trips delete the file.
  - The ``expires_at`` field is informational at read time. Enforcers
    (Phase 3 heal.py-as-reaper) clear expired files; until they do,
    an expired file still reports as a trip in raw reads. Use
    ``list_active()`` for "trips that are currently in-effect."
  - Atomic writes via tempfile + os.replace. Audit log appended via
    O_APPEND for single-write atomicity (small JSON records, well
    under PIPE_BUF).
  - Owned by the ``evolve`` user; ``{shared_dir}`` already has the
    right ACL. No sudo or /tmp staging needed.

The schema is intentionally trivial JSON — no pickle-derived shapes —
because every enforcer (heal.py, the TS plugin, future Python-side
readers running in slim subprocess contexts) will read the file
DIRECTLY rather than import this module. Adding fields is safe;
renaming or changing types is breaking.

The string ``"pod"`` is reserved as a pod-wide scope identifier. Bot
IDs equal to ``"pod"`` would collide; the validation in ``trip()``
rejects that.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso_micro as _now_iso


# Valid breaker types.
# - "cost"    — L1 cost trip; pauses heartbeat + background, user chat works
# - "cost_l2" — L2 cost trip; bootouts gateway, manual reset only
#               (Phase 6 of the 2026-06 cost-cap normalization; spec at
#               docs/spec-cost-caps-2026-06-05.md)
# - "full"    — pod-wide pause (operator-driven, not cost-driven)
BREAKER_TYPES: frozenset[str] = frozenset({"cost", "cost_l2", "full"})
BreakerType = Literal["cost", "cost_l2", "full"]

# Reserved scope for pod-wide trips.
POD_SCOPE: str = "pod"

# Reasonable subdir-name pattern for a bot_id — letters/numbers/underscore/hyphen.
# Keeps us from accidentally creating breakers/.. or breakers// paths.
_VALID_SCOPE_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


@dataclass(frozen=True)
class BreakerRecord:
    """One breaker's persisted state."""

    bot_id: str                          # bot_id or "pod"
    type: str                            # "cost" | "full"
    state: str                           # "tripped" (the only persisted state)
    tripped_at: str                      # ISO 8601
    expires_at: str | None               # ISO 8601 or None for indefinite
    initiated_by: str                    # "auto" | "admin:<id>" | "user:<id>" | "evo:<id>"
    reason: str
    motivating_signals: list[str] = field(default_factory=list)
    trip_id: str = ""                    # uuid4
    audit_summary: str | None = None     # populated async after trip
    audit_recommendation: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BreakerRecord":
        return cls(
            bot_id=data["bot_id"],
            type=data["type"],
            state=data.get("state", "tripped"),
            tripped_at=data["tripped_at"],
            expires_at=data.get("expires_at"),
            initiated_by=data.get("initiated_by", "unknown"),
            reason=data.get("reason", ""),
            motivating_signals=list(data.get("motivating_signals") or []),
            trip_id=data.get("trip_id", ""),
            audit_summary=data.get("audit_summary"),
            audit_recommendation=data.get("audit_recommendation"),
        )


# ── Path helpers ─────────────────────────────────────────────────────────────


def _validate_scope(scope: str) -> None:
    if not scope:
        raise ValueError("scope must be a non-empty string")
    if any(c not in _VALID_SCOPE_CHARS for c in scope):
        raise ValueError(
            f"scope {scope!r} contains invalid characters; "
            f"allowed: letters, digits, underscore, hyphen"
        )


def _validate_type(breaker_type: str) -> None:
    if breaker_type not in BREAKER_TYPES:
        raise ValueError(
            f"unknown breaker type {breaker_type!r}; "
            f"valid: {sorted(BREAKER_TYPES)}"
        )


def breakers_dir(shared_dir: Path) -> Path:
    """Return the breakers root, creating it (and log/) on demand."""
    p = Path(shared_dir) / "breakers"
    p.mkdir(parents=True, exist_ok=True)
    (p / "log").mkdir(parents=True, exist_ok=True)
    return p


def breaker_file_path(
    shared_dir: Path, scope: str, breaker_type: str,
) -> Path:
    """Return the path for a (scope, type) breaker file."""
    _validate_scope(scope)
    _validate_type(breaker_type)
    return breakers_dir(shared_dir) / scope / f"{breaker_type}.json"


def audit_log_path(shared_dir: Path, when: datetime | None = None) -> Path:
    """Return today's audit log file path."""
    when = when or datetime.now(timezone.utc)
    date_str = when.strftime("%Y-%m-%d")
    return breakers_dir(shared_dir) / "log" / f"{date_str}.jsonl"


# ── Atomic write helpers ─────────────────────────────────────────────────────


def _append_audit(
    shared_dir: Path,
    record: dict[str, Any],
    *,
    when: datetime | None = None,
) -> None:
    """Append one record to today's audit log.

    O_APPEND makes the seek-to-end + write atomic on POSIX for writes
    smaller than PIPE_BUF. Our records are small JSON objects so
    concurrent writers won't interleave in practice."""
    path = audit_log_path(shared_dir, when=when)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    raw = s
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Public API ───────────────────────────────────────────────────────────────


def is_expired(record: BreakerRecord, *, now: datetime | None = None) -> bool:
    """True if the record has an expires_at in the past.

    Indefinite trips (expires_at is None) never return True.
    """
    if record.expires_at is None:
        return False
    exp = _parse_iso(record.expires_at)
    if exp is None:
        # Malformed expiry — treat as not-expired (fail-open: don't
        # accidentally clear a trip on a parsing glitch).
        return False
    now = now or datetime.now(timezone.utc)
    return now >= exp


def read_trip(
    shared_dir: Path, scope: str, breaker_type: str,
) -> BreakerRecord | None:
    """Read one breaker's record, or None if no file exists.

    Raw read — does NOT filter on expiry. Callers that want
    "currently in-effect" semantics should use list_active() or
    check is_expired().

    Fail-open on corrupt files: return None as if no trip exists.
    Better to let a turn through than to lock a bot out on a
    truncated JSON file. Matches the failure-mode discipline of
    recovery.py and the existing heal.py reader.
    """
    path = breaker_file_path(shared_dir, scope, breaker_type)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return BreakerRecord.from_json(data)
    except (KeyError, TypeError):
        return None


def list_all(shared_dir: Path) -> list[BreakerRecord]:
    """Return every breaker record on disk (active and expired)."""
    root = breakers_dir(shared_dir)
    out: list[BreakerRecord] = []
    for scope_dir in sorted(root.iterdir()):
        if not scope_dir.is_dir() or scope_dir.name == "log":
            continue
        for breaker_type in sorted(BREAKER_TYPES):
            rec = read_trip(shared_dir, scope_dir.name, breaker_type)
            if rec is not None:
                out.append(rec)
    return out


def list_active(
    shared_dir: Path, *, now: datetime | None = None,
) -> list[BreakerRecord]:
    """Return only breakers whose expires_at hasn't passed (or is None)."""
    return [r for r in list_all(shared_dir) if not is_expired(r, now=now)]


def trip(
    *,
    shared_dir: Path,
    scope: str,
    breaker_type: str,
    duration: timedelta | None,
    initiated_by: str,
    reason: str,
    motivating_signals: Iterable[str] | None = None,
    now: datetime | None = None,
) -> BreakerRecord:
    """Trip a breaker.

    If the breaker is already tripped, this overwrites with a fresh
    trip (new trip_id, refreshed timestamps). The audit log records
    the overwrite. This matches recovery.pause_all's idempotency.

    ``duration=None`` means indefinite (never auto-expires).
    """
    _validate_scope(scope)
    _validate_type(breaker_type)

    now = now or datetime.now(timezone.utc)
    expires = (now + duration) if duration is not None else None

    previous = read_trip(shared_dir, scope, breaker_type)

    record = BreakerRecord(
        bot_id=scope,
        type=breaker_type,
        state="tripped",
        tripped_at=now.isoformat(),
        expires_at=expires.isoformat() if expires else None,
        initiated_by=initiated_by,
        reason=reason,
        motivating_signals=list(motivating_signals or []),
        trip_id=str(uuid.uuid4()),
    )

    path = breaker_file_path(shared_dir, scope, breaker_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, record.to_json(), sort_keys=True)

    _append_audit(shared_dir, {
        "timestamp": _now_iso(),
        "action": "trip" if previous is None else "retrip",
        "scope": scope,
        "type": breaker_type,
        "trip_id": record.trip_id,
        "previous_trip_id": previous.trip_id if previous else None,
        "initiated_by": initiated_by,
        "reason": reason,
        "duration_seconds": int(duration.total_seconds()) if duration else None,
        "expires_at": record.expires_at,
        "motivating_signals": record.motivating_signals,
    }, when=now)

    return record


def reset(
    *,
    shared_dir: Path,
    scope: str,
    breaker_type: str,
    initiated_by: str,
    reason: str = "",
    now: datetime | None = None,
) -> BreakerRecord | None:
    """Reset a breaker. Returns the prior record, or None if nothing was tripped.

    Idempotent: resetting an already-clear breaker is a no-op (no
    audit entry written). Logging "tried to reset a non-tripped
    breaker" is noise; the operator's intent was reached either way.
    """
    _validate_scope(scope)
    _validate_type(breaker_type)

    previous = read_trip(shared_dir, scope, breaker_type)
    if previous is None:
        return None

    path = breaker_file_path(shared_dir, scope, breaker_type)
    try:
        path.unlink()
    except FileNotFoundError:
        # Race: another writer cleared it between our read and delete.
        # Treat as success — the desired post-state holds.
        pass

    _append_audit(shared_dir, {
        "timestamp": _now_iso(),
        "action": "reset",
        "scope": scope,
        "type": breaker_type,
        "trip_id": previous.trip_id,
        "initiated_by": initiated_by,
        "reason": reason or "manual reset",
    }, when=now)

    return previous


def extend(
    *,
    shared_dir: Path,
    scope: str,
    breaker_type: str,
    additional: timedelta,
    initiated_by: str,
    now: datetime | None = None,
) -> BreakerRecord | None:
    """Extend a tripped breaker by ``additional``. Returns the new record.

    Returns None if the breaker isn't currently tripped (nothing to
    extend). Indefinite trips (expires_at is None) are left unchanged
    — there's nothing to extend, and silently turning indefinite into
    bounded would be a surprise.
    """
    _validate_scope(scope)
    _validate_type(breaker_type)

    current = read_trip(shared_dir, scope, breaker_type)
    if current is None:
        return None
    if current.expires_at is None:
        # Indefinite — don't touch.
        return current

    now = now or datetime.now(timezone.utc)
    current_expiry = _parse_iso(current.expires_at)
    if current_expiry is None:
        return current  # malformed; don't compound the problem
    # Extend from MAX(now, current_expiry) so extending an already-expired
    # trip pushes it from now forward, not from the past.
    base = max(now, current_expiry)
    new_expiry = base + additional

    new_record = BreakerRecord(
        bot_id=current.bot_id,
        type=current.type,
        state=current.state,
        tripped_at=current.tripped_at,
        expires_at=new_expiry.isoformat(),
        initiated_by=current.initiated_by,
        reason=current.reason,
        motivating_signals=current.motivating_signals,
        trip_id=current.trip_id,
        audit_summary=current.audit_summary,
        audit_recommendation=current.audit_recommendation,
    )

    path = breaker_file_path(shared_dir, scope, breaker_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, new_record.to_json(), sort_keys=True)

    _append_audit(shared_dir, {
        "timestamp": _now_iso(),
        "action": "extend",
        "scope": scope,
        "type": breaker_type,
        "trip_id": new_record.trip_id,
        "initiated_by": initiated_by,
        "additional_seconds": int(additional.total_seconds()),
        "previous_expires_at": current.expires_at,
        "new_expires_at": new_record.expires_at,
    }, when=now)

    return new_record


def update_audit_fields(
    *,
    shared_dir: Path,
    scope: str,
    breaker_type: str,
    audit_summary: str | None = None,
    audit_recommendation: str | None = None,
) -> BreakerRecord | None:
    """Populate the async audit-of-cause fields on a tripped breaker.

    Used by the Phase 5 audit generator after a trip lands. Returns
    None if the breaker isn't currently tripped.
    """
    _validate_scope(scope)
    _validate_type(breaker_type)

    current = read_trip(shared_dir, scope, breaker_type)
    if current is None:
        return None

    new_record = BreakerRecord(
        bot_id=current.bot_id,
        type=current.type,
        state=current.state,
        tripped_at=current.tripped_at,
        expires_at=current.expires_at,
        initiated_by=current.initiated_by,
        reason=current.reason,
        motivating_signals=current.motivating_signals,
        trip_id=current.trip_id,
        audit_summary=audit_summary if audit_summary is not None else current.audit_summary,
        audit_recommendation=(
            audit_recommendation if audit_recommendation is not None
            else current.audit_recommendation
        ),
    )

    path = breaker_file_path(shared_dir, scope, breaker_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, new_record.to_json(), sort_keys=True)
    return new_record


# ── Audit log reading (admin UI / status command) ────────────────────────────


def read_audit_log(
    shared_dir: Path, *, days: int = 7, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return audit-log records from the last ``days`` days, newest first."""
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for d_offset in range(days):
        day = now - timedelta(days=d_offset)
        path = audit_log_path(shared_dir, when=day)
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return out


# ── CLI-friendly duration parser ─────────────────────────────────────────────


def parse_duration(s: str) -> timedelta | None:
    """Parse a duration string into a timedelta, or None for "indefinite".

    Accepts: "1h", "4h", "24h", "7d", "indefinite", "indef", "none".
    Also accepts integer suffixes: "30m" → 30 minutes, "2h" → 2 hours,
    "3d" → 3 days. Raises ValueError on unparseable input.
    """
    s = s.strip().lower()
    if not s:
        raise ValueError("duration must be non-empty")
    if s in ("indefinite", "indef", "none"):
        return None
    if len(s) < 2:
        raise ValueError(f"unparseable duration {s!r}")
    unit = s[-1]
    try:
        n = int(s[:-1])
    except ValueError as e:
        raise ValueError(f"unparseable duration {s!r}") from e
    if n < 0:
        raise ValueError(f"duration must be non-negative: {s!r}")
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    raise ValueError(f"unknown duration unit {unit!r} in {s!r}; use m/h/d or 'indefinite'")
