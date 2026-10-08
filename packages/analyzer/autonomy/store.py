"""autonomy.store — the per-bot autonomy posture intent file.

Spec: internal/spec-autonomy-ladder-2026-06-10.md §2.2.

Storage location (the slack-policy precedent):
  ``{shared_dir}/bots/<bot_id>/autonomy.json``

owned by the ``evolve`` user under shared_dir — atomic temp-file +
``os.replace``, no /tmp + sudo dance. The file is the source of truth
for *intent*; ``autonomy.renderer`` renders it down to enforcement
surfaces.

Concurrency discipline (Phase 7.1-A, mirrors ``signals.store``):
every read-modify-write sequence holds a per-bot flock and re-reads
the file inside the lock (compare-and-swap) — UI promotion and a
future auto-demotion reflex may race. ``set_posture`` additionally
accepts ``expected_current_rung`` so a UI click made against a stale
row fails loudly (:class:`StalePostureError`) instead of silently
clobbering a newer transition.

History is append-only: every rung change appends a record; nothing
ever rewrites or prunes past entries. The history list IS the audit
surface's timeline (spec §4.1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from evolve_util import atomic_write_json, now_iso
from store_lock import locked as _flock

from . import catalog as _catalog


SCHEMA_VERSION = 1
AUTONOMY_FILENAME = "autonomy.json"
LOCK_FILE_NAME = ".autonomy.lock"

# Well-known actors (spec §2.2 set_by). Phase B adds primary_bot,
# proposal:<id>, auto_demotion:<signal_id>.
ACTOR_SHIPPED_DEFAULT = "shipped_default"
ACTOR_OPERATOR_UI = "operator_ui"
ACTOR_BACKFILL = "backfill_inferred"
# Phase B actors. proposal:<id> / auto_demotion:<signal_id> are prefixes
# (the suffix carries the provenance id); primary_bot is evo's chat path.
ACTOR_PRIMARY_BOT = "primary_bot"
ACTOR_PREFIX_PROPOSAL = "proposal:"
ACTOR_PREFIX_AUTO_DEMOTION = "auto_demotion:"


class StalePostureError(ValueError):
    """The caller's view of the current rung no longer matches the file.

    Raised by :func:`set_posture` when ``expected_current_rung`` is
    supplied and disagrees with the on-disk rung — the CAS failure
    path. Callers (the API route) surface this as "the posture changed
    underneath you; reload and retry."
    """


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class IntegrationPosture:
    """One integration's posture on one bot (spec §2.2 schema)."""

    integration_id: str
    kind: str
    rung: str
    rules: dict[str, Any] = field(default_factory=dict)
    set_by: dict[str, Any] = field(default_factory=dict)   # {"actor": ..., "note": ...}
    set_at: str = ""
    enforcement: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("integration_id")  # keyed by id in the file, not duplicated
        return d


@dataclass
class AutonomyDoc:
    """The whole per-bot file."""

    bot_id: str
    schema_version: int = SCHEMA_VERSION
    integrations: dict[str, IntegrationPosture] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bot_id": self.bot_id,
            "integrations": {
                iid: p.to_dict() for iid, p in sorted(self.integrations.items())
            },
        }


# ── Paths ─────────────────────────────────────────────────────────────────────


def autonomy_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "bots" / bot_id / AUTONOMY_FILENAME


def lock_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "bots" / bot_id / LOCK_FILE_NAME


# ── Load / save ───────────────────────────────────────────────────────────────


def load(shared_dir: Path, bot_id: str) -> AutonomyDoc | None:
    """Return the bot's stored posture doc, or ``None`` if absent.

    Raises ``ValueError`` on a present-but-malformed file — refusing to
    render against garbage is safer than silently re-defaulting (the
    slack-policy loader convention).
    """
    path = autonomy_path(shared_dir, bot_id)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"autonomy file {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"autonomy file {path} root is not an object")
    return _from_dict(data, bot_id_default=bot_id, path=path)


