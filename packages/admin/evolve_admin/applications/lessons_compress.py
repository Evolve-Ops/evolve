"""
lessons_compress.py — Roll Instance change_log entries into Lesson records.

Per internal/spec-manifest-v7-2026-05-20.md §6 + §8.1.4. The change_log is a
private append-only audit trail; Lessons are the evidence-cited, shareable
distillation. Compression bridges them: for each change_log entry that
cites concrete evidence (a signal store reference, error count, user
complaint), emit a Lesson record.

Hard rules from §6 enforced here:

  - Every Lesson must cite evidence. Change_log entries without
    `reason_signals[]` are skipped.
  - Per-kind evidence minimums:
      new_capability     → at least one user_request OR user_complaint
      blueprint_correction → at least one quantitative metric
      worked_well        → at least one quantitative metric (deferred — change_log
                            doesn't currently have a "worked_well" kind tag)
      failed             → at least one quantitative metric (same)
    LLM narration ("we learned X") with no signal-store backing is rejected.

  - `shareable_in_lessons: false` in the Spec's privacy block: compression
    still produces a local Lessons record (audit value), but the share
    gateway must check the flag before publishing. That's S4 export work.

  - `proposed_spec_change` is advisory and free-text (per S2.12). Emitted
    for capability_added + blueprint_correction entries that suggest
    Spec-level changes.

Compression strategy is deterministic (no LLM in v1). The change_log entry's
description becomes the Lesson summary; reason_signals map directly to
Lesson evidence. Future iterations can add LLM-driven cross-entry synthesis
("after 3 months of these capability_added entries, the app's objective has
drifted toward X — propose Spec rename").

Idempotency: tracked via Lessons.observation_window.end. Each run picks up
change_log entries with timestamp > window.end, then advances the window.
Re-running on a quiet Instance is a no-op.

Usage:
    # Compress one bot's Lessons:
    python3 -m evolve_admin.applications.lessons_compress --bot-id team_bot_a

    # All bots from network.json:
    python3 -m evolve_admin.applications.lessons_compress
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from evolve_util import atomic_write_json as _atomic_write_json
from evolve_util import now_iso as _now_iso

from ..config import bot_home, load_network


# ── Constants ────────────────────────────────────────────────────────────────

# change_log.kind → Lesson.kind mapping. The change_log vocabulary (per §4.1)
# is operational; Lesson vocabulary (per §6) is evaluative. Maps where the
# semantics align; entries with unmappable kinds (data_refresh, scanner_inferred,
# forge_rebuild, user_redirect) don't produce Lessons.
_CHANGELOG_KIND_TO_LESSON_KIND = {
    "capability_added":      "new_capability",
    "blueprint_correction":  "blueprint_correction",
    "config_tuning":         "blueprint_correction",  # tuning is a small-scale correction
}

# Lesson-kind → set of evidence types that satisfy the per-kind minimum.
# Per §6 hard rule #1.
_REQUIRED_EVIDENCE_BY_KIND = {
    "new_capability":       {"user_request", "user_complaint"},
    "blueprint_correction": {"error_count", "completion_rate", "failure_rate",
                             "success_rate", "metric"},
    "worked_well":          {"error_count", "completion_rate", "failure_rate",
                             "success_rate", "metric"},
    "failed":               {"error_count", "completion_rate", "failure_rate",
                             "success_rate", "metric"},
}

# change_log.reason_signals[].signal_type → Lesson evidence type. Mostly
# identity but some change_log uses friendlier names.
_SIGNAL_TYPE_TO_EVIDENCE_TYPE = {
    "user_request":     "user_request",
    "user_complaint":   "user_complaint",
    "error_count":      "error_count",
    "completion_rate":  "completion_rate",
    "failure_rate":     "failure_rate",
    "success_rate":     "success_rate",
    "metric":           "metric",
    # change_log might use a generic signal_store reference; route it through
    "signal_store":     "signal_store_ref",
    "signal_store_ref": "signal_store_ref",
}


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class LessonsCompressionResult:
    """Outcome of compressing one Instance's change_log."""
    spec_id: str
    bot_id: str
    lessons_path: Path
    new_lessons_count: int = 0
    skipped_no_evidence: int = 0
    skipped_per_kind_minimum: int = 0
    skipped_unmappable_kind: int = 0
    new_window_end: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"[{self.bot_id}/{self.spec_id}] +{self.new_lessons_count} lessons "  # identity: see resolve_app_id — spec binding, as below
            f"(skipped: no-evidence={self.skipped_no_evidence}, "
            f"min-not-met={self.skipped_per_kind_minimum}, "
            f"unmappable-kind={self.skipped_unmappable_kind})"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_lesson_id() -> str:
    return f"les-{secrets.token_hex(4)}"


