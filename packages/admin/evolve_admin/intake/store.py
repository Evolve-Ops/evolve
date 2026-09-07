"""Filesystem-backed intake persistence.

Mirrors :mod:`signals.store` — see that module for the design pattern.
The intake on-disk layout is described in
``internal/spec-primary-bot-interface-2026-05-14.md`` §6.2 and in the
``evolve_admin.intake`` package docstring.

Atomic write convention: temp-file + ``os.replace`` in the *same* dir
(not /tmp), mode 0o644 so anyone with read on the directory can read
the file. Owned by whichever user the admin server runs as (``evolve``).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from evolve_util import atomic_write_json, now_iso_offset as _utc_now_iso

from .envelope import ActivityEvent, Intake, IntakeState


Subdir = Literal["open", "triaged", "filed", "closed"]

_STATE_TO_SUBDIR: dict[IntakeState, Subdir] = {
    "open": "open",
    "triaged": "triaged",
    "filed": "filed",
    "closed": "closed",
}

_ALL_SUBDIRS: tuple[Subdir, ...] = ("open", "triaged", "filed", "closed")
_ACTIVE_SUBDIRS: tuple[Subdir, ...] = ("open", "triaged")

# Valid state edges. open is the start state; closed is terminal.
# filed is reachable from open or triaged (admin may file without
# explicit triage); a filed intake can still be closed once resolved.
_TRANSITIONS: dict[IntakeState, frozenset[IntakeState]] = {
    "open": frozenset({"triaged", "filed", "closed"}),
    "triaged": frozenset({"filed", "closed", "open"}),  # un-triage allowed
    "filed": frozenset({"closed"}),
    "closed": frozenset({"open"}),  # re-open is permitted; rare but supported
}


class IllegalTransitionError(ValueError):
    """Raised when a state transition is not legal."""


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────


def intake_root(shared_dir: Path) -> Path:
    return Path(shared_dir) / "intake"


def intake_path(shared_dir: Path, intake_id: str, *, subdir: Subdir) -> Path:
    return intake_root(shared_dir) / subdir / f"{intake_id}.json"


def log_path(shared_dir: Path, day: str | None = None) -> Path:
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return intake_root(shared_dir) / "log" / f"{day}.jsonl"


def subdir_for_state(state: IntakeState) -> Subdir:
    return _STATE_TO_SUBDIR[state]


# ─────────────────────────────────────────────────────────────────────────────
# Atomic write
# ─────────────────────────────────────────────────────────────────────────────


def _write_intake_json(data: dict, path: Path) -> None:
    """Ensure the parent subdir, then atomic-write via the blessed primitive.

    Cross-user-read concern applies: the admin server writes; admin-UI
    and (eventually) the alert-notifier read — hence mode 0o644.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data, mode=0o644)


# Kept under the historical name with (data, path) argument order — tests
# monkeypatch ``store._atomic_write_json`` with that exact signature to
# inject write failures.
_atomic_write_json = _write_intake_json


# ─────────────────────────────────────────────────────────────────────────────
# IDs
# ─────────────────────────────────────────────────────────────────────────────


