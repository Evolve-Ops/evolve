"""generators._intent_memory — Append-only dedup log for intent-aware generators.

Spec: internal/spec-config-intent-system-2026-05-21.md §2.5 + §4.

A generator that finds a deliberate-deviation (intent-present) signal must
not re-emit the same audit-only Signal every sweep. This module tracks
"already-emitted" by ``(generator_id, signature)`` against an append-only
JSONL log at ``{shared_dir}/config_intents/_generator_memory.jsonl``.

Signature shape per spec: ``<bot_id>:<field_path>:<reason_category>:<intent_id>``.
``reason_category ∈ {audit_only, stale_flagged}``. Generators construct
the signature themselves; this module treats it as an opaque string.

Dedup window: 90 days. Older entries don't block re-emission so an intent
the operator forgot about gets a periodic reminder instead of silence
forever. Aligns with the Signal store's 90-day archive retention
(CLAUDE.md "Signal store").

JSONL (not JSON) so concurrent generator runs append without rewriting,
and partial-write recovery is automatic (last-line corruption skipped).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_SHARED_DIR = Path("/Users/Shared/evolve")
LOG_RELATIVE_PATH = Path("config_intents") / "_generator_memory.jsonl"
DEDUP_WINDOW_DAYS = 90


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _log_path(shared_dir: Path | None) -> Path:
    base = Path(shared_dir) if shared_dir is not None else DEFAULT_SHARED_DIR
    return base / LOG_RELATIVE_PATH


def _parse_iso(at: str) -> datetime | None:
    """Tolerant ISO parse — accepts trailing 'Z' or explicit offset."""
    if not isinstance(at, str):
        return None
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_log_entries(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # Partial-write artifact — skip, don't fail. JSONL's
                    # value is that one corrupt line never blocks the rest.
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def already_emitted(
    *, generator_id: str, signature: str,
    shared_dir: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if ``(generator_id, signature)`` was logged within the window.

    Reads the full log on each call. Acceptable: a typical pod has at most
    a few hundred entries per year, well within fast-read territory. If
    the log grows past a few MB we revisit (open question §2.5 retention).
    """
    if not generator_id or not signature:
        return False
    cutoff = (now or _utc_now()) - timedelta(days=DEDUP_WINDOW_DAYS)
    for entry in _iter_log_entries(_log_path(shared_dir)):
        if entry.get("generator_id") != generator_id:
            continue
        if entry.get("signature") != signature:
            continue
        at = _parse_iso(entry.get("at", ""))
        if at is None:
            # Entry without parseable timestamp — treat as "long ago", so
            # it doesn't suppress current emission. Safer than asserting.
            continue
        if at >= cutoff:
            return True
    return False


def record_emission(
    *, generator_id: str, signature: str,
    event: str = "audit_signal_emitted",
    shared_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Append one emission record to the dedup log.

    Auto-creates the parent directory on first write. Append is the only
    write mode; never rewrites the log here (retention pruning is a
    separate, future-cron concern). Failures are swallowed — a missing
    log entry causes one redundant emission next sweep, which is much
    less bad than a generator crashing on a logging error.
    """
    if not generator_id or not signature:
        return
    path = _log_path(shared_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    record = {
        "at": (now or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator_id": generator_id,
        "signature": signature,
        "event": event,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        with path.open("a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return