def _evidence_from_reason_signals(reason_signals: list) -> list[dict]:
    """Map change_log entry's reason_signals → Lesson evidence records."""
    out = []
    for rs in reason_signals or []:
        if not isinstance(rs, dict):
            continue
        signal_type = rs.get("signal_type") or ""
        evidence_type = _SIGNAL_TYPE_TO_EVIDENCE_TYPE.get(signal_type)
        if not evidence_type:
            continue
        ev: dict = {
            "type": evidence_type,
            "ref": rs.get("ref") or "",
        }
        # Optional fields preserve quantitative claims
        if "value" in rs:
            ev["value"] = rs["value"]
        if "count" in rs:
            ev["count"] = rs["count"]
        if "summary" in rs:
            ev["summary"] = rs["summary"]
        out.append(ev)
    return out


def _meets_per_kind_minimum(lesson_kind: str, evidence: list[dict]) -> bool:
    """Per §6 hard rule #1: per-kind evidence minimums."""
    required = _REQUIRED_EVIDENCE_BY_KIND.get(lesson_kind)
    if not required:
        return True  # no specific requirement → accept
    return any(ev.get("type") in required for ev in evidence)


def _build_lesson_from_changelog_entry(
    entry: dict,
    evidence: list[dict],
    lesson_kind: str,
) -> dict:
    """Build a Lesson record from a change_log entry + extracted evidence."""
    lesson = {
        "lesson_id": _new_lesson_id(),
        "kind": lesson_kind,
        "summary": entry.get("description", "").strip()
                   or f"[{lesson_kind}] {entry.get('entry_id', '?')}",
        "evidence": evidence,
        # Trace back to the source change_log entry for audit
        "source_change_log_entry_id": entry.get("entry_id"),
        "source_timestamp": entry.get("timestamp"),
    }
    # If the change_log entry has a user_intent_quote, fold into summary
    if uiq := entry.get("user_intent_quote"):
        # Truncate long quotes; redaction happens later at share-time
        lesson["summary"] = lesson["summary"] + f' (user: "{uiq[:120]}")'

    # Spec-change proposal heuristic: capability_added → propose new capability;
    # blueprint_correction → propose the fix to the bound file
    cl_kind = entry.get("kind")
    if cl_kind == "capability_added":
        lesson["proposed_spec_change"] = {
            "target": "the app's blueprint.files[] and capability_summary",
            "kind": "capability_addition",
            "description": entry.get("description", "")
                           or "Add the new capability to the Spec's blueprint.",
        }
    elif cl_kind == "blueprint_correction":
        lesson["proposed_spec_change"] = {
            "target": (entry.get("file_changes") or [{}])[0].get("path", "the affected file"),
            "kind": "fix",
            "description": entry.get("description", "")
                           or "Apply the correction at the Spec level.",
        }
    return lesson


# ── Core compression ─────────────────────────────────────────────────────────

