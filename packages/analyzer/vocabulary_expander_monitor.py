"""vocabulary_expander_monitor — Layer 2 plumbing for runtime vocab
expansion. No LLM in this module (β scope); the LLM hookup ships
in PR γ.

What this monitor does today:

  1. Reads pod config from ``network.json``. If
     ``rsi.vocabulary_expansion.enabled`` is missing or false (the
     default), the monitor exits silently. Zero cost when off.

  2. For each member bot, walks the last N days of ObservationTuples
     and identifies *unrecognized nouns* — tokens that don't resolve
     to any domain via the merged (static ∪ dynamic) vocabulary.

  3. If the bot's unrecognized-noun count clears
     ``MIN_UNRECOGNIZED_NOUNS_FOR_EXPANSION`` (default 20), the
     monitor writes a *preflight report* to
     ``{shared_dir}/vocabulary/preflight/<bot>.json`` describing
     what an LLM expansion call would look like. Operators can
     inspect these reports to see what the LLM would be asked
     before any LLM cost is incurred.

  4. **The LLM call is not made in this PR.** PR γ wires a
     subprocess that reads preflight reports + makes per-bot LLM
     calls using each bot's own credentials, then writes proposals
     to ``{shared_dir}/vocabulary/pending.json`` for operator review.

What this monitor enables:

  - Operators can run ``python3 -m vocabulary_expander_monitor
    --shared-dir /Users/Shared/evolve`` to see what the LLM-expansion
    flow *would* propose, without paying for it.
  - Manual vocab additions via ``tools.vocab_add`` work today and
    flow through the same dynamic.json the LLM flow will write to.
  - The pattern monitors already consume ``effective_keywords()``
    via ``_merged_vocabulary``, so any vocab addition (manual or
    LLM-driven once γ ships) is honored on the next monitor cycle.

Failure mode:

  - Pod config unreadable → exit silently with diagnostic.
  - Observation tuples missing → no preflight written; monitor
    continues with the next bot.
  - All bots below threshold → exit with summary, no preflight
    files written.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import _merged_vocabulary as mv
from observations.access import window as obs_window


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PRODUCER = "vocabulary_expander_monitor"

DEFAULT_WINDOW_DAYS = 30

# A pattern run is only worth a preflight write when there's enough
# unrecognized signal to justify an LLM call. Below this floor, the
# LLM would be classifying noise; above it, the operator can decide
# whether the count is real evidence of vocab drift.
MIN_UNRECOGNIZED_NOUNS_FOR_EXPANSION = 20

# Cap how many unrecognized nouns flow into a preflight report.
# Without a cap a bot with N hundred unique nouns would balloon the
# preflight file and (once γ ships) the LLM input. Pick the 100 most
# frequent — the long tail is rarely worth classifying.
PREFLIGHT_NOUN_CAP = 100

# Stop-words that aren't worth proposing as domain keywords even when
# they appear as ObservationTuple nouns. Mostly observation-extraction
# artifacts that snuck in despite the upstream LLM filter.
_BORING_NOUNS = frozenset({
    "thing", "stuff", "something", "anything", "nothing",
    "everything", "someone", "anyone", "everyone", "nobody",
    "today", "tomorrow", "yesterday", "now", "then",
    "the", "a", "an", "this", "that", "these", "those",
    "i", "you", "we", "they", "he", "she", "it",
    "?",
})


# ─────────────────────────────────────────────────────────────────────────────
# Config gate
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _Config:
    enabled: bool = False
    threshold_nouns: int = MIN_UNRECOGNIZED_NOUNS_FOR_EXPANSION
    window_days: int = DEFAULT_WINDOW_DAYS


def _load_pod_config(network_json_path: Path) -> _Config:
    """Read ``rsi.vocabulary_expansion`` from network.json.

    Missing file or missing keys → defaults (enabled=False), so the
    monitor exits silently on any pod that hasn't explicitly opted
    in. That's the "no cost when off" contract."""
    cfg = _Config()
    try:
        data = json.loads(network_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    rsi_block = data.get("rsi")
    if not isinstance(rsi_block, dict):
        return cfg
    expansion = rsi_block.get("vocabulary_expansion")
    if not isinstance(expansion, dict):
        return cfg
    cfg.enabled = bool(expansion.get("enabled"))
    threshold = expansion.get("threshold_nouns")
    if isinstance(threshold, (int, float)) and threshold > 0:
        cfg.threshold_nouns = int(threshold)
    window = expansion.get("window_days")
    if isinstance(window, (int, float)) and window > 0:
        cfg.window_days = int(window)
    return cfg


def _network_bot_ids(network_json_path: Path) -> list[str]:
    """Read the configured bot list from network.json. Mirrors the
    pattern used in cost_watchdog + capability_gap_monitor."""
    try:
        data = json.loads(network_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    bots = data.get("bots")
    if not isinstance(bots, dict):
        return []
    return sorted(bots.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Unrecognized-noun detection
# ─────────────────────────────────────────────────────────────────────────────


def _looks_like_a_real_noun(noun: str) -> bool:
    """Filter ObservationTuple nouns down to ones worth proposing.

    Strip stop-words, single-character tokens, all-numeric tokens
    (timestamps + counts the extractor occasionally picks up), and
    nouns longer than 40 chars (likely an extraction artifact)."""
    n = noun.strip().lower()
    if not n or len(n) > 40:
        return False
    if n in _BORING_NOUNS:
        return False
    if len(n) < 3:
        return False
    if n.isnumeric():
        return False
    return True


def _noun_resolves_to_a_domain(
    noun: str, kw_map: dict[str, str]
) -> bool:
    """Substring match — mirrors the resolution path
    capability_gap_monitor + engagement_amplifier_monitor use."""
    noun_l = noun.lower()
    return any(kw in noun_l for kw in kw_map)


def collect_unrecognized_nouns(
    bot_id: str,
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Counter:
    """Return a Counter of (unrecognized noun → frequency) over the
    last ``window_days`` of ObservationTuples for this bot."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=window_days - 1)
    kw_map = mv.effective_keywords(shared_dir, now=now)
    counts: Counter = Counter()
    try:
        win = obs_window(
            bot_id, start=start, end=now, shared_dir=shared_dir
        )
    except Exception:
        return counts
    for t in win.tuples():
        noun = getattr(t, "noun", "")
        if not _looks_like_a_real_noun(noun):
            continue
        if _noun_resolves_to_a_domain(noun, kw_map):
            continue
        counts[noun.lower()] += 1
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Preflight report writer
# ─────────────────────────────────────────────────────────────────────────────


def _preflight_dir(shared_dir: Path) -> Path:
    return Path(shared_dir) / "vocabulary" / "preflight"


def _preflight_path(shared_dir: Path, bot_id: str) -> Path:
    return _preflight_dir(shared_dir) / f"{bot_id}.json"


def write_preflight_report(
    bot_id: str,
    shared_dir: Path,
    unrecognized: Counter,
    *,
    window_days: int,
    threshold: int,
    cap: int = PREFLIGHT_NOUN_CAP,
) -> Path:
    """Persist a preflight report describing what an LLM expansion
    call *would* look like for this bot. Inert by itself — the
    actual LLM hookup ships in PR γ."""
    nouns_sorted = unrecognized.most_common(cap)
    payload = {
        "schema_version": 1,
        "producer": PRODUCER,
        "bot_id": bot_id,
        "generated_at": mv._utc_now_iso(),
        "window_days": window_days,
        "threshold": threshold,
        "unrecognized_noun_count": sum(unrecognized.values()),
        "distinct_unrecognized_nouns": len(unrecognized),
        "nouns_for_classification": [
            {"noun": n, "frequency": c} for n, c in nouns_sorted
        ],
        "instructions_for_llm_caller": (
            "Pass the nouns_for_classification list to an LLM with a "
            "prompt that maps each to one of the current domain tags "
            "OR proposes a new domain tag. Per the per-bot inference "
            "rule, the call must use this bot's own credentials, not "
            "a centralized API key. Write structured proposals to "
            "{shared_dir}/vocabulary/pending.json for operator "
            "review. See internal/spec-rsi-proposal-eligibility-2026-"
            "06-05.md §Phase 2 / Layer 2 design discussion."
        ),
        "current_vocabulary_size": len(
            mv.effective_keywords(shared_dir)
        ),
    }
    path = _preflight_path(shared_dir, bot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def run_for_pod(
    bot_ids: Iterable[str],
    shared_dir: Path,
    *,
    config: _Config,
    now: datetime | None = None,
) -> dict:
    """Per-bot pass over the unrecognized-noun threshold. Returns a
    summary describing which bots cleared the threshold + what
    preflight reports were written."""
    bots = list(bot_ids)
    per_bot: dict[str, dict] = {}
    written: list[str] = []
    for bot_id in bots:
        unrecognized = collect_unrecognized_nouns(
            bot_id,
            shared_dir,
            now=now,
            window_days=config.window_days,
        )
        distinct = len(unrecognized)
        per_bot[bot_id] = {
            "distinct_unrecognized_nouns": distinct,
            "above_threshold": distinct >= config.threshold_nouns,
        }
        if distinct >= config.threshold_nouns:
            write_preflight_report(
                bot_id,
                shared_dir,
                unrecognized,
                window_days=config.window_days,
                threshold=config.threshold_nouns,
            )
            written.append(bot_id)
    return {
        "producer": PRODUCER,
        "enabled": config.enabled,
        "bots_scanned": len(bots),
        "threshold": config.threshold_nouns,
        "window_days": config.window_days,
        "preflight_reports_written": written,
        "per_bot": per_bot,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Layer 2 preflight — identify unrecognized observation "
            "nouns per bot and write preflight reports describing "
            "what an LLM expansion call would look like. No LLM "
            "calls in this monitor; PR γ wires that."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=Path("/Users/Shared/evolve/network.json"),
        help="Path to pod network.json (config gate lives here).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run even when the opt-in flag is off. Useful for dev "
            "inspection — never enabled in the daemon."
        ),
    )
    args = parser.parse_args()

    config = _load_pod_config(args.network)
    if not config.enabled and not args.force:
        # Default branch: do nothing. Zero cost when off.
        print(
            json.dumps({
                "producer": PRODUCER,
                "enabled": False,
                "note": (
                    "vocabulary expansion is off (set "
                    "network.json::rsi.vocabulary_expansion.enabled "
                    "= true to enable). No preflight reports written."
                ),
            }, indent=2),
            flush=True,
        )
        return 0

    bot_ids = _network_bot_ids(args.network)
    if not bot_ids:
        print(
            f"[{PRODUCER}] no bots found in {args.network}; nothing "
            f"to scan",
            file=sys.stderr,
        )
        return 0

    summary = run_for_pod(
        bot_ids,
        args.shared_dir,
        config=config,
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
