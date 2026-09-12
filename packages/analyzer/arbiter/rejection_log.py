"""arbiter.rejection_log — Append-only log of dismissed/rejected proposals.

The point of this log is to give generators a memory: before re-emitting
a proposal whose fingerprint was just rejected, the generator should
back off for a cooldown window. Without this, a sensor-style generator
that re-detects the same condition every cycle will queue a fresh copy
of an already-rejected proposal as soon as the operator clears the last
one — annoying at best, ignored at worst.

The log lives at ``{shared_dir}/feedback/rejections.jsonl`` (the path the
admin server already references in ``_move_proposal``). Each line is a
self-contained JSON record:

    {
      "ts":          ISO8601 UTC,
      "proposal_id": <uuid>,
      "fingerprint": <sha256 hex>,    # arbiter.dedup.compute_fingerprint
      "generator_id": <generator id>,
      "bot_id":      <bot id> | null,
      "dimension":   <proposal.dimension>,
      "trigger_observations": [...],   # for human-readable signature
      "problem":     <proposal.problem>,
      "actor":       "user:<id>" | "system" | etc.,
      "reason":      <free-form short reason>,
      "note":        <free-form longer note>
    }

Append-only, atomic line writes (single JSON line per write). No
compaction; rotate by the existing retention infra if needed.

Producers: the admin server's dismiss handler.
Consumers: generator context factories — they read recent rejections to
populate ``recent_rejection_fingerprints`` on the per-generator context
so ``observe()`` can suppress repeats.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evolve_util import now_iso_offset as _utc_now_iso

from arbiter.dedup import compute_fingerprint
from schema.proposal import Proposal


def rejection_log_path(shared_dir: Path) -> Path:
    """Path to the rejection JSONL log."""
    return Path(shared_dir) / "feedback" / "rejections.jsonl"


@dataclass(frozen=True)
class RejectionEntry:
    ts: str
    proposal_id: str
    fingerprint: str
    generator_id: str
    bot_id: str | None
    dimension: str
    trigger_observations: list[str]
    problem: str
    actor: str
    reason: str
    note: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "proposal_id": self.proposal_id,
            "fingerprint": self.fingerprint,
            "generator_id": self.generator_id,
            "bot_id": self.bot_id,
            "dimension": self.dimension,
            "trigger_observations": list(self.trigger_observations),
            "problem": self.problem,
            "actor": self.actor,
            "reason": self.reason,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RejectionEntry":
        return cls(
            ts=str(data.get("ts", "")),
            proposal_id=str(data.get("proposal_id", "")),
            fingerprint=str(data.get("fingerprint", "")),
            generator_id=str(data.get("generator_id", "")),
            bot_id=data.get("bot_id"),
            dimension=str(data.get("dimension", "")),
            trigger_observations=list(data.get("trigger_observations") or []),
            problem=str(data.get("problem", "")),
            actor=str(data.get("actor", "")),
            reason=str(data.get("reason", "")),
            note=str(data.get("note", "")),
        )


def write_rejection(
    shared_dir: Path,
    proposal: Proposal,
    *,
    actor: str,
    reason: str = "",
    note: str = "",
) -> None:
    """Append a rejection entry for ``proposal`` to the log.

    Caller is the dismiss/reject endpoint, after the proposal has been
    successfully transitioned to a terminal state. Failures here should
    NOT break the dismiss flow — wrap in a try/except at the call site.
    """
    fp = compute_fingerprint(proposal)
    entry = RejectionEntry(
        ts=_utc_now_iso(),
        proposal_id=proposal.id,
        fingerprint=fp,
        generator_id=proposal.generator_id,
        bot_id=proposal.bot_id,
        dimension=proposal.dimension,
        trigger_observations=list(proposal.trigger_observations),
        problem=proposal.problem,
        actor=actor,
        reason=reason,
        note=note,
    )
    path = rejection_log_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict()) + "\n")


def read_recent_rejections(
    shared_dir: Path,
    *,
    generator_id: str | None = None,
    bot_id: str | None = None,
    within_days: int = 14,
    now: datetime | None = None,
) -> list[RejectionEntry]:
    """Return rejection entries newer than ``within_days``, optionally filtered.

    Generators query this from their context factory to pre-compute a
    set of recently-rejected fingerprints for cooldown enforcement.
    """
    path = rejection_log_path(shared_dir)
    if not path.exists():
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=within_days)
    out: list[RejectionEntry] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            entry = RejectionEntry.from_dict(data)
        except (TypeError, ValueError):
            continue
        # Filter: generator + bot
        if generator_id is not None and entry.generator_id != generator_id:
            continue
        if bot_id is not None and entry.bot_id != bot_id:
            continue
        # Filter: recency
        try:
            ts = datetime.fromisoformat(entry.ts)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        out.append(entry)
    return out


def recent_rejection_fingerprints(
    shared_dir: Path,
    *,
    generator_id: str,
    bot_id: str | None = None,
    within_days: int = 14,
    now: datetime | None = None,
) -> set[str]:
    """Return just the fingerprints of recent rejections — the common case
    a generator needs to enforce a cooldown. Convenience wrapper over
    :func:`read_recent_rejections`."""
    entries = read_recent_rejections(
        shared_dir,
        generator_id=generator_id,
        bot_id=bot_id,
        within_days=within_days,
        now=now,
    )
    return {e.fingerprint for e in entries}