def new_intake_id() -> str:
    """``intake-YYYYMMDD-xxxx`` where xxxx is 4 hex chars.

    Date-prefixed so the on-disk listing is naturally chronological; hex
    suffix is short for chat-friendly references but with enough entropy
    that the same-day collision probability stays low (1 in 65k per pair).
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = secrets.token_hex(2)
    return f"intake-{day}-{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# Read / write primitives
# ─────────────────────────────────────────────────────────────────────────────


def write_intake(
    intake: Intake,
    shared_dir: Path,
    *,
    subdir: Subdir | None = None,
) -> Path:
    target: Subdir = subdir or subdir_for_state(intake.state)
    path = intake_path(shared_dir, intake.id, subdir=target)
    _atomic_write_json(intake.to_dict(), path)
    return path


def load_intake_file(path: Path) -> Intake | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Intake.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def find_intake(
    shared_dir: Path, intake_id: str
) -> tuple[Intake, Path, Subdir] | None:
    """Locate an intake across all subdirs."""
    for sd in _ALL_SUBDIRS:
        path = intake_path(shared_dir, intake_id, subdir=sd)
        ix = load_intake_file(path)
        if ix is not None:
            return ix, path, sd
    return None


def find_by_github_issue(
    shared_dir: Path,
    repo: str,
    number: int,
) -> Intake | None:
    """Locate the intake that filed a given GitHub issue.

    Phase 1 of the Issue Inbox: the upstream-issues-watcher needs to
    map an observed comment on `<repo>#<number>` back to the intake we
    promoted it from, so it can append an activity event.

    Implementation: linear scan of ``filed/`` (the only state that has
    a populated ``promotion`` block). The filed subdir is small enough
    in practice that an index isn't worth maintaining — typical pods
    file <100 issues/year.
    """
    if not repo or not isinstance(number, int) or number <= 0:
        return None
    target_url_substr = f"{repo}/issues/{number}"
    for ix in iter_intakes(shared_dir, subdirs=("filed",)):
        prom = ix.promotion
        # Two ways to match: explicit issue_number + repo-from-url, or
        # the substring match on github_issue_url. Both kept defensive
        # — we don't store repo separately on the promotion record so
        # the URL is our source of truth for which repo it landed on.
        if prom.github_issue_number == number and prom.github_issue_url:
            if target_url_substr in prom.github_issue_url:
                return ix
    return None


def append_activity(
    intake: Intake,
    event: ActivityEvent,
    shared_dir: Path,
) -> Intake:
    """Append an activity event and atomically rewrite the intake file.

    Updates ``intake.activity_log`` in place AND persists. The caller
    can decide whether to dispatch an alert separately; this function's
    job is durable record-keeping for the Inbox UI.
    """
    intake.activity_log.append(event)
    intake.updated_at = _utc_now_iso()
    write_intake(intake, shared_dir)
    return intake


def mark_activity_seen(
    intake: Intake,
    shared_dir: Path,
    *,
    cursor: str | None = None,
) -> Intake:
    """Move the operator's read cursor to ``cursor`` (default: now).

    Used when the operator opens an intake in the Inbox UI — clears
    its "unread" badge without modifying activity_log itself.
    """
    intake.last_seen_activity_at = cursor or _utc_now_iso()
    intake.updated_at = _utc_now_iso()
    write_intake(intake, shared_dir)
    return intake


def delete_intake(
    shared_dir: Path, intake_id: str, *, subdir: Subdir
) -> bool:
    path = intake_path(shared_dir, intake_id, subdir=subdir)
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


def iter_intakes(
    shared_dir: Path,
    *,
    subdirs: tuple[Subdir, ...] = _ALL_SUBDIRS,
    kind: str | None = None,
    state: IntakeState | None = None,
) -> Iterator[Intake]:
    """Yield intakes across the given subdirs.

    Sort order: id ascending (which is chronological-then-random by
    construction). Filter args of ``None`` mean "no constraint."
    """
    root = intake_root(shared_dir)
    for sd in subdirs:
        d = root / sd
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            ix = load_intake_file(p)
            if ix is None:
                continue
            if kind is not None and ix.kind != kind:
                continue
            if state is not None and ix.state != state:
                continue
            yield ix


# ─────────────────────────────────────────────────────────────────────────────
# State transitions
# ─────────────────────────────────────────────────────────────────────────────


def transition(
    intake: Intake,
    *,
    to: IntakeState,
    shared_dir: Path,
    actor: str | None = None,
    note: str | None = None,
) -> Intake:
    """Move ``intake`` to state ``to``, persist, and append a log line.

    Raises :class:`IllegalTransitionError` for invalid edges. Updates
    ``intake.state``, ``intake.updated_at`` in place; renames the file
    if the new state lives in a different subdir.
    """
    src = intake.state
    if to not in _TRANSITIONS.get(src, frozenset()):
        raise IllegalTransitionError(f"{src} → {to} is not a legal intake transition")

    old_path = intake_path(shared_dir, intake.id, subdir=subdir_for_state(src))
    intake.state = to
    intake.updated_at = _utc_now_iso()
    new_path = intake_path(shared_dir, intake.id, subdir=subdir_for_state(to))

    # Write the new file atomically first; only unlink the old one once
    # the new write succeeded so a crash mid-transition leaves the old
    # file readable rather than producing a vanished intake.
    _atomic_write_json(intake.to_dict(), new_path)
    if old_path != new_path and old_path.exists():
        try:
            old_path.unlink()
        except OSError:
            # If the old path can't be removed we have a duplicate.
            # Surface it via the log so retention can clean up, but
            # don't fail the transition — the new path is authoritative.
            pass

    _append_log(
        shared_dir,
        {
            "ts": intake.updated_at,
            "id": intake.id,
            "from": src,
            "to": to,
            "actor": actor,
            "note": note,
        },
    )
    return intake


def _append_log(shared_dir: Path, entry: dict) -> None:
    """Append a state-change line to today's JSONL log.

    Best-effort: log write failure is recorded as a print to stderr but
    does not fail the caller. Retention is handled separately.
    """
    p = log_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
