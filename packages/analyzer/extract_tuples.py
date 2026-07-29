#!/usr/bin/env python3
"""evolve/extract_tuples.py — Daily L3 tuple extractor.

Reads recent session_summary records for one or all bots, runs the
configured extractor (Haiku by default) on each non-trivial summary,
and persists ObservationTuples under
``{shared_dir}/observations/{bot}/{YYYY-MM-DD}.jsonl``.

Wired by deploy.py as ``ai.openclaw.evolve.tuples.{bot_id}``, runs daily
at 01:30 with jitter. Also used for backfill: pass ``--days 14`` to
extract from the last two weeks of summaries (cheap on re-runs because
``write_tuples`` dedupes by ``source_hash`` + tuple id).

Usage:
    python3 extract_tuples.py --bot-id team_bot_a
    python3 extract_tuples.py --network /Users/Shared/evolve/network.json
    python3 extract_tuples.py --bot-id team_bot_a --days 14            # backfill
    python3 extract_tuples.py --bot-id team_bot_a --dry-run            # plan only
    python3 extract_tuples.py --bot-id team_bot_a --max-sessions 25
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# evolve_admin layout: this script lives in packages/analyzer/, so its
# sibling modules (observations/, schema/, etc.) are importable directly.
try:
    from evolve_config import CANONICAL_SHARED_DIR, resolve_network_path
except ImportError:
    from platform_profile import get_profile as _gp
    CANONICAL_SHARED_DIR = Path(_gp().shared_dir_default)
    def resolve_network_path() -> Path:  # type: ignore[no-redef]
        return CANONICAL_SHARED_DIR / "network.json"

from observations.access import window
from observations.extract import (
    ExtractorVocabulary,
    extract_from_session_summary,
)
from observations.tuples import write_tuples
from schema.observation import ObservationTuple


logger = logging.getLogger("evolve.extract_tuples")


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────


# Per-run hard ceiling on LLM calls. With 7 bots × 5 sessions/day this
# is far more than typical, but we set it so a runaway day (e.g. someone
# scripts a flood) can't blow through the budget unnoticed.
DEFAULT_MAX_SESSIONS_PER_RUN: int = 50

# How far back to look by default. The daily run only needs ~1 day, but
# we widen a bit so a missed run catches up.
DEFAULT_DAYS_BACK: int = 2


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_bots_from_network(network_path: Path) -> tuple[list[str], str]:
    """Return (bot_ids, shared_dir). Treat the evolve bot like the others."""
    if not network_path.exists():
        raise FileNotFoundError(f"network file not found: {network_path}")
    network = json.loads(network_path.read_text())
    bots = list(network.get("members") or [])
    if "evolve" not in bots:
        bots = bots + ["evolve"]
    shared_dir = str(network.get("sharedDir") or "/Users/Shared/evolve")
    return bots, shared_dir


def _existing_source_hashes(
    shared_dir: Path, bot_id: str, start: datetime, end: datetime
) -> set[str]:
    """Collect source_hash values already on disk so we skip re-extraction."""
    seen: set[str] = set()
    base = shared_dir / "observations" / bot_id
    if not base.exists():
        return seen
    cur = start.astimezone(timezone.utc).date()
    last = end.astimezone(timezone.utc).date()
    while cur <= last:
        path = base / f"{cur.isoformat()}.jsonl"
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        sh = rec.get("source_hash")
                        if isinstance(sh, str):
                            seen.add(sh)
            except OSError:
                pass
        cur += timedelta(days=1)
    return seen


def _build_vocabulary_from_summaries(summaries: list[dict]) -> ExtractorVocabulary:
    """Use the union of applications_invoked across the window as active nouns.

    Avoids reading openclaw.json (which requires the bot's home and ACLs)
    by inferring the active app set from what session_summaries actually
    reference. Falls back to an empty list — the extractor's prompt
    handles that case (it will pick a generic domain word).
    """
    nouns: set[str] = set()
    for rec in summaries:
        for app in rec.get("applications_invoked") or []:
            if isinstance(app, str) and app.strip():
                # Normalize: lowercase, drop a trailing "-app" / "-tool"
                token = app.strip().lower()
                for suf in ("-app", "-tool", "_app", "_tool"):
                    if token.endswith(suf):
                        token = token[: -len(suf)]
                nouns.add(token)
    return ExtractorVocabulary.from_active_nouns(sorted(nouns))


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot extraction
# ─────────────────────────────────────────────────────────────────────────────


def extract_for_bot(
    bot_id: str,
    *,
    shared_dir: Path,
    days: int,
    max_sessions: int,
    dry_run: bool = False,
) -> dict:
    """Extract tuples for one bot over the last ``days`` days.

    Returns a stats dict suitable for logging / aggregation.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    w = window(bot_id, start=start, end=end, shared_dir=shared_dir)

    summaries = list(w.session_summaries())
    if not summaries:
        return {
            "bot_id": bot_id,
            "summaries": 0,
            "skipped_dedup": 0,
            "skipped_trivial": 0,
            "extracted": 0,
            "tuples_written": 0,
            "quota_hit": False,
        }

    seen = _existing_source_hashes(shared_dir, bot_id, start, end)
    vocabulary = _build_vocabulary_from_summaries(summaries)

    # Newest-first ordering: when quota_hit clips a backfill, the recent
    # sessions (the ones the generators actually need) get covered first.
    # On subsequent daily runs, source_hash dedup carries forward and
    # the older sessions catch up over a few days.
    summaries.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)

    skipped_trivial = 0
    skipped_dedup = 0
    extracted_calls = 0
    tuples_written: list[ObservationTuple] = []
    quota_hit = False

    # We need a stable hash to preempt the LLM call when the summary is
    # already-extracted. The source_hash is computed from the synthesized
    # transcript inside extract_tuples, so we reproduce the synthesis
    # here without calling the LLM.
    from observations.extract import session_summary_to_transcript

    for rec in summaries:
        transcript = session_summary_to_transcript(rec)
        if transcript is None:
            skipped_trivial += 1
            continue
        if transcript.text_hash in seen:
            skipped_dedup += 1
            continue
        if extracted_calls >= max_sessions:
            quota_hit = True
            break

        if dry_run:
            extracted_calls += 1
            continue

        try:
            new_tuples = extract_from_session_summary(rec, vocabulary=vocabulary)
        except Exception as exc:  # noqa: BLE001 — one bad session ≠ kill the run
            logger.warning(
                "[extract_tuples] %s session=%s extraction failed: %s",
                bot_id,
                rec.get("session_id"),
                exc,
            )
            new_tuples = []
        extracted_calls += 1
        tuples_written.extend(new_tuples)

    if not dry_run and tuples_written:
        # Group by date so each tuple lands in its summary's day-file.
        from collections import defaultdict

        grouped: dict[str, list[ObservationTuple]] = defaultdict(list)
        for t in tuples_written:
            day_iso = (t.timestamp_start or "")[:10]
            if not day_iso:
                day_iso = end.astimezone(timezone.utc).date().isoformat()
            grouped[day_iso].append(t)
        for day_iso, batch in grouped.items():
            try:
                day_dt = datetime.fromisoformat(day_iso).replace(tzinfo=timezone.utc)
            except ValueError:
                day_dt = end.astimezone(timezone.utc)
            write_tuples(batch, shared_dir=shared_dir, bot_id=bot_id, day=day_dt)

    return {
        "bot_id": bot_id,
        "summaries": len(summaries),
        "skipped_dedup": skipped_dedup,
        "skipped_trivial": skipped_trivial,
        "extracted": extracted_calls,
        "tuples_written": len(tuples_written),
        "quota_hit": quota_hit,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract ObservationTuples from session_summary records"
    )
    parser.add_argument(
        "--bot-id", help="Bot ID to extract for (default: all bots in network)"
    )
    parser.add_argument(
        "--shared-dir",
        default=str(CANONICAL_SHARED_DIR),
        help="Path to shared evolve directory (default: platform-keyed canonical shared dir)",
    )
    parser.add_argument(
        "--network",
        default=str(resolve_network_path()),
        help="Path to network.json (used when --bot-id is omitted)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS_BACK,
        help=f"Days of history to scan (default: {DEFAULT_DAYS_BACK}); "
        "use 14 for backfill",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=DEFAULT_MAX_SESSIONS_PER_RUN,
        help=(
            "Max sessions to extract per bot per run (cost ceiling). "
            f"Default {DEFAULT_MAX_SESSIONS_PER_RUN}."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — don't call the LLM or write tuples",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    shared_dir = Path(args.shared_dir)

    # Wire the Haiku extractor unless we're in dry-run mode (in dry-run
    # mode the default no-op extractor is fine; we only count sessions).
    if not args.dry_run:
        try:
            from observations.llm_extractor import wire_default_extractor

            wire_default_extractor()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[extract_tuples] LLM wiring failed: %s", exc)

    if args.bot_id:
        bots = [args.bot_id]
        if args.shared_dir == "/Users/Shared/evolve":
            try:
                _, sd = _load_bots_from_network(Path(args.network))
                shared_dir = Path(sd)
            except FileNotFoundError:
                pass
    else:
        try:
            bots, sd = _load_bots_from_network(Path(args.network))
            shared_dir = Path(sd)
        except FileNotFoundError as e:
            print(f"[evolve/extract_tuples] {e}", file=sys.stderr)
            return 1
        if not bots:
            print(
                "[evolve/extract_tuples] No bots in network.members",
                file=sys.stderr,
            )
            return 1

    total_extracted = 0
    total_written = 0
    for bot_id in bots:
        try:
            stats = extract_for_bot(
                bot_id,
                shared_dir=shared_dir,
                days=args.days,
                max_sessions=args.max_sessions,
                dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — one bot's failure mustn't kill others
            logger.exception(
                "[extract_tuples] %s extraction crashed: %s", bot_id, exc
            )
            continue

        logger.info(
            "[extract_tuples] %s summaries=%d trivial=%d dedup=%d "
            "extracted=%d tuples=%d quota_hit=%s",
            stats["bot_id"],
            stats["summaries"],
            stats["skipped_trivial"],
            stats["skipped_dedup"],
            stats["extracted"],
            stats["tuples_written"],
            stats["quota_hit"],
        )
        total_extracted += stats["extracted"]
        total_written += stats["tuples_written"]

    logger.info(
        "[extract_tuples] done — extracted=%d tuples_written=%d bots=%d "
        "shared_dir=%s days=%d dry_run=%s",
        total_extracted,
        total_written,
        len(bots),
        shared_dir,
        args.days,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
