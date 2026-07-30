"""metrics.resolvers.security — Security Warden event-rate metrics.

Resolves:
  - ``security.injection_events_per_week`` — count of confirmed prompt-
    injection events emitted by Security Warden in the trailing 7 days,
    across every proposal subdir (a dismissed event still happened).

Source: ``{shared_dir}/proposals/{subdir}/<id>.json`` for each subdir.
Recognition: ``generator_id == "security_warden"`` AND any
``trigger_observations`` entry starts with ``"prompt_injection:"``.

Spec: docs/archive/specs/spec-security-warden-completion-2026-04-18.md §4.6.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metrics.registry import MetricSpec, MetricValue, register

from arbiter.store import proposals_root


_SHARED_DIR = Path("/Users/Shared/evolve")


def set_shared_dir(path: Path) -> None:
    """Test hook: override the shared-dir root used for proposal lookups."""
    global _SHARED_DIR
    _SHARED_DIR = Path(path)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_injection_proposal_data(data: dict) -> bool:
    if data.get("generator_id") != "security_warden":
        return False
    triggers = data.get("trigger_observations") or []
    return any(
        isinstance(t, str) and t.startswith("prompt_injection:")
        for t in triggers
    )


def _iter_candidate_files(shared_dir: Path, mtime_cutoff_ts: float):
    """Yield JSON proposal paths whose mtime is within the lookback window.

    Filters by `os.stat().st_mtime` before reading, so stale files in
    archived/ don't have to be parsed. As `archived/` grows unboundedly
    over a long-running pod, this turns the metric from O(N total) into
    O(K recent + cheap N stat calls).

    The mtime filter is conservative: created_at on the proposal is the
    authoritative timestamp (checked again after parsing), so files that
    were touched recently but represent old events still get filtered
    out at the second stage.
    """
    root = proposals_root(shared_dir)
    subdirs = ("pending", "snoozed", "applied", "archived")
    for sd in subdirs:
        dir_path = root / sd
        if not dir_path.exists():
            continue
        for path in dir_path.glob("*.json"):
            try:
                if path.stat().st_mtime < mtime_cutoff_ts:
                    continue
            except OSError:
                continue
            yield path


def resolve_injection_events_per_week(
    bot_id: str, as_of: datetime
) -> MetricValue:
    """Count injection events in [as_of - 7d, as_of] for ``bot_id``.

    Two-stage filter: skip files older than the window by mtime (cheap
    stat call), then parse the survivors and check ``created_at`` and
    ``bot_id`` / ``generator_id`` / trigger.
    """
    cutoff = as_of - timedelta(days=7)
    # Allow a 1-day mtime grace window — proposals can be touched (status
    # transitions write the file) after creation. The created_at check
    # below filters those exactly.
    mtime_cutoff_ts = (cutoff - timedelta(days=1)).timestamp()

    count = 0
    for path in _iter_candidate_files(_SHARED_DIR, mtime_cutoff_ts):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("bot_id") != bot_id:
            continue
        if not _is_injection_proposal_data(data):
            continue
        created = _parse_iso(data.get("created_at"))
        if created is None:
            continue
        if created < cutoff or created > as_of:
            continue
        count += 1

    return MetricValue(
        value=float(count),
        confidence=1.0,
        source_note=(
            f"prompt_injection events in {cutoff.isoformat()}..{as_of.isoformat()} "
            f"for {bot_id}"
        ),
    )


register(
    MetricSpec(
        name="security.injection_events_per_week",
        description=(
            "Count of confirmed prompt-injection events emitted by "
            "Security Warden in the trailing 7 days."
        ),
        unit="count",
        source="{shared_dir}/proposals/{subdir}/*.json (generator_id=security_warden, trigger_observations~=prompt_injection:*)",
    ),
    resolve_injection_events_per_week,
)
