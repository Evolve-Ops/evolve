#!/usr/bin/env python3
"""
evolve/recommendations.py — Shared recommendations library

All recommendation generators import write_recommendations() from here.
The admin UI and Better Engine read current.json via load_recommendations().

Storage layout (all paths per-bot — never cross-bot reads during generation):
  {sharedDir}/{botId}/recommendations/
    current.json   — active recommendations; rewritten on each generator run
    log.jsonl      — append-only audit history of all events
    profile.json   — bot capability profile (written by profile_builder.py)

Privacy invariant: generators receive a single bot_id and read/write only
from {sharedDir}/{botId}/. Cross-bot aggregation is the Better Engine's job,
not the generators'.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from evolve_util import now_iso_micro as _now_iso

SCHEMA_VERSION = 1
DEFAULT_EXPIRY_DAYS = 90


def _recs_dir(bot_id: str, shared_dir: Path) -> Path:
    return shared_dir / bot_id / "recommendations"


def _current_path(bot_id: str, shared_dir: Path) -> Path:
    return _recs_dir(bot_id, shared_dir) / "current.json"


def _log_path(bot_id: str, shared_dir: Path) -> Path:
    return _recs_dir(bot_id, shared_dir) / "log.jsonl"


def _ensure_dir(bot_id: str, shared_dir: Path) -> None:
    """Create {sharedDir}/{botId}/recommendations/ if absent."""
    d = _recs_dir(bot_id, shared_dir)
    d.mkdir(parents=True, exist_ok=True)


def _staged_write_json(path: Path, data: object) -> None:
    """Write JSON via /tmp staging + sudo-cp fallback.

    NOT ``evolve_util.atomic_write_json`` — the destination may be
    bot-owned, so this is the cross-user staged-write pattern (same
    family as ``safe_write_bot_config``), not the same-dir temp+rename
    primitive."""
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-recs-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    try:
        import shutil
        shutil.move(tmp, str(path))
    except PermissionError:
        # Bot-owned destination — use sudo cp to transfer the pre-written tmp file.
        subprocess.run(["sudo", "/bin/cp", tmp, str(path)],
                       capture_output=True, check=False)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    except OSError:
        # Non-permission OS error (disk full, bad path, etc.) — don't silently
        # swallow it into a sudo attempt that will also fail.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_log(path: Path, entry: dict) -> None:
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# Fields that change every run even when the recommendation is substantively
# the same — excluded from the content signature so a stable rec does not
# re-emit an "updated" event (and re-grow log.jsonl) on each generator pass.
_VOLATILE_REC_KEYS = frozenset({
    "generated_at", "status", "status_updated_at", "status_note",
    "expires_at", "schema_version",
})


def _rec_content_sig(rec: dict) -> str:
    """Stable signature of a recommendation's substantive content (the diff-gate)."""
    return json.dumps(
        {k: rec[k] for k in sorted(rec) if k not in _VOLATILE_REC_KEYS},
        sort_keys=True, default=str,
    )