def save(shared_dir: Path, doc: AutonomyDoc) -> Path:
    """Atomically write the doc. Returns the final path.

    Validates every entry before write — callers don't get to persist
    invalid postures. Mutators below hold the per-bot flock around
    their load→mutate→save sequence; ``save`` itself only guarantees
    per-file atomicity (``atomic_write_json``).

    Mode 0644 is load-bearing: ``session_surface.load_autonomy_block``
    runs as the BOT user and must read this file to inject the
    guidance surface — an unreadable intent file would silently kill
    the procedural enforcement while every badge claims it's live.
    """
    errors = validate_doc(doc)
    if errors:
        raise ValueError(f"autonomy doc is invalid: {'; '.join(errors)}")
    path = autonomy_path(shared_dir, doc.bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, doc.to_dict(), mode=0o644)
    return path


def validate_doc(doc: AutonomyDoc) -> list[str]:
    errors: list[str] = []
    if doc.schema_version != SCHEMA_VERSION:
        errors.append(
            f"schema_version {doc.schema_version} != expected {SCHEMA_VERSION}"
        )
    if not doc.bot_id.strip():
        errors.append("bot_id is required")
    for iid, posture in doc.integrations.items():
        if posture.rung not in _catalog.RUNGS:
            errors.append(f"integrations[{iid}].rung={posture.rung!r} unknown")
            continue
        spec = _catalog.kind_spec(posture.kind)
        if spec is None:
            errors.append(f"integrations[{iid}].kind={posture.kind!r} unknown")
            continue
        for err in _catalog.validate_rules(spec, posture.rung, posture.rules):
            errors.append(f"integrations[{iid}]: {err}")
    return errors


# ── Mutators (flock + CAS) ────────────────────────────────────────────────────

_UNSET = object()


def set_posture(
    shared_dir: Path,
    bot_id: str,
    integration_id: str,
    *,
    rung: str,
    rules: dict[str, Any] | None = None,
    actor: str,
    note: str = "",
    kind: str | None = None,
    expected_current_rung: Any = _UNSET,
    history_extra: dict[str, Any] | None = None,
) -> IntegrationPosture:
    """Set an integration's rung — the single write path for promotion
    AND demotion (spec §3.1: one API, friction lives in the UI).

    Holds the per-bot flock across load→mutate→save and re-reads inside
    the lock, so a concurrent writer's transition is never clobbered.
    ``expected_current_rung`` (pass the rung the caller displayed; use
    ``None`` for "expected absent") turns a stale UI click into a
    :class:`StalePostureError` instead of a lost update.

    ``history_extra`` merges extra audit fields into the appended
    history record (never into the posture itself). The auto-demotion
    reflex stores ``prior_rules`` there so the one-click restore can
    resurrect the rung-3 rules block the demotion cleared.

    Raises ``ValueError`` for unknown rung/kind or an invalid rules
    block (rung 3 without rules, rules outside rung 3, vocabulary
    violations).
    """
    rules = dict(rules or {})
    if rung not in _catalog.RUNGS:
        raise ValueError(f"unknown rung {rung!r} (expected one of {_catalog.RUNGS})")

    with _flock(lock_path(shared_dir, bot_id)):
        doc = load(shared_dir, bot_id) or AutonomyDoc(bot_id=bot_id)
        current = doc.integrations.get(integration_id)

        if expected_current_rung is not _UNSET:
            current_rung = current.rung if current is not None else None
            if current_rung != expected_current_rung:
                raise StalePostureError(
                    f"{bot_id}/{integration_id}: expected current rung "
                    f"{expected_current_rung!r} but file has {current_rung!r} "
                    "— reload and retry"
                )

        resolved_kind = kind or (current.kind if current else None)
        if resolved_kind is None:
            binding = _catalog.binding_for(integration_id)
            resolved_kind = binding.kind if binding else None
        spec = _catalog.kind_spec(resolved_kind) if resolved_kind else None
        if resolved_kind is None or spec is None:
            raise ValueError(
                f"{bot_id}/{integration_id}: unknown kind {resolved_kind!r}"
            )
        errors = _catalog.validate_rules(spec, rung, rules)
        if errors:
            raise ValueError(
                f"{bot_id}/{integration_id}: {'; '.join(errors)}"
            )

        at = now_iso()
        record = {
            "at": at,
            "from": current.rung if current is not None else None,
            "to": rung,
            "actor": actor,
            "note": note,
        }
        for key, value in (history_extra or {}).items():
            record.setdefault(key, value)
        if current is None:
            current = IntegrationPosture(
                integration_id=integration_id, kind=resolved_kind, rung=rung,
            )
            doc.integrations[integration_id] = current
        current.kind = resolved_kind
        current.rung = rung
        current.rules = rules
        current.set_by = {"actor": actor, **({"note": note} if note else {})}
        current.set_at = at
        current.history.append(record)
        save(shared_dir, doc)
        return current


