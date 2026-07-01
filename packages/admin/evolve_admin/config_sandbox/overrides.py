"""overrides — per-bot Policy override storage.

Phase 2 of ``docs/spec-openclaw-json-derived-artifact-2026-05-24.md``.

This module is the durable layer for per-bot deviations from schema /
network defaults. It's the back half of the config-sandbox: the schema
(``schema.py``) declares what's tunable; the read API (``api.py``)
projects current values; this module persists the operator's explicit
choices.

After Phase 3 lands, the materializer rebuilds each bot's
``openclaw.json`` from (schema defaults + network defaults + overrides
read here). For now (Phase 2) the file is *written* but no consumer
reads it — deploy still uses the gap-fill path. The file is dormant
until Phase 3 flips the cutover.

File layout
-----------

One file per bot, atomic temp+rename writes:

    {shared_dir}/sandbox/overrides/<bot_id>.json

    {
      "schema_version": 1,
      "bot_id": "security_bot",
      "overrides": {
        "<schema_path>": {
          "value": <serialised value>,
          "set_by": "operator|rsi:<id>|auto:drift_<ts>|migration:<id>|wizard",
          "set_at": "2026-05-24T18:00:00Z",
          "note": "optional operator context",
          "expires_at": "2026-08-01" | null,
          "needs_review": true | false
        }
      }
    }

The key is the sandbox-schema logical path (``TunableKey.path``), not the
on-disk openclaw.json dotted key. The schema entry's ``target_path``
carries the openclaw.json address that the Phase 3 materializer will
write to. Using the logical path here keeps the file stable across
openclaw.json reorganization (e.g. an upstream OC rename of
``plugins.evolve.tier`` → ``plugins.entries.evolve.config.tier``
doesn't invalidate stored overrides).

Validation
----------

Every write is gated by:

1. The key must exist in SCHEMA (``schema.by_path``). Unknown paths are
   rejected at write time — otherwise the overrides file could carry
   the same bug class as PR #1525 (schema-removed key persisting).
2. The schema entry must be ``per_bot=True``. Pod-wide overrides are
   not in scope for Phase 2; they'd live in a separate layer (or
   network.json).
3. The value's Python type must match the entry's ``type_hint``.

These constraints mean the overrides file can never be the source of a
schema-validity bug at materialization time — every value in it has
already been validated against the schema we ship.

Provenance
----------

Each write also records a parallel entry in ``provenance.json`` (the
cross-cutting audit log shared with all sandbox-driven changes; see
``provenance.py``). The overrides file is authoritative for the value;
provenance is the searchable history for the customizations UI.
``delete_override`` also calls ``provenance.forget``.

Owned by the ``evolve`` user — ``{shared_dir}/sandbox/`` already has
the right ACL. No /tmp staging + sudo needed.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from evolve_util import atomic_write_json

from . import provenance as _prov
from .schema import SCHEMA, TunableKey, by_path


_log = logging.getLogger(__name__)


OVERRIDES_SUBDIR = "sandbox/overrides"
SCHEMA_VERSION = 1


class OverrideValidationError(ValueError):
    """Raised when a write_override call fails validation.

    Carries an attribute ``reason`` (string) so callers can surface a
    consistent operator-facing message without parsing the exception
    text. Subclasses ValueError so callers that catch ValueError keep
    working.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OverrideEntry:
    value: Any
    set_by: str
    set_at: str
    note: str | None = None
    expires_at: str | None = None
    needs_review: bool = False