def compress_changelog(
    instance: dict,
    spec: dict,
    lessons: dict,
) -> tuple[dict, LessonsCompressionResult]:
    """
    Pure function: take an Instance + Spec + existing Lessons file → produce
    the updated Lessons file dict + a compression result.

    No file I/O — caller passes dicts and writes back.

    Idempotency: only processes change_log entries with timestamp >
    lessons.observation_window.end.
    """
    # identity: see resolve_app_id — the Instance->Spec BINDING, NESTED under
    # ``provenance`` where the top-level-only resolver cannot see it. A v7-arc
    # Instance's own resolved id is its ``instance_id`` (AL-1.4 §6 delta 6);
    # Lessons are per-SPEC (portable across bots and pods), so keying them on
    # the instance would make them unshareable.
    spec_id = instance.get("provenance", {}).get("spec_id", "")
    bot_id = instance.get("bot_id", "")
    result = LessonsCompressionResult(
        spec_id=spec_id, bot_id=bot_id,
        lessons_path=Path("<not-on-disk-yet>"),
    )

    # Determine cutoff: timestamps strictly greater than this are new
    window = lessons.get("observation_window") or {}
    cutoff = (window.get("end") or "")

    change_log = instance.get("change_log") or []
    new_lessons: list[dict] = []
    latest_ts = cutoff

    for entry in change_log:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("timestamp", "")
        if cutoff and ts <= cutoff:
            continue  # already compressed
        if ts > latest_ts:
            latest_ts = ts

        cl_kind = entry.get("kind", "")
        lesson_kind = _CHANGELOG_KIND_TO_LESSON_KIND.get(cl_kind)
        if not lesson_kind:
            result.skipped_unmappable_kind += 1
            continue

        evidence = _evidence_from_reason_signals(entry.get("reason_signals") or [])
        if not evidence:
            result.skipped_no_evidence += 1
            continue

        if not _meets_per_kind_minimum(lesson_kind, evidence):
            result.skipped_per_kind_minimum += 1
            continue

        new_lessons.append(_build_lesson_from_changelog_entry(
            entry, evidence, lesson_kind,
        ))

    # Update the Lessons file
    updated = dict(lessons)  # shallow copy; we replace top-level fields
    updated.setdefault("lessons", [])
    updated["lessons"] = list(updated["lessons"]) + new_lessons

    # Bump observation_window
    window_out = dict(window)
    if not window_out.get("start"):
        window_out["start"] = (change_log[0].get("timestamp") if change_log else _now_iso())
    if latest_ts:
        window_out["end"] = latest_ts
    else:
        window_out["end"] = window_out.get("end") or _now_iso()
    window_out["instance_runs"] = int(window_out.get("instance_runs", 0)) + 1
    updated["observation_window"] = window_out

    # Refresh spec_version_observed in case Adopt has bumped it
    spec_version = instance.get("provenance", {}).get("spec_version")
    if spec_version:
        updated["spec_version_observed"] = spec_version

    result.new_lessons_count = len(new_lessons)
    result.new_window_end = window_out.get("end")
    return updated, result


# ── File I/O orchestration ───────────────────────────────────────────────────

def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_spec(shared_dir: Path, spec_id: str, spec_version: str) -> Optional[dict]:
    """Find the Spec across all gallery tiers."""
    gallery = shared_dir / "gallery"
    candidates = [
        gallery / "local" / spec_id / f"{spec_version}.json",
        gallery / "builtin" / spec_id / f"{spec_version}.json",
    ]
    # imported tier is nested under source_pod_id; try a glob
    imported_root = gallery / "imported"
    if imported_root.is_dir():
        candidates.extend(imported_root.glob(f"*/{spec_id}/{spec_version}.json"))
    for p in candidates:
        if p.is_file():
            data = _load_json(p)
            if data:
                return data
    return None