def _prune_log(path: Path, *, retain_days: int = DEFAULT_EXPIRY_DAYS) -> None:
    """Drop log.jsonl lines older than retain_days; best-effort, never raises.

    The log is append-only audit history with NO production reader (prod reads
    only current.json), so it would grow unbounded. 90 days matches
    DEFAULT_EXPIRY_DAYS — a rec's whole lifecycle fits inside the window.
    Lines whose ts is missing/unparseable are kept (never risk dropping data
    we can't age)."""
    try:
        if not path.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
        kept: list[str] = []
        changed = False
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ts = datetime.fromisoformat(json.loads(line)["ts"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                kept.append(line)
                continue
            if ts < cutoff:
                changed = True
            else:
                kept.append(line)
        if changed:
            path.write_text(("\n".join(kept) + "\n") if kept else "")
    except OSError:
        pass


def load_recommendations(
    bot_id: str,
    shared_dir: Path,
    *,
    status: Optional[list[str]] = None,
    category: Optional[str] = None,
) -> list[dict]:
    """Read active recommendations from current.json.

    Args:
        status: if provided, filter to recs with status in this list
        category: if provided, filter to this category only
    """
    path = _current_path(bot_id, shared_dir)
    if not path.exists():
        return []
    try:
        recs = json.loads(path.read_text())
        if not isinstance(recs, list):
            return []
    except Exception:
        return []

    if status:
        recs = [r for r in recs if r.get("status") in status]
    if category:
        recs = [r for r in recs if r.get("category") == category]
    return recs


def write_recommendations(
    bot_id: str,
    new_recs: list[dict],
    shared_dir: Path,
) -> None:
    """Merge new recommendations into current.json; append events to log.jsonl.

    Merge rules:
    - New rec_id: add to current, log "created"
    - Existing rec_id, status "new" or "surfaced": full update, log "updated"
    - Existing rec_id, status "accepted" or "dismissed": preserve status,
      update evidence/rationale only, log "refreshed"
    - Entries past expires_at: remove from current, log "expired"
    """
    _ensure_dir(bot_id, shared_dir)
    cur_path = _current_path(bot_id, shared_dir)
    log_path = _log_path(bot_id, shared_dir)
    now = _now_iso()

    # Load existing entries keyed by rec_id
    existing: dict[str, dict] = {}
    if cur_path.exists():
        try:
            for r in json.loads(cur_path.read_text()):
                if isinstance(r, dict) and r.get("rec_id"):
                    existing[r["rec_id"]] = r
        except Exception:
            pass

    # Expire stale entries
    for rec_id in list(existing.keys()):
        exp = existing[rec_id].get("expires_at")
        if exp and exp < now:
            _append_log(log_path, {"rec_id": rec_id, "event": "expired", "ts": now})
            del existing[rec_id]

    # Merge new recommendations
    default_expiry = (
        datetime.now(timezone.utc) + timedelta(days=DEFAULT_EXPIRY_DAYS)
    ).isoformat()

    for rec in new_recs:
        rec_id = rec.get("rec_id")
        if not rec_id:
            continue

        rec.setdefault("schema_version", SCHEMA_VERSION)
        rec.setdefault("bot_id", bot_id)
        rec.setdefault("generated_at", now)
        rec.setdefault("expires_at", default_expiry)

        if rec_id not in existing:
            # Brand new recommendation
            rec.setdefault("status", "new")
            rec.setdefault("status_updated_at", None)
            rec.setdefault("status_note", None)
            existing[rec_id] = rec
            _append_log(log_path, {"rec_id": rec_id, "event": "created", "ts": now, **_log_fields(rec)})
        else:
            old = existing[rec_id]
            if old.get("status") in ("accepted", "dismissed"):
                # Preserve operator decision; only refresh data fields
                old["evidence"] = rec.get("evidence", old.get("evidence"))
                old["rationale"] = rec.get("rationale", old.get("rationale"))
                old["generated_at"] = now
                _append_log(log_path, {"rec_id": rec_id, "event": "refreshed", "ts": now})
            else:
                # Active rec — full update, preserve status lifecycle fields.
                # Diff-gate the log append: only record "updated" when the rec's
                # substantive content actually changed, so a stable rec re-emitted
                # every generator run doesn't re-grow log.jsonl with no-op events.
                content_changed = _rec_content_sig(old) != _rec_content_sig(rec)
                rec["status"] = old.get("status", "new")
                rec["status_updated_at"] = old.get("status_updated_at")
                rec["status_note"] = old.get("status_note")
                existing[rec_id] = rec
                if content_changed:
                    _append_log(log_path, {"rec_id": rec_id, "event": "updated", "ts": now, **_log_fields(rec)})

    _staged_write_json(cur_path, list(existing.values()))
    _prune_log(log_path)


def update_recommendation_status(
    bot_id: str,
    rec_id: str,
    status: str,
    shared_dir: Path,
    *,
    note: Optional[str] = None,
) -> Optional[dict]:
    """Update the status of a single recommendation.

    Returns the updated recommendation dict, or None if rec_id not found.
    Called by the admin server when an operator dismisses or accepts a rec.
    """
    _ensure_dir(bot_id, shared_dir)
    cur_path = _current_path(bot_id, shared_dir)
    log_path = _log_path(bot_id, shared_dir)
    now = _now_iso()

    recs: list[dict] = []
    if cur_path.exists():
        try:
            recs = json.loads(cur_path.read_text())
        except Exception:
            return None

    updated = None
    for rec in recs:
        if rec.get("rec_id") == rec_id:
            rec["status"] = status
            rec["status_updated_at"] = now
            rec["status_note"] = note
            updated = rec
            break

    if updated is None:
        return None

    _staged_write_json(cur_path, recs)
    _append_log(log_path, {
        "rec_id": rec_id,
        "event": "status_change",
        "ts": now,
        "status": status,
        "note": note,
    })
    _prune_log(log_path)
    return updated


def _log_fields(rec: dict) -> dict:
    """Extract minimal fields for log entries (avoid bloating log with full payload)."""
    return {
        k: rec[k] for k in ("type", "source", "category", "title", "confidence")
        if k in rec
    }
