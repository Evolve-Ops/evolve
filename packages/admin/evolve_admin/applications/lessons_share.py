"""
lessons_share.py — Share-time redaction for Lessons (S4e).

Per internal/spec-manifest-v7-2026-05-20.md §6 + §9. The Lessons file at
{shared_dir}/lessons/<bot_id>/<spec_id>.json is the *local* distillation
of an Instance's change_log + signal-store evidence. It contains
operator-readable but potentially user-identifying material: paraphrased
user complaints, ISO timestamps that can fingerprint when a user was
active, signal-store refs that key into the bot's private store.

The share gateway requires `redaction_applied: true` before publishing
(schema §6 hard rule). This module applies the redaction transformations
and produces a copy fit for cross-pod sharing or any audience outside
the originating bot's operator.

What v1 redacts (deterministic, no LLM):

  - User-quote evidence summaries → paraphrased to "User feedback
    (paraphrased): N occurrence(s)". The semantic content (kind, count)
    is preserved; the literal user words are not.
    → redaction_kind: user_quotes_paraphrased

  - observation_window.start / .end → quantized to ISO week (Monday
    00:00 UTC of the same week). Granularity sufficient to reason about
    when Lessons were compiled, fine enough to defeat narrow
    fingerprinting.
    → redaction_kind: timestamps_quantized_to_week

  - source_timestamp on each Lesson → quantized to ISO week.
    Same rationale as above for per-Lesson provenance pins.
    → redaction_kind: timestamps_quantized_to_week  (same flag; one entry)

What v1 does *not* redact (deferred):

  - The lesson-level `summary` field — that's the operator-authored
    distillation; redacting it would defeat the purpose of sharing.
    Future versions may add an LLM-paraphrase pass for lesson.summary
    if cross-pod review surfaces information leaks.
  - Signal-store refs — those are opaque per-pod tokens; sharing them
    leaks nothing without the originating pod's signal store.
  - source_change_log_entry_id — opaque pod-local IDs.

Idempotent: re-running on an already-redacted Lessons file is a no-op
(already-paraphrased summaries are detected, already-quantized
timestamps round to themselves). redaction_kind merges with any
existing list.

Usage:
    # Redact one bot's Lessons for a specific Spec:
    python3 -m evolve_admin.applications.lessons_share \\
        --bot-id team_bot_a --spec-id p-aaaa1111

    # Custom output path (default: {shared_dir}/lessons-shared/<bot>/<spec_id>.json):
    python3 -m evolve_admin.applications.lessons_share \\
        --bot-id team_bot_a --spec-id p-aaaa1111 \\
        --output /tmp/shared-lessons.json

    # Dry-run (writes nothing; prints redaction_kind summary):
    python3 -m evolve_admin.applications.lessons_share \\
        --bot-id team_bot_a --spec-id p-aaaa1111 --dry-run
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json as _atomic_write_json


# ── Redaction primitive ──────────────────────────────────────────────────────

# Marker prefix on paraphrased evidence summaries. Used by both the
# paraphrase step (to construct the replacement) and the idempotency
# check (to detect "already paraphrased").
_PARAPHRASE_PREFIX = "User feedback (paraphrased)"

# Evidence types that carry user-identifying quotes. Only these have
# their `summary` field paraphrased.
_USER_QUOTE_TYPES = frozenset({"user_request", "user_complaint"})


def _quantize_to_iso_week(ts: str) -> Optional[str]:
    """Round an ISO-8601 timestamp to Monday 00:00 UTC of the same week.

    Returns the rounded value as a Z-suffixed ISO string, or None if the
    input doesn't parse. Idempotent: already-quantized timestamps round
    to themselves (Monday rounds to Monday).
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # fromisoformat handles "+00:00" but not the literal "Z" suffix on
        # older Python versions, so swap before parsing.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Normalize to UTC so the Monday-rounding is consistent across timezones.
    dt = dt.astimezone(timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%Y-%m-%dT%H:%M:%SZ")


def _looks_paraphrased(summary: str) -> bool:
    """Heuristic to skip re-paraphrasing on idempotent re-runs."""
    return isinstance(summary, str) and summary.startswith(_PARAPHRASE_PREFIX)


def _paraphrase_user_quote(count: int) -> str:
    """Construct the canonical replacement summary for user-quote evidence."""
    n = max(1, int(count or 1))
    plural = "s" if n != 1 else ""
    return f"{_PARAPHRASE_PREFIX}: {n} occurrence{plural}"


def redact_lessons(lessons: dict) -> dict:
    """Return a deep copy of ``lessons`` with share-time redaction applied.

    Mutations:
      - User-quote evidence summaries (type ∈ user_request, user_complaint)
        are replaced with a paraphrased form preserving only the count.
      - observation_window.start + .end are quantized to ISO week.
      - Each lesson's source_timestamp (if present) is quantized to ISO week.
      - redaction_applied is set to True.
      - redaction_kind is populated/extended with the list of categories
        actually applied (sorted by first-use order in this function).

    Idempotent: a second call on the output is a no-op modulo the result
    set being the same. Already-paraphrased summaries are detected via
    prefix; already-quantized timestamps round to themselves.

    Pure: does not touch disk. Caller is responsible for writing the
    result. The input dict is not mutated (deep-copied first).
    """
    out = copy.deepcopy(lessons)
    kinds_added: list[str] = []

    def _flag(kind: str) -> None:
        if kind not in kinds_added:
            kinds_added.append(kind)

    # ── Quantize observation_window timestamps ──
    ow = out.get("observation_window")
    if isinstance(ow, dict):
        for key in ("start", "end"):
            ts = ow.get(key)
            if not isinstance(ts, str):
                continue
            quantized = _quantize_to_iso_week(ts)
            if quantized is None:
                continue
            if quantized != ts:
                ow[key] = quantized
                _flag("timestamps_quantized_to_week")

    # ── Paraphrase user-quote evidence + quantize per-Lesson timestamps ──
    for lesson in out.get("lessons") or []:
        if not isinstance(lesson, dict):
            continue

        # Per-Lesson source_timestamp (added by S4d compression for audit)
        st = lesson.get("source_timestamp")
        if isinstance(st, str):
            quantized = _quantize_to_iso_week(st)
            if quantized is not None and quantized != st:
                lesson["source_timestamp"] = quantized
                _flag("timestamps_quantized_to_week")

        for ev in lesson.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") not in _USER_QUOTE_TYPES:
                continue
            summary = ev.get("summary")
            if summary is None:
                # No quote to paraphrase; still flag so the redaction record
                # accurately reflects "we considered user-quote evidence."
                # Actually: don't flag if no summary changed — would confuse
                # downstream consumers about whether a quote ever existed.
                continue
            if _looks_paraphrased(summary):
                continue
            ev["summary"] = _paraphrase_user_quote(ev.get("count") or 1)
            _flag("user_quotes_paraphrased")

    # ── Stamp redaction_applied + merge redaction_kind ──
    out["redaction_applied"] = True
    existing = out.get("redaction_kind") or []
    if not isinstance(existing, list):
        existing = []
    for k in kinds_added:
        if k not in existing:
            existing.append(k)
    out["redaction_kind"] = existing

    return out


# ── Disk-side helpers ────────────────────────────────────────────────────────

@dataclass
class ShareResult:
    """Outcome of a share_lessons call."""
    source_path: Path
    output_path: Optional[Path]
    redaction_kind: list[str] = field(default_factory=list)
    lessons_count: int = 0
    dry_run: bool = False

    def summary(self) -> str:
        kinds = ", ".join(self.redaction_kind) or "none"
        verb = "would write" if self.dry_run else "wrote"
        dest = str(self.output_path) if self.output_path else "(dry-run; no output)"
        return (
            f"Redacted {self.lessons_count} lesson(s) from {self.source_path}; "
            f"kinds=[{kinds}]; {verb} → {dest}"
        )


# identity: see resolve_app_id — every ``spec_id`` in this module is a SPEC
# path key, never a manifest identity read: it names the Lessons file
# (``lessons/<bot_id>/<spec_id>.json``, and the shared copy under
# ``lessons-shared/``) and, paired with ``spec_version``, the gallery tier path
# ``gallery/<tier>/<spec_id>/<spec_version>.json`` that ``load_spec_version``
# resolves. AL-1.4 §5 keeps both. The value arrives as a CLI argument
# (``--spec-id``); this module never reads it off a manifest.
def _default_output_path(shared_dir: Path, bot_id: str, spec_id: str) -> Path:
    """Where to put a freshly redacted Lessons copy.

    Separate dir from the canonical {shared_dir}/lessons/<bot>/<spec>.json
    so the share artifact is distinct from the bot's working copy — avoids
    the share-gateway accidentally reading the unredacted source on the
    next cycle.
    """
    return shared_dir / "lessons-shared" / bot_id / f"{spec_id}.json"


class SpecPrivacyError(PermissionError):
    """Raised when the bound Spec's privacy block forbids Lessons sharing.

    Distinguished from a generic PermissionError so the REST endpoint can
    return 403 with a specific explanation instead of swallowing it as a
    filesystem permission issue.
    """


def _spec_allows_lessons_share(shared_dir: Path, spec_id: str,
                                spec_version: Optional[str]) -> tuple[bool, str]:
    """Check the bound Spec's ``privacy.shareable_in_lessons`` flag.

    Returns (allowed, reason). When ``allowed`` is False, ``reason`` is a
    short string the UI surfaces to the operator. Defaults to False —
    matches the schema's default and the spec §6 rule that share-time
    redaction is required before publishing. An operator must explicitly
    opt this app's Lessons into share-eligibility on the Spec itself
    before sharing works.

    Looked up across gallery tiers (local → builtin → imported) at the
    Lessons-observed spec_version when available, else any version. Falls
    back to deny-by-default if no Spec is findable — defensive: never
    share Lessons whose governing Spec we can't read.
    """
    # Local import to avoid a circular: adopt.load_spec_version pulls
    # from this module's neighbor. Adopt-only dependency.
    from .adopt import load_spec_version
    from .spec_drift import _latest_spec_version

    spec = None
    if spec_version:
        spec = load_spec_version(shared_dir, spec_id, spec_version)
    if spec is None:
        # Fall back to whatever version the gallery has — operator may have
        # rebuilt past the Lessons observed window.
        fallback_version = _latest_spec_version(spec_id, shared_dir)
        if fallback_version:
            spec = load_spec_version(shared_dir, spec_id, fallback_version)

    if spec is None:
        return False, (
            f"no Spec found in gallery for {spec_id!r} — Lessons share "
            "denied by default since the governing privacy block can't be read"
        )

    privacy = spec.get("privacy") or {}
    if not isinstance(privacy, dict):
        return False, "Spec.privacy is malformed"

    allowed = bool(privacy.get("shareable_in_lessons", False))
    if not allowed:
        return False, (
            f"Spec {spec_id!r} has privacy.shareable_in_lessons=false "
            "(or unset; default is false). The operator must opt this app "
            "into Lessons sharing on the Spec before share works."
        )
    return True, "ok"


def share_lessons(
    bot_id: str,
    spec_id: str,
    shared_dir: Path,
    *,
    output_path: Optional[Path] = None,
    dry_run: bool = False,
) -> ShareResult:
    """Load the local Lessons file, redact it, and write the result.

    The canonical source is {shared_dir}/lessons/<bot_id>/<spec_id>.json
    (where migration + lessons_compress both write).

    Output defaults to {shared_dir}/lessons-shared/<bot_id>/<spec_id>.json.

    Privacy gate: reads ``Spec.privacy.shareable_in_lessons`` (schema §6)
    and raises ``SpecPrivacyError`` when the flag is unset/false — the
    operator must explicitly opt the app into Lessons sharing on the
    Spec first. Default-deny matches the schema's default.

    Raises:
      FileNotFoundError — source Lessons file doesn't exist
      ValueError        — source file isn't valid JSON
      SpecPrivacyError  — bound Spec doesn't permit Lessons share
    """
    source = shared_dir / "lessons" / bot_id / f"{spec_id}.json"
    if not source.is_file():
        raise FileNotFoundError(f"no Lessons at {source}")
    try:
        lessons = json.loads(source.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Lessons at {source} not valid JSON: {e}")

    # ── Privacy gate ──────────────────────────────────────────────────────
    # Look up the bound Spec via spec_version_observed (set by compression
    # and migration). Refuse share when shareable_in_lessons is not true.
    spec_version = lessons.get("spec_version_observed")
    allowed, reason = _spec_allows_lessons_share(shared_dir, spec_id, spec_version)
    if not allowed:
        raise SpecPrivacyError(reason)

    redacted = redact_lessons(lessons)
    out_path = output_path or _default_output_path(shared_dir, bot_id, spec_id)

    result = ShareResult(
        source_path=source,
        output_path=out_path if not dry_run else None,
        redaction_kind=list(redacted.get("redaction_kind") or []),
        lessons_count=len(redacted.get("lessons") or []),
        dry_run=dry_run,
    )
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(out_path, redacted)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Share-time redaction for Lessons (v7-arc S4e)"
    )
    p.add_argument("--bot-id", required=True, help="Bot whose Lessons to redact.")
    p.add_argument(
        "--spec-id",
        required=True,
        help="Spec the Lessons pertain to (Lessons file is <spec_id>.json).",
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--output",
        help="Output path override. Default: "
             "{shared_dir}/lessons-shared/<bot>/<spec>.json",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write the output; just print what redaction would do.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    output_path = Path(args.output) if args.output else None

    try:
        result = share_lessons(
            args.bot_id,
            args.spec_id,  # identity: see resolve_app_id — CLI --spec-id, as above
            shared_dir,
            output_path=output_path,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ERROR: write failed: {e}", file=sys.stderr)
        return 1

    print(result.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