def ensure_entry(
    shared_dir: Path,
    bot_id: str,
    integration_id: str,
    *,
    kind: str,
    rung: str,
    actor: str,
    note: str = "",
) -> tuple[IntegrationPosture, bool]:
    """Find-or-create an integration entry (the backfill write path).

    Never overwrites an existing entry — backfill must not stomp a
    deliberate posture (spec §5.2). Returns ``(posture, created)``.
    """
    with _flock(lock_path(shared_dir, bot_id)):
        doc = load(shared_dir, bot_id) or AutonomyDoc(bot_id=bot_id)
        existing = doc.integrations.get(integration_id)
        if existing is not None:
            return existing, False
        posture = set_posture(
            shared_dir, bot_id, integration_id,
            rung=rung, actor=actor, note=note, kind=kind,
            expected_current_rung=None,
        )
        return posture, True


def record_enforcement(
    shared_dir: Path,
    bot_id: str,
    integration_id: str,
    enforcement: list[dict[str, Any]],
) -> IntegrationPosture | None:
    """Record the renderer's per-surface enforcement outcome (spec §2.2
    ``enforcement[]`` — written by the renderer, read by the audit).

    Not a posture change: no history entry, no set_by/set_at update.
    Returns the updated posture, or ``None`` when the entry vanished
    (benign race with a concurrent file rewrite; the next render
    re-records).
    """
    with _flock(lock_path(shared_dir, bot_id)):
        doc = load(shared_dir, bot_id)
        if doc is None:
            return None
        posture = doc.integrations.get(integration_id)
        if posture is None:
            return None
        posture.enforcement = list(enforcement)
        save(shared_dir, doc)
        return posture


# ── Dict <-> dataclass mapping ───────────────────────────────────────────────


def _from_dict(data: dict[str, Any], *, bot_id_default: str, path: Path) -> AutonomyDoc:
    """Parse a stored JSON document back into dataclasses.

    Forward-compatible at the field level (unknown fields dropped,
    missing fields defaulted); strict at the enum level — an unknown
    rung raises so a hand-edited file can't silently coerce to a
    default and lose operator intent (the slack-policy convention).
    """
    integrations: dict[str, IntegrationPosture] = {}
    raw_integrations = data.get("integrations") or {}
    if not isinstance(raw_integrations, dict):
        raise ValueError(f"autonomy file {path}: integrations is not an object")
    for iid, raw in raw_integrations.items():
        if not isinstance(raw, dict):
            continue
        rung = raw.get("rung")
        if rung not in _catalog.RUNGS:
            raise ValueError(
                f"autonomy file {path}: integrations[{iid}].rung={rung!r} "
                f"not in {_catalog.RUNGS}"
            )
        set_by = raw.get("set_by")
        if not isinstance(set_by, dict):
            set_by = {"actor": str(set_by)} if set_by else {}
        integrations[str(iid)] = IntegrationPosture(
            integration_id=str(iid),
            kind=str(raw.get("kind") or ""),
            rung=str(rung),
            rules=dict(raw.get("rules") or {}),
            set_by=set_by,
            set_at=str(raw.get("set_at") or ""),
            enforcement=[e for e in (raw.get("enforcement") or []) if isinstance(e, dict)],
            history=[h for h in (raw.get("history") or []) if isinstance(h, dict)],
        )
    return AutonomyDoc(
        bot_id=str(data.get("bot_id") or bot_id_default),
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        integrations=integrations,
    )


__all__ = [
    "ACTOR_BACKFILL",
    "ACTOR_OPERATOR_UI",
    "ACTOR_PREFIX_AUTO_DEMOTION",
    "ACTOR_PREFIX_PROPOSAL",
    "ACTOR_PRIMARY_BOT",
    "ACTOR_SHIPPED_DEFAULT",
    "AUTONOMY_FILENAME",
    "AutonomyDoc",
    "IntegrationPosture",
    "SCHEMA_VERSION",
    "StalePostureError",
    "autonomy_path",
    "ensure_entry",
    "load",
    "lock_path",
    "record_enforcement",
    "save",
    "set_posture",
    "validate_doc",
]