def compress_instance(
    instance_path: Path,
    shared_dir: Path,
    dry_run: bool = False,
) -> Optional[LessonsCompressionResult]:
    """
    Compress one Instance's change_log into its Lessons file.

    Looks up Lessons at {shared_dir}/lessons/<bot_id>/<spec_id>.json.
    Returns None if the Instance isn't v7-arc shaped (skip silently).
    """
    instance = _load_json(instance_path)
    if not instance or instance.get("manifest_shape") != "v7-arc":
        return None

    # identity: see resolve_app_id — the Instance->Spec binding, as above. Here
    # it is also half of two path keys that must stay paired with
    # ``spec_version``: the Lessons file ``lessons/<bot_id>/<spec_id>.json`` and
    # the gallery tier path ``gallery/<tier>/<spec_id>/<spec_version>.json``
    # resolved by ``_resolve_spec``. AL-1.4 §5 forbids collapsing either.
    spec_id = instance.get("provenance", {}).get("spec_id", "")
    bot_id = instance.get("bot_id", "")
    spec_version = instance.get("provenance", {}).get("spec_version", "")
    if not spec_id or not bot_id:
        return None

    spec = _resolve_spec(shared_dir, spec_id, spec_version) or {}
    lessons_path = shared_dir / "lessons" / bot_id / f"{spec_id}.json"
    lessons = _load_json(lessons_path) or {
        "lessons_id": f"l-{secrets.token_hex(4)}",
        "spec_id": spec_id,
        "spec_version_observed": spec_version,
        "source_pod_id": "",
        "source_bot_id": bot_id,
        "observation_window": {
            "start": _now_iso(),
            "end": "",
            "instance_runs": 0,
        },
        "lessons": [],
        "redaction_applied": False,
    }

    updated, result = compress_changelog(instance, spec, lessons)
    result.lessons_path = lessons_path

    if not dry_run and result.new_lessons_count > 0:
        try:
            lessons_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(lessons_path, updated)
        except OSError as e:
            result.errors.append(f"failed to write lessons file: {e}")

    return result


def compress_bot(
    bot_id: str,
    shared_dir: Path,
    dry_run: bool = False,
) -> list[LessonsCompressionResult]:
    """Compress every v7-arc Instance for the given bot."""
    mdir = bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    results: list[LessonsCompressionResult] = []
    if not mdir.is_dir():
        return results
    for f in mdir.glob("*.json"):
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        res = compress_instance(f, shared_dir, dry_run=dry_run)
        if res is not None:
            results.append(res)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Compress Instance change_log entries into Lesson records "
                    "(v7-arc §6 + §8.1.4)"
    )
    p.add_argument(
        "--bot-id",
        action="append",
        default=[],
        help="Bot to compress (repeatable). Default: all bots in network.json.",
    )
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Pod shared dir (default: /Users/Shared/evolve)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute what would change without writing Lessons files.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bot_id
    if not bot_ids:
        try:
            net = load_network(shared_dir / "network.json")
            bots_field = net.get("bots") or {}
            if isinstance(bots_field, dict):
                bot_ids = list(bots_field.keys())
            elif isinstance(bots_field, list):
                bot_ids = [b["id"] for b in bots_field
                           if isinstance(b, dict) and "id" in b]
        except Exception as e:
            print(f"ERROR: couldn't load network.json: {e}", file=sys.stderr)
            return 1
    if not bot_ids:
        print("ERROR: no bots to compress", file=sys.stderr)
        return 1

    if args.dry_run:
        print("DRY RUN — no Lessons files will be written.\n")

    total_new = 0
    total_skipped = 0
    for bot_id in bot_ids:
        try:
            results = compress_bot(bot_id, shared_dir, dry_run=args.dry_run)
        except Exception as e:
            print(f"[{bot_id}] ERROR: {e}", file=sys.stderr)
            continue
        for r in results:
            print(r.summary())
            for err in r.errors:
                print(f"  ERROR: {err}", file=sys.stderr)
            total_new += r.new_lessons_count
            total_skipped += (
                r.skipped_no_evidence
                + r.skipped_per_kind_minimum
                + r.skipped_unmappable_kind
            )

    print(f"\nTotal: +{total_new} lessons across all bots; {total_skipped} entries skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
