"""observations.access — Query helper for the observation stream.

Spec: docs/archive/specs/spec-rsi-layer-1-foundation-2026-04-18.md §6.

L1 shipments:
  - ``session_summaries()``: streams session_summary records from the
    existing annotation JSONL files.
  - ``turn_annotations()``: streams turn_annotation records from the same
    files.
  - ``tuples()``: raises ``TupleAccessNotYetAvailable`` with a message
    pointing at L3 — any generator that tries to use this before L3
    ships will fail loudly, not silently.
  - ``clusters()``: same as tuples.

The annotation JSONL format is established by the existing plugin code
(see ``packages/plugin/src/observer/SessionSummarizer.ts``). Each line is
one JSON record with a ``type`` field that is either
``"turn_annotation"`` or ``"session_summary"``. Daily files live at
``{shared_dir}/annotations/{bot_id}/{YYYY-MM-DD}.jsonl``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class TupleAccessNotYetAvailable(NotImplementedError):
    """Raised when a caller tries to access tuple-shaped observations in L1.

    Tuple extraction ships in L3. Generators that need pre-L3 observations
    should use ``session_summaries()`` or ``turn_annotations()`` instead.
    """

    def __init__(self, method: str = "tuples"):
        super().__init__(
            f"ObservationWindow.{method}() is not available until L3. "
            "Use session_summaries() or turn_annotations() in the meantime, "
            "or check spec-rsi-layer-3-cost-security-tuples-2026-04-18.md."
        )


def _iter_dates(start: date, end: date) -> Iterator[date]:
    """Yield dates from start to end inclusive, in ascending order."""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


@dataclass
class ObservationWindow:
    """A time- and bot-scoped window into the observation stream.

    Construct via the ``window()`` factory function rather than directly.

    Attributes:
        bot_id: bot to query
        start: inclusive start (UTC)
        end:   exclusive end (UTC)
        shared_dir: root of the evolve shared directory (defaults to canonical)
    """

    bot_id: str
    start: datetime
    end: datetime
    shared_dir: Path

    # ── Session summaries ──────────────────────────────────────────────────

    def session_summaries(self) -> Iterator[dict]:
        """Stream session_summary records overlapping the window.

        The JSONL files are one per day, keyed by ``bot_id``. We read all
        files whose date range overlaps the window and filter records by
        their ``ts`` timestamp (when present) or fall back to the file date.
        """
        yield from self._iter_records(record_type="session_summary")

    # ── Turn annotations ───────────────────────────────────────────────────

    def turn_annotations(self) -> Iterator[dict]:
        """Stream turn_annotation records overlapping the window."""
        yield from self._iter_records(record_type="turn_annotation")

    # ── Cost events (Budget Hawk v2) ───────────────────────────────────────

    def cost_events(self) -> Iterator[dict]:
        """Stream cost_event records overlapping the window.

        Two sources, in this priority order:
          1. ``cost_events-{date}.jsonl`` written by ``cost_event_converter.py``
             — the current source of truth. Every line is a cost_event;
             no type filter needed.
          2. Legacy ``{date}.jsonl`` records of ``type == cost_event``
             emitted by the plugin's old ``llm_output`` writer (silent
             since OC 2026.4.29 stopped firing the hook on embedded turns).
             Kept for historical reads ≤ 2026-04-21.

        Budget Hawk's forensics detectors are the primary consumer; the
        admin UI's Spike Explorer reads this too.
        """
        yield from self._iter_cost_events_converter()
        yield from self._iter_records(record_type="cost_event")

    def _iter_cost_events_converter(self) -> Iterator[dict]:
        """Yield cost_event records from the converter-written sibling files.

        Path: ``{shared_dir}/annotations/{bot}/cost_events-{date}.jsonl``.
        Records here are already type-filtered (the converter only writes
        cost_event), so we apply just the time-window filter.
        """
        annotations_dir = self.shared_dir / "annotations" / self.bot_id
        if not annotations_dir.exists():
            return
        start_date = self.start.astimezone(timezone.utc).date()
        end_date = self.end.astimezone(timezone.utc).date()
        for d in _iter_dates(start_date, end_date):
            path = annotations_dir / f"cost_events-{d.isoformat()}.jsonl"
            if not path.exists():
                continue
            for record in _read_jsonl(path):
                if not self._record_in_window(record):
                    continue
                yield record

    # ── Tuples (L3+) ───────────────────────────────────────────────────────

    def tuples(self) -> "Iterator[ObservationTupleType]":
        """Stream ObservationTuple records for this window."""
        # Import lazily to avoid circular imports (L1 shipped this as a stub)
        from observations.tuples import read_tuples_range

        yield from read_tuples_range(
            self.shared_dir, self.bot_id, self.start, self.end
        )

    def clusters(self, by: "ClusterSpecType | None" = None) -> "ClusterViewType":
        """Aggregate tuples in this window into cluster cells."""
        from collections import defaultdict

        from schema.observation import (
            ClusterCell,
            ClusterSpec,
            ClusterView,
        )

        spec = by if by is not None else ClusterSpec()
        group_keys = tuple(spec.group_by)
        filter_spec = spec.filter or {}

        grouped: dict[tuple, list] = defaultdict(list)
        for t in self.tuples():
            # Apply filter
            if filter_spec:
                skip = False
                for k, v in filter_spec.items():
                    if getattr(t, k, None) != v:
                        skip = True
                        break
                if skip:
                    continue
            key = tuple(getattr(t, k, None) for k in group_keys)
            grouped[key].append(t)

        cells: list[ClusterCell] = []
        for key, bucket in grouped.items():
            mood_dist: dict[str, int] = {}
            sessions: set[str] = set()
            engagement_total = 0
            earliest = bucket[0].timestamp_start
            latest = bucket[0].timestamp_start
            for t in bucket:
                mood_key = t.mood if t.mood else "unspecified"
                mood_dist[mood_key] = mood_dist.get(mood_key, 0) + 1
                sessions.add(t.session_id)
                engagement_total += int(t.engagement)
                if t.timestamp_start < earliest:
                    earliest = t.timestamp_start
                if t.timestamp_end > latest:
                    latest = t.timestamp_end
            cells.append(
                ClusterCell(
                    key=key,
                    tuple_count=len(bucket),
                    engagement_total=engagement_total,
                    distinct_sessions=len(sessions),
                    mood_distribution=mood_dist,
                    earliest=earliest,
                    latest=latest,
                )
            )

        # Sort cells deterministically
        cells.sort(key=lambda c: tuple(str(k) if k is not None else "" for k in c.key))
        return ClusterView(spec=spec, cells=cells)

    # ── Misc helpers ───────────────────────────────────────────────────────

    def file_paths(self) -> list[Path]:
        """Return the annotation file paths this window would read from.

        Useful for tests and for generators that want to sanity-check
        before reading.
        """
        paths: list[Path] = []
        annotations_dir = self.shared_dir / "annotations" / self.bot_id
        if not annotations_dir.exists():
            return paths
        start_date = self.start.astimezone(timezone.utc).date()
        end_date = self.end.astimezone(timezone.utc).date()
        for d in _iter_dates(start_date, end_date):
            path = annotations_dir / f"{d.isoformat()}.jsonl"
            if path.exists():
                paths.append(path)
        return paths

    def _iter_records(self, *, record_type: str) -> Iterator[dict]:
        """Yield records of ``record_type`` that fall within the window.

        Records that don't carry a timestamp we can locate get yielded
        anyway (better to see them than to filter them out on missing data).
        """
        for path in self.file_paths():
            for record in _read_jsonl(path):
                if record.get("type") != record_type:
                    continue
                if not self._record_in_window(record):
                    continue
                yield record

    def _record_in_window(self, record: dict) -> bool:
        """Best-effort timestamp filter.

        Different record shapes carry timestamps under different keys.
        ``ts`` is the canonical key — every current producer writes it
        (turn_annotation and session_summary via the plugin observers,
        cost_event via both the converter and the legacy plugin writer).
        The other candidates are spec-era shapes kept for historical data.
        We try the candidates in order and return True if the first one
        found falls in the window. Records with no recognizable timestamp
        pass through (we prefer to include rather than exclude on
        incomplete data).
        """
        for key in ("ts", "session_end", "session_start", "timestamp", "at"):
            raw = record.get(key)
            if not raw:
                continue
            ts = _parse_iso8601(raw)
            if ts is None:
                continue
            if self.start <= ts < self.end:
                return True
            # Found a timestamp but it's out of window
            return False
        return True  # No recognizable timestamp; keep it


def _parse_iso8601(raw: str) -> datetime | None:
    """Parse a few common ISO-8601 shapes. Returns None on failure."""
    if not isinstance(raw, str):
        return None
    # Normalize the trailing Z (UTC) → +00:00 for fromisoformat
    candidate = raw
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive — assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_jsonl(path: Path) -> Iterator[dict]:
    """Yield dict records from a JSONL file; skip malformed lines."""
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return
    with handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                # Skip malformed lines silently; log upstream if needed
                continue
            if isinstance(record, dict):
                yield record


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def window(
    bot_id: str,
    *,
    days: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    shared_dir: Path | str = Path("/Users/Shared/evolve"),
) -> ObservationWindow:
    """Construct an ObservationWindow.

    Two usage styles:

        window(bot_id, days=7)                  # last 7 days ending now
        window(bot_id, start=..., end=...)      # explicit range
    """
    if start is None and end is None:
        if days is None:
            raise ValueError("window(): must specify days or (start, end)")
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
    elif start is not None and end is not None:
        start_dt = start
        end_dt = end
    else:
        raise ValueError("window(): specify both start and end, or days alone")

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    return ObservationWindow(
        bot_id=bot_id,
        start=start_dt,
        end=end_dt,
        shared_dir=Path(shared_dir),
    )
