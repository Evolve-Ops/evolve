"""Persistent do-not-reflag list for the prompt-injection scanner.

Spec: docs/archive/specs/spec-security-warden-completion-2026-04-18.md §4.5.

Mirrors the profile inferrer's ``do_not_infer`` mechanism. When the same
``(bot_id, pattern_set)`` injection signature gets dismissed N times in
a row, the scanner promotes it to a suppression list — subsequent
matches log a counter but no longer emit a proposal.

Storage: ``{shared_dir}/security_warden/do_not_reflag.json``
Owner: the ``evolve`` user (writes go through atomic temp-rename, no
sudo, since ``{shared_dir}`` is evolve-owned).

Per-run dedup (in ``observe.py``) handles "same conversation, same
pattern" within one scan window. This module handles the longer-term
"this user/pod always types this phrase legitimately" case.

Auto-promotion threshold defaults to 3 dismissals — small enough to be
useful in alpha, large enough that an operator slip-click doesn't
permanently silence a real attack class.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1
DEFAULT_PROMOTION_THRESHOLD = 3
_FILE_NAME = "do_not_reflag.json"


def _store_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "security_warden" / _FILE_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(bot_id: str, pattern_set: Iterable[str]) -> str:
    """Stable key for a (bot, pattern_set) pair: sorted, comma-joined."""
    return f"{bot_id}:{','.join(sorted(set(pattern_set)))}"


@dataclass
class _State:
    """In-memory representation of do_not_reflag.json."""

    schema_version: int = SCHEMA_VERSION
    dismissals: dict[str, dict] = field(default_factory=dict)
    suppressed: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "dismissals": dict(self.dismissals),
            "suppressed": list(self.suppressed),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_State":
        if not isinstance(data, dict):
            return cls()
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            dismissals=dict(data.get("dismissals") or {}),
            suppressed=list(data.get("suppressed") or []),
        )


_lock = Lock()


def _load(shared_dir: Path) -> _State:
    path = _store_path(shared_dir)
    if not path.exists():
        return _State()
    try:
        return _State.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("do_not_reflag: failed to read %s (%s); starting empty", path, e)
        return _State()


def _save(shared_dir: Path, state: _State) -> None:
    path = _store_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def is_suppressed(
    shared_dir: Path, bot_id: str, pattern_set: Iterable[str]
) -> bool:
    """True if the (bot_id, pattern_set) signature is on the suppression list."""
    if not pattern_set:
        return False
    target_key = _key(bot_id, pattern_set)
    state = _load(shared_dir)
    for entry in state.suppressed:
        if _key(entry.get("bot_id", ""), entry.get("pattern_set", [])) == target_key:
            return True
    return False


def record_dismissal(
    shared_dir: Path,
    bot_id: str,
    pattern_set: Iterable[str],
    *,
    threshold: int = DEFAULT_PROMOTION_THRESHOLD,
) -> dict:
    """Record one dismissal; auto-promote to suppression on the Nth.

    Returns a dict ``{"count": int, "promoted": bool}`` describing what happened.
    """
    pattern_list = sorted(set(pattern_set))
    if not pattern_list:
        return {"count": 0, "promoted": False}

    target_key = _key(bot_id, pattern_list)
    with _lock:
        state = _load(shared_dir)

        entry = state.dismissals.get(target_key) or {
            "bot_id": bot_id,
            "pattern_set": pattern_list,
            "count": 0,
            "first_seen": _utc_now(),
        }
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_seen"] = _utc_now()
        entry["bot_id"] = bot_id
        entry["pattern_set"] = pattern_list
        state.dismissals[target_key] = entry

        promoted = False
        already_suppressed = any(
            _key(s.get("bot_id", ""), s.get("pattern_set", [])) == target_key
            for s in state.suppressed
        )
        if entry["count"] >= threshold and not already_suppressed:
            state.suppressed.append({
                "bot_id": bot_id,
                "pattern_set": pattern_list,
                "added_at": _utc_now(),
                "reason": f"auto-promoted after {entry['count']} dismissals",
                "source": "auto",
            })
            promoted = True

        _save(shared_dir, state)
        return {"count": entry["count"], "promoted": promoted}


def add_suppression(
    shared_dir: Path,
    bot_id: str,
    pattern_set: Iterable[str],
    *,
    reason: str = "manual",
) -> bool:
    """Manually add a suppression. Returns True if added, False if duplicate."""
    pattern_list = sorted(set(pattern_set))
    if not pattern_list:
        return False
    target_key = _key(bot_id, pattern_list)
    with _lock:
        state = _load(shared_dir)
        for s in state.suppressed:
            if _key(s.get("bot_id", ""), s.get("pattern_set", [])) == target_key:
                return False
        state.suppressed.append({
            "bot_id": bot_id,
            "pattern_set": pattern_list,
            "added_at": _utc_now(),
            "reason": reason,
            "source": "manual",
        })
        _save(shared_dir, state)
        return True


def remove_suppression(
    shared_dir: Path, bot_id: str, pattern_set: Iterable[str]
) -> bool:
    """Remove a suppression. Returns True if removed, False if not found."""
    pattern_list = sorted(set(pattern_set))
    if not pattern_list:
        return False
    target_key = _key(bot_id, pattern_list)
    with _lock:
        state = _load(shared_dir)
        before = len(state.suppressed)
        state.suppressed = [
            s for s in state.suppressed
            if _key(s.get("bot_id", ""), s.get("pattern_set", [])) != target_key
        ]
        if len(state.suppressed) == before:
            return False
        # Also clear the dismissal counter so a future flagging restarts cleanly
        state.dismissals.pop(target_key, None)
        _save(shared_dir, state)
        return True


def list_suppressions(shared_dir: Path) -> list[dict]:
    """Return all suppression entries (bot_id, pattern_set, added_at, reason, source)."""
    state = _load(shared_dir)
    return list(state.suppressed)
