"""observations.tuples — Storage + query for ObservationTuple JSONL files.

Spec: docs/archive/specs/spec-rsi-layer-3-cost-security-tuples-2026-04-18.md §4.2.

Files live at ``{shared_dir}/observations/{bot_id}/{YYYY-MM-DD}.jsonl``.
Each line is one tuple's JSON. Writers append; readers stream.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from schema.observation import ObservationTuple


def _daily_path(shared_dir: Path, bot_id: str, day_iso: str) -> Path:
    return shared_dir / "observations" / bot_id / f"{day_iso}.jsonl"


def write_tuples(
    tuples: Iterable[ObservationTuple],
    *,
    shared_dir: Path,
    bot_id: str,
    day: datetime | None = None,
) -> Path:
    """Append a batch of tuples to today's JSONL for a bot.

    Ignores duplicates by ``source_hash`` if the file already contains
    them — prevents idempotency concerns if an extraction runs twice.
    Uses a temp-file + rename for the rewrite that includes new records;
    not truly append-atomic on filesystem but sufficient for local use.

    Returns the path written to.
    """
    if day is None:
        day = datetime.now(timezone.utc)
    day_iso = day.astimezone(timezone.utc).date().isoformat()
    path = _daily_path(shared_dir, bot_id, day_iso)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing (cheap for daily files) to dedupe by (source_hash, id)
    existing_hashes: set[str] = set()
    existing_ids: set[str] = set()
    if path.exists():
        for rec in _read_jsonl(path):
            sh = rec.get("source_hash")
            if isinstance(sh, str):
                existing_hashes.add(sh)
            rid = rec.get("id")
            if isinstance(rid, str):
                existing_ids.add(rid)

    new_records: list[dict] = []
    for t in tuples:
        if t.source_hash in existing_hashes and t.id in existing_ids:
            continue
        if t.id in existing_ids:
            # Same id but different source_hash — overwrite by letting it
            # pass through; storage is append-only semantically but readers
            # generally tolerate dup ids.
            pass
        new_records.append(t.to_dict())

    if not new_records:
        return path

    # Append to file (buffered)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            if path.exists():
                with path.open("r", encoding="utf-8") as old:
                    for line in old:
                        fh.write(line if line.endswith("\n") else line + "\n")
            for rec in new_records:
                fh.write(json.dumps(rec) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def read_tuples_for_day(
    shared_dir: Path,
    bot_id: str,
    day_iso: str,
) -> Iterator[ObservationTuple]:
    """Stream tuples from one day's JSONL. Missing file → empty iterator."""
    path = _daily_path(shared_dir, bot_id, day_iso)
    if not path.exists():
        return
    for record in _read_jsonl(path):
        try:
            yield ObservationTuple.from_dict(record)
        except (KeyError, ValueError, TypeError):
            # Skip malformed; upstream logging will catch
            continue


def read_tuples_range(
    shared_dir: Path,
    bot_id: str,
    start: datetime,
    end: datetime,
) -> Iterator[ObservationTuple]:
    """Stream tuples for a bot across all day files overlapping [start, end]."""
    from datetime import timedelta

    start_utc = start.astimezone(timezone.utc) if start.tzinfo else start.replace(
        tzinfo=timezone.utc
    )
    end_utc = end.astimezone(timezone.utc) if end.tzinfo else end.replace(
        tzinfo=timezone.utc
    )
    cur = start_utc.date()
    end_date = end_utc.date()
    while cur <= end_date:
        for t in read_tuples_for_day(shared_dir, bot_id, cur.isoformat()):
            # Filter by window — inclusive at both ends to make
            # "last N days" queries behave as operators expect.
            ts = _parse_iso(t.timestamp_start)
            if ts is not None and (ts < start_utc or ts > end_utc):
                continue
            yield t
        cur = cur + timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> Iterator[dict]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _parse_iso(raw: str) -> datetime | None:
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