@dataclass
class BotOverrides:
    """Container for one bot's overrides file."""

    schema_version: int = SCHEMA_VERSION
    bot_id: str = ""
    overrides: dict[str, OverrideEntry] = field(default_factory=dict)

    def get(self, key: str) -> OverrideEntry | None:
        return self.overrides.get(key)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "bot_id": self.bot_id,
            "overrides": {k: asdict(v) for k, v in self.overrides.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BotOverrides":
        raw = data.get("overrides") or {}
        overrides: dict[str, OverrideEntry] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            try:
                overrides[k] = OverrideEntry(
                    value=v.get("value"),
                    set_by=str(v["set_by"]),
                    set_at=str(v["set_at"]),
                    note=v.get("note"),
                    expires_at=v.get("expires_at"),
                    needs_review=bool(v.get("needs_review", False)),
                )
            except (KeyError, TypeError):
                # Skip malformed entries rather than fail the whole load —
                # matches provenance.py's defensive pattern.
                continue
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            bot_id=str(data.get("bot_id", "")),
            overrides=overrides,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────


def overrides_dir(shared_dir: Path) -> Path:
    return shared_dir / OVERRIDES_SUBDIR


# Stricter than a blocklist: positive regex matching the bot-id shape we
# actually use elsewhere (lowercase letters, digits, _, -). Leading dot
# is excluded so we can't write a hidden file that collides with the temp
# prefix `.<bot>.json.<rand>` and breaks `_write_overrides`.
_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def path_for_bot(shared_dir: Path, bot_id: str) -> Path:
    if not bot_id or not _BOT_ID_RE.match(bot_id):
        # bot_id is interpolated into a path. We never trust caller input
        # here; reject anything that doesn't match the canonical shape.
        raise OverrideValidationError(f"invalid bot_id for overrides path: {bot_id!r}")
    return overrides_dir(shared_dir) / f"{bot_id}.json"


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────


class OverrideStateError(RuntimeError):
    """Raised when the on-disk state isn't safe to mutate.

    Distinct from OverrideValidationError (caller's bad input). This one
    means "the file exists but I can't safely read-modify-write it" —
    most often a malformed-but-non-empty file or a future schema_version.
    Refusing to write rather than silently obliterating is the conservative
    choice: a future-version file means a newer admin wrote it (rollback
    in progress?) and we shouldn't downgrade.
    """


def _read_raw(path: Path) -> tuple[BotOverrides | None, str]:
    """Inner read used by both ``read_bot_overrides`` (defensive) and the
    write path (strict).

    Returns ``(BotOverrides | None, reason)``. ``None`` means the file is
    present but couldn't be safely parsed; ``reason`` describes why.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"read failed: {e}"
    if not text.strip():
        return BotOverrides(), ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"json decode failed: {e}"
    if not isinstance(data, dict):
        return None, "top-level is not an object"
    sv = data.get("schema_version", SCHEMA_VERSION)
    try:
        sv_int = int(sv)
    except (TypeError, ValueError):
        return None, f"unparseable schema_version: {sv!r}"
    if sv_int > SCHEMA_VERSION:
        return None, (
            f"file's schema_version={sv_int} is newer than ours "
            f"({SCHEMA_VERSION}); refusing to downgrade"
        )
    return BotOverrides.from_dict(data), ""


def read_bot_overrides(shared_dir: Path, bot_id: str) -> BotOverrides:
    """Load one bot's overrides file. Returns an empty BotOverrides if absent.

    Defensive: a malformed file becomes empty, so a downstream caller
    that just wants to *read* doesn't crash. The write path takes the
    strict route via ``_read_raw`` and refuses to clobber unparseable
    content.
    """
    path = path_for_bot(shared_dir, bot_id)
    if not path.exists():
        return BotOverrides(bot_id=bot_id)
    bo, reason = _read_raw(path)
    if bo is None:
        _log.warning(
            "overrides: %s exists but unreadable (%s); returning empty",
            path, reason,
        )
        return BotOverrides(bot_id=bot_id)
    bo.bot_id = bot_id
    return bo


def iter_all_overrides(
    shared_dir: Path,
) -> Iterator[tuple[str, str, OverrideEntry]]:
    """Yield ``(bot_id, key, entry)`` for every override in the pod.

    Useful for cross-bot sweeps — e.g. the expires_at enforcer in Phase 5,
    or a "show me everything that needs review" UI query.

    Corrupted files are logged (via ``logging.warning``) but skipped — a
    failing sweep is worse than missing some entries, and the noisy log
    makes the corruption visible. Files whose names don't match the
    bot_id regex (e.g. accidentally-renamed backup, stray test file)
    are skipped silently.
    """
    d = overrides_dir(shared_dir)
    if not d.exists():
        return
    for child in sorted(d.iterdir()):
        if not child.is_file() or child.suffix != ".json":
            continue
        bot_id = child.stem
        if not _BOT_ID_RE.match(bot_id):
            continue
        bo, reason = _read_raw(child)
        if bo is None:
            _log.warning(
                "iter_all_overrides: skipping corrupted file %s: %s",
                child, reason,
            )
            continue
        for key, entry in bo.overrides.items():
            yield bot_id, key, entry


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


_TYPE_CHECKERS: dict[str, callable] = {
    # bool first — bool is also int in Python, so naive isinstance(value, int)
    # would silently accept True for int-typed keys. Each checker is strict.
    "bool": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "enum": lambda v: isinstance(v, str),       # plugin schema enum-value
                                                # checking is done at OC
                                                # validate time in Phase 3
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),      # JSON object (e.g. the
                                                # per-severity grace map)
    "doc": lambda v: isinstance(v, str),        # whole-document
}


def _validate_value_type(entry: TunableKey, value: Any) -> None:
    check = _TYPE_CHECKERS.get(entry.type_hint)
    if check is None:
        raise OverrideValidationError(
            f"schema entry {entry.path} has unknown type_hint {entry.type_hint!r}"
        )
    if not check(value):
        raise OverrideValidationError(
            f"value for {entry.path} has wrong type: expected {entry.type_hint}, "
            f"got {type(value).__name__}"
        )


def _validate_write(entry: TunableKey | None, key: str, value: Any) -> TunableKey:
    """Run the per-write checks. Returns the resolved TunableKey on success."""
    if entry is None:
        raise OverrideValidationError(
            f"unknown schema path: {key!r}. Add it to config_sandbox.schema "
            f"before overriding."
        )
    if not entry.per_bot:
        raise OverrideValidationError(
            f"{key} is a pod-wide key (per_bot=False); per-bot overrides "
            f"are out of scope. Set it in network.json instead."
        )
    _validate_value_type(entry, value)
    return entry


def _validate_expires_at(expires_at: str | None) -> None:
    """Reject malformed ``expires_at`` at write time.

    Phase 5 will enforce expiry by parsing this field; if it's malformed,
    the enforcer would either crash or silently skip the override and it
    would never expire. Catching the bug at write time means the operator
    sees the error immediately rather than discovering it months later.

    Accepts:
      - ISO 8601 dates: ``2026-08-01``
      - ISO 8601 datetimes: ``2026-08-01T14:00:00Z`` (with or without Z;
        Python 3.11+'s ``fromisoformat`` accepts the Z; on 3.10 we strip).
    """
    if expires_at is None:
        return
    if not isinstance(expires_at, str):
        raise OverrideValidationError(
            f"expires_at must be a string or None, got {type(expires_at).__name__}"
        )
    s = expires_at.rstrip("Z")
    try:
        # date-only ("YYYY-MM-DD") fits the date.fromisoformat / datetime
        # form. datetime.fromisoformat accepts both, so a single call works.
        datetime.fromisoformat(s)
    except ValueError as e:
        raise OverrideValidationError(
            f"expires_at={expires_at!r} is not a valid ISO date/datetime: {e}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Atomic write
# ─────────────────────────────────────────────────────────────────────────────


def _utc_iso(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_overrides(bo: BotOverrides, shared_dir: Path) -> Path:
    """Write a BotOverrides to disk via temp+rename, mode 0644.

    The write itself is ``evolve_util.atomic_write_json``; this wrapper
    keeps the overrides-specific extras (path resolution + parent mkdir
    + returning the path).

    0644 (vs the 0600 ``tempfile.mkstemp`` gives by default) is the same
    rationale documented in ``signals.store._atomic_write_json``: the
    file is read by multiple user contexts (evolve service, admin UI,
    eventually the deploy materializer) and 0600 silently breaks those
    consumers.

    Callers should hold the lock from ``_locked_modify`` before invoking
    this — temp+rename is torn-write-free, but the *read-modify-write*
    around it is not without external locking.
    """
    path = path_for_bot(shared_dir, bo.bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, bo.to_dict(), sort_keys=True, mode=0o644)
    return path


@contextmanager
def _locked_modify(shared_dir: Path, bot_id: str):
    """Acquire an exclusive flock on the bot's overrides file for the
    duration of a read-modify-write.

    Without this, two concurrent writers (operator clicking Accept while
    the auto-promote daemon writes a drift entry on the same bot) can
    each load the same content, mutate, write back — last writer wins,
    the other's change is lost. This is the classic lost-update problem;
    fcntl.flock(LOCK_EX) on a sidecar lock file serializes the read
    through the rename.

    Lock file is a sibling of the main file with a ``.lock`` suffix; it
    persists (we don't bother unlinking — that creates its own race) and
    is empty, so it's cheap.
    """
    path = path_for_bot(shared_dir, bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    # Open O_CREAT|O_RDWR so we can flock it even if it didn't exist.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_for_write(shared_dir: Path, bot_id: str) -> BotOverrides:
    """Strict read used by the write path: refuses to clobber a malformed
    or future-versioned file.

    A malformed file likely means manual editing went wrong or a write was
    half-completed during a crash. Returning empty (as the defensive read
    does) would silently destroy every operator override on the next
    write. Better to surface the corruption.
    """
    path = path_for_bot(shared_dir, bot_id)
    if not path.exists():
        return BotOverrides(bot_id=bot_id)
    bo, reason = _read_raw(path)
    if bo is None:
        raise OverrideStateError(
            f"refusing to overwrite {path}: {reason}. "
            f"Inspect the file manually; once it's parseable or empty, "
            f"the write will succeed."
        )
    bo.bot_id = bot_id
    return bo


# ─────────────────────────────────────────────────────────────────────────────
# Public write API
# ─────────────────────────────────────────────────────────────────────────────


def write_override(
    shared_dir: Path,
    bot_id: str,
    key: str,
    value: Any,
    *,
    set_by: str,
    note: str | None = None,
    expires_at: str | None = None,
    needs_review: bool = False,
    now: datetime | None = None,
) -> OverrideEntry:
    """Set an override for ``key`` on ``bot_id``. Validates + persists.

    Raises ``OverrideValidationError`` if:
      - ``key`` is not in the schema (call ``schema.by_path`` to check).
      - The schema entry is not ``per_bot=True``.
      - ``value``'s Python type doesn't match ``entry.type_hint``.
      - ``bot_id`` looks like a path-traversal attempt.
      - ``expires_at`` is non-None and not a parseable ISO date/datetime.

    Raises ``OverrideStateError`` if the existing on-disk file is
    malformed or carries a newer schema_version than this code knows.
    Refusing to overwrite is safer than silently destroying every
    operator override on the bot.

    Idempotent: re-writing the same key overwrites the previous entry.
    Also records a parallel entry in ``provenance.json`` so the
    customizations UI's set_by/set_at queries keep working unchanged.

    Concurrent writers (operator + auto-promote racing on the same bot)
    are serialized by an exclusive flock on a sidecar ``.lock`` file —
    the classic read-modify-write lost-update problem on JSON files.
    """
    entry = _validate_write(by_path(key), key, value)
    _validate_expires_at(expires_at)

    oe = OverrideEntry(
        value=value,
        set_by=set_by,
        set_at=_utc_iso(now),
        note=note,
        expires_at=expires_at,
        needs_review=needs_review,
    )

    with _locked_modify(shared_dir, bot_id):
        bo = _read_for_write(shared_dir, bot_id)
        bo.bot_id = bot_id  # in case the file was missing or had a stale id
        bo.overrides[key] = oe
        _write_overrides(bo, shared_dir)

    # Mirror into provenance.json — cross-cutting audit log. Logged on
    # failure so a systematic provenance-write break (permissions, disk)
    # is visible to operators rather than silently desynchronizing the
    # customizations UI from the overrides file.
    try:
        _prov.record(
            entry,
            value,
            set_by=set_by,
            bot_id=bot_id,
            reason=note,
            shared_dir=shared_dir,
            now=now,
        )
    except Exception as e:
        _log.warning(
            "overrides: provenance.record failed for %s/%s (%s: %s); "
            "overrides file is authoritative, customizations UI may "
            "show 'source unknown' until next write",
            bot_id, key, type(e).__name__, e,
        )

    return oe


def delete_override(
    shared_dir: Path,
    bot_id: str,
    key: str,
) -> bool:
    """Remove an override. Returns True if one was present.

    Use for the operator's "revert" action: the override is removed, the
    next deploy materializes the schema default, and provenance.forget
    keeps the audit log in sync.

    Raises ``OverrideStateError`` if the existing on-disk file is malformed
    (same safety rationale as ``write_override``).
    """
    with _locked_modify(shared_dir, bot_id):
        bo = _read_for_write(shared_dir, bot_id)
        if key not in bo.overrides:
            return False
        del bo.overrides[key]
        bo.bot_id = bot_id
        _write_overrides(bo, shared_dir)
    try:
        _prov.forget(key, bot_id=bot_id, shared_dir=shared_dir)
    except Exception as e:
        _log.warning(
            "overrides: provenance.forget failed for %s/%s (%s: %s)",
            bot_id, key, type(e).__name__, e,
        )
    return True


def mark_reviewed(
    shared_dir: Path,
    bot_id: str,
    key: str,
    *,
    set_by: str = "operator",
    note: str | None = None,
    now: datetime | None = None,
) -> OverrideEntry | None:
    """Clear ``needs_review`` for an override and re-attribute it.

    Use for the operator's "accept" action on an auto-promoted drift or
    Migration A entry. The value isn't changed; only the metadata that
    flags it for review is updated. Returns the updated entry, or None
    if the key wasn't present.

    ``note``: if provided, **replaces** the existing note. Pass ``None``
    (the default) to keep the existing note intact.

    Raises ``OverrideStateError`` if the existing on-disk file is malformed.
    """
    with _locked_modify(shared_dir, bot_id):
        bo = _read_for_write(shared_dir, bot_id)
        existing = bo.overrides.get(key)
        if existing is None:
            return None
        new = OverrideEntry(
            value=existing.value,
            set_by=set_by,
            set_at=_utc_iso(now),
            note=note if note is not None else existing.note,
            expires_at=existing.expires_at,
            needs_review=False,
        )
        bo.overrides[key] = new
        bo.bot_id = bot_id
        _write_overrides(bo, shared_dir)

    # Refresh provenance with the new set_by so the audit log reflects
    # the operator's acceptance (not the original auto-record).
    try:
        entry = by_path(key)
        if entry is not None:
            _prov.record(
                entry,
                existing.value,
                set_by=set_by,
                bot_id=bot_id,
                reason=new.note,
                shared_dir=shared_dir,
                now=now,
            )
    except Exception as e:
        _log.warning(
            "overrides: provenance.record (mark_reviewed) failed for %s/%s (%s: %s)",
            bot_id, key, type(e).__name__, e,
        )

    return new
