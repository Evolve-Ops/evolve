"""content_scan.suppressions — operator-acknowledged Mark-Reviewed records.

Spec: internal/spec-prompt-injection-scanner-2026-05-10.md §5.3.

When the operator inspects a content_scan_match and decides it's benign
they click "Mark Reviewed". That writes a suppression entry keyed by
(bot_id, file, pattern_id, line_range). The scanner consults the
suppression store on each cycle: a match whose tuple is suppressed gets
dropped from the findings list (and the corresponding signal swept-
resolved as cleared).

Suppressions expire after 30 days by default — operator-tunable later.
The list is itself an input to a future "graduate to permanent
exclusion" suggestion flow (Phase B).

On-disk layout: ``{shared_dir}/content-scan/suppressions/<bot_id>.json``.
One file per bot, all suppressions for that bot. Atomic writes via
temp-file + rename.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_util import now_iso as _utc_now_iso

DEFAULT_TTL_DAYS = 30

# "Permanent" suppression — ten years out. Not literally permanent
# (revisited under operator review when the scanner notices an aged
# suppression on every cycle); long enough that the operator isn't
# re-triaging the same match every month. Phase B graduation flow.
PERMANENT_TTL_DAYS = 3650


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None




# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Suppression:
    bot_id: str
    file: str
    pattern_id: str
    line_range: list[int]  # [start, end] inclusive; single-line uses [n, n]
    excerpt: str  # the matched text at suppression time (for operator review)
    reviewed_at: str
    expires_at: str
    reviewer_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        exp = _parse_iso(self.expires_at)
        if exp is None:
            # Malformed / missing expiry: fail closed. Treat as expired so
            # prune_expired removes the entry on the next sweep and the
            # signal it would have suppressed fires again. The alternative
            # (fail open → "never expires") risks invisible suppression of
            # real findings if a hand-edit corrupts the file.
            return True
        return exp <= now

    def signature_key(self) -> tuple[str, str, str, int, int]:
        if len(self.line_range) >= 2:
            lo, hi = int(self.line_range[0]), int(self.line_range[1])
        elif self.line_range:
            lo = hi = int(self.line_range[0])
        else:
            lo = hi = 0
        return (self.bot_id, self.file, self.pattern_id, lo, hi)


# ── On-disk layout ────────────────────────────────────────────────────────────

def suppressions_dir(shared_dir: Path) -> Path:
    return shared_dir / "content-scan" / "suppressions"


def suppressions_path(shared_dir: Path, bot_id: str) -> Path:
    return suppressions_dir(shared_dir) / f"{bot_id}.json"


def load_for_bot(shared_dir: Path, bot_id: str) -> list[Suppression]:
    p = suppressions_path(shared_dir, bot_id)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Suppression] = []
    for item in raw.get("suppressions") or []:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Suppression(
                bot_id=str(item.get("bot_id") or bot_id),
                file=str(item.get("file") or ""),
                pattern_id=str(item.get("pattern_id") or ""),
                line_range=[int(x) for x in (item.get("line_range") or [])],
                excerpt=str(item.get("excerpt") or ""),
                reviewed_at=str(item.get("reviewed_at") or ""),
                expires_at=str(item.get("expires_at") or ""),
                reviewer_note=str(item.get("reviewer_note") or ""),
            ))
        except (ValueError, TypeError):
            continue
    return out


def load_all(shared_dir: Path) -> list[Suppression]:
    """Return every suppression across every bot file in the store."""
    d = suppressions_dir(shared_dir)
    if not d.exists():
        return []
    out: list[Suppression] = []
    for f in sorted(d.glob("*.json")):
        out.extend(load_for_bot(shared_dir, f.stem))
    return out


def _write_for_bot(shared_dir: Path, bot_id: str, items: list[Suppression]) -> None:
    target = suppressions_path(shared_dir, bot_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    payload = {
        "bot_id": bot_id,
        "updated_at": _utc_now_iso(),
        "suppressions": [s.to_dict() for s in items],
    }
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(target)


# ── Mutation API ──────────────────────────────────────────────────────────────

def add(
    shared_dir: Path,
    *,
    bot_id: str,
    file: str,
    pattern_id: str,
    line_range: list[int],
    excerpt: str,
    reviewer_note: str = "",
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> Suppression:
    """Add (or refresh) a suppression entry. Returns the stored record."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=int(ttl_days))
    sup = Suppression(
        bot_id=bot_id,
        file=file,
        pattern_id=pattern_id,
        line_range=list(line_range),
        excerpt=excerpt,
        reviewed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        reviewer_note=reviewer_note,
    )
    existing = load_for_bot(shared_dir, bot_id)
    # Replace any equivalent suppression (same key) — re-marking refreshes TTL.
    key = sup.signature_key()
    kept = [s for s in existing if s.signature_key() != key]
    kept.append(sup)
    _write_for_bot(shared_dir, bot_id, kept)
    return sup


def remove(
    shared_dir: Path,
    *,
    bot_id: str,
    file: str,
    pattern_id: str,
    line_range: list[int],
) -> bool:
    """Drop a single suppression. Returns True if removed."""
    existing = load_for_bot(shared_dir, bot_id)
    if not existing:
        return False
    target_key = Suppression(
        bot_id=bot_id, file=file, pattern_id=pattern_id,
        line_range=list(line_range), excerpt="", reviewed_at="", expires_at="",
    ).signature_key()
    kept = [s for s in existing if s.signature_key() != target_key]
    if len(kept) == len(existing):
        return False
    _write_for_bot(shared_dir, bot_id, kept)
    return True


def prune_expired(shared_dir: Path) -> int:
    """Drop expired suppressions across every bot file. Returns count pruned."""
    d = suppressions_dir(shared_dir)
    if not d.exists():
        return 0
    now = datetime.now(timezone.utc)
    pruned = 0
    for f in sorted(d.glob("*.json")):
        existing = load_for_bot(shared_dir, f.stem)
        kept = [s for s in existing if not s.is_expired(now)]
        if len(kept) != len(existing):
            pruned += len(existing) - len(kept)
            _write_for_bot(shared_dir, f.stem, kept)
    return pruned


# ── Match filter ──────────────────────────────────────────────────────────────

def is_suppressed(
    suppressions: list[Suppression],
    *,
    bot_id: str,
    file: str,
    pattern_id: str,
    line: int,
    line_tolerance: int = 2,
) -> Suppression | None:
    """Return the matching suppression if the (bot, file, pattern, line) tuple
    is covered by an unexpired suppression.

    ``line_tolerance`` allows small line drift from minor unrelated edits
    above the match — without it, adding a header line above a benign-but-
    flagged comment would resurrect the signal.
    """
    now = datetime.now(timezone.utc)
    for s in suppressions:
        if s.bot_id != bot_id or s.file != file or s.pattern_id != pattern_id:
            continue
        if s.is_expired(now):
            continue
        if not s.line_range:
            # A suppression with no line range matches only file-level findings
            # (the structural-anomaly class, which reports line=0). Don't let a
            # hand-edit with an empty line_range silently suppress every line
            # for the pattern.
            if line == 0:
                return s
            continue
        lo = int(s.line_range[0])
        hi = int(s.line_range[-1])
        if (lo - line_tolerance) <= line <= (hi + line_tolerance):
            return s
    return None
