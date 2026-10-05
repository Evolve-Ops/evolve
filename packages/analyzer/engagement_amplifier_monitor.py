"""engagement_amplifier_monitor — "what's working, do more of it" detector.

Second Phase 2 RSI substrate monitor. Spec:
internal/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".

Companion to capability_gap_monitor (which detects MISSING capability —
"user keeps asking about X, no app covers it"). This monitor detects
the inverse: a conversational pattern that's WORKING and worth
deepening — high engagement, low frustration, recurring across
sessions and days.

The user's original RSI critique called this out specifically:

    "If anything, [RSI] should be focused on patterns of behavior,
    not outliers, so that it can propose ways to facilitate more
    of the desired activity."

The capability_gap_monitor closes the "find what users want and don't
have" half. This monitor closes the "find what users have and use
heavily" half.

For each bot:

  1. Walk ObservationTuples for the last 30 days.
  2. Cluster by (noun, verb) — finer-grain than capability_gap which
     uses noun alone. The (verb) axis is what makes "tracking workouts"
     a different pattern from "planning workouts", which deserves
     different deepening.
  3. Compute the bot's own engagement baseline from this window.
     A cluster qualifies as "high engagement" iff it's above the
     baseline's 75th percentile AND clears an absolute floor.
     Per-bot relative threshold prevents a quiet bot from never
     surfacing anything; absolute floor prevents a sleepy bot's
     top quartile from firing trivially.
  4. Mood gate: ``frustrated_mood_share`` must be below
     MAX_FRUSTRATED_SHARE. ``positive_mood_share`` (enthusiastic +
     reflective) is recorded but not required — neutral-mood
     patterns are valid amplification candidates (a calm working
     conversation isn't less valuable than an excited one).
  5. Recurrence: ≥ MIN_DISTINCT_SESSIONS distinct sessions and
     ≥ MIN_DISTINCT_DAYS distinct days within the window.
  6. Objective categorization: read the bot's workspace AGENTS.md.
     - Domain keyword matching the cluster's noun appears in
       purpose text → ``objective_alignment="confirmed"`` (the
       bot is delivering on its stated purpose; the amplification
       proposal says "double down on what's working").
     - No keyword → ``objective_alignment="emergent"`` (users have
       organically converged on this; the amplification proposal
       asks "consider whether to formally embrace this").
     - AGENTS.md unreadable → skip the bot (conservative; we need
       to know the bot's role to categorize the pattern).
  7. ``signals_store.observe`` emits an
     ``engagement_amplification_opportunity`` Signal per surviving
     (bot, noun, verb) triple.
  8. ``signals_store.sweep_resolve`` at the end of a pod-wide run
     so opportunities that stopped trending (mood degraded,
     engagement dropped, or the cluster faded out of the window)
     auto-resolve.

Consumer: ``engagement_amplifier`` generator subscribes to
``engagement_amplification_opportunity`` Signals and emits Phase A
operator-first Proposals on ``surface=improvement``.

Architectural note: pure Python, no LLM. Same cost posture as
capability_gap_monitor — the user's
``RSI infrastructure must be cheap`` memory rule. The 30-day window
is the read-heavy piece; the math on top is trivial.
"""
from __future__ import annotations

import json
import math
import statistics
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import CANONICAL_SHARED_DIR, get_members, get_primary, load_config
from observations.access import ObservationWindow, window as obs_window
from schema.observation import ClusterSpec
from signals import store as signals_store


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PRODUCER = "engagement_amplifier_monitor"
SIGNAL_TYPE = "engagement_amplification_opportunity"

DEFAULT_WINDOW_DAYS = 30

# Mood gate. ``frustrated`` is the disqualifier — even high-engagement
# clusters dominated by friction aren't amplification candidates; the
# persona_tuner pathway handles those. The threshold matches
# persona_tuner's frustration trigger (0.25); a cluster that's above
# that threshold is persona_tuner's problem, not ours.
MAX_FRUSTRATED_SHARE = 0.20

# Engagement gates — both must pass.
#   ABSOLUTE: a cluster must clear this floor regardless of bot
#             baseline. Stops a sleepy bot from amplifying trivial
#             clusters just because they're its top quartile.
#   PERCENTILE: relative-to-bot. A cluster must be above this
#               percentile of the bot's other cluster engagements
#               in the window.
ABSOLUTE_ENGAGEMENT_FLOOR = 15
RELATIVE_ENGAGEMENT_PERCENTILE = 0.75

# Recurrence gates.
MIN_DISTINCT_SESSIONS = 4
MIN_DISTINCT_DAYS = 4

# Per-bot signal cap. A pod-wide run that fires every cluster on the
# 75th percentile would flood the operator; cap to the strongest few.
# Picks top-K by engagement_total within bot. Beyond the cap, signals
# are dropped silently for this run; they re-emerge in next cycle if
# still strong.
MAX_SIGNALS_PER_BOT = 3

# AGENTS.md read cap.
AGENTS_MD_READ_CAP_BYTES = 8192

# Mood vocabulary partitioning (must match schema.observation.MOOD_VOCABULARY).
POSITIVE_MOODS = frozenset({"enthusiastic", "reflective"})
NEGATIVE_MOODS = frozenset({"frustrated"})
# "neutral", "urgent", "uncertain" are neither; they don't disqualify
# but don't boost either.


# ─────────────────────────────────────────────────────────────────────────────
# Domain keyword vocabulary — re-uses app_suggester's so producer and
# capability_gap_monitor speak the same domain language. Lazy import
# keeps this monitor importable in analyzer-only environments.
# ─────────────────────────────────────────────────────────────────────────────


def _domain_keywords(shared_dir: Path | None = None) -> dict[str, str]:
    """Return effective ``static ∪ dynamic`` vocabulary. See
    ``_merged_vocabulary.effective_keywords`` for the merge rules."""
    try:
        import _merged_vocabulary as mv

        return mv.effective_keywords(shared_dir)
    except ImportError:
        return {}


def _noun_to_domains(noun: str, kw_map: dict[str, str]) -> set[str]:
    noun_l = (noun or "").lower()
    if not noun_l:
        return set()
    out: set[str] = set()
    for kw, tag in kw_map.items():
        if kw in noun_l:
            out.add(tag)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Bot purpose reader — same pattern as capability_gap_monitor
# ─────────────────────────────────────────────────────────────────────────────


def _bot_workspace_agents_md(bot_id: str) -> Path:
    try:
        from evolve_admin.config import bot_home  # type: ignore

        home = bot_home(bot_id)
    except Exception:
        try:
            from evolve_config import bot_home as _bot_home

            home = _bot_home(bot_id)
        except Exception:
            from platform_profile import get_profile

            home = Path(get_profile().user_home_root) / bot_id
    return home / ".openclaw" / "workspace" / "AGENTS.md"


def _read_agents_md(bot_id: str) -> str | None:
    path = _bot_workspace_agents_md(bot_id)
    try:
        with path.open("r", encoding="utf-8") as f:
            return f.read(AGENTS_MD_READ_CAP_BYTES)
    except FileNotFoundError:
        return None
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                return r.stdout[:AGENTS_MD_READ_CAP_BYTES]
        except Exception:
            return None
        return None
    except Exception:
        return None


def _objective_alignment(
    purpose_text: str | None,
    noun: str,
    kw_map: dict[str, str],
    *,
    anti_domains: set[str] | None = None,
) -> str:
    """Categorize the (bot, noun) cluster against the bot's purpose.

    Returns one of:
      ``"confirmed"``    — a domain keyword for this noun appears
                           in AGENTS.md. Amplification = "deepen
                           what's working."
      ``"emergent"``     — no domain keyword overlap. Users have
                           organically converged on this pattern;
                           amplification = "consider whether to
                           embrace what users are doing."
      ``"contradicted"`` — the noun's domain is in the bot's
                           explicit "out of scope" markers AND
                           users are still engaging heavily.
                           Amplification = "users are doing
                           something the bot's AGENTS.md says is
                           out of scope — make a decision (widen
                           scope, redirect users, or tolerate)."
                           Distinct from the gap path's
                           contradicted handling: we still emit
                           the Signal because the operator wants
                           to know about it, not because they
                           want to act on the pattern as-is.
      ``"skip"``         — AGENTS.md unreadable. Conservative
                           skip.

    ``anti_domains`` — pre-parsed set of ``domain:*`` tags the bot
    has explicitly excluded. Pass the result of
    ``anti_domains.parse_anti_domains(purpose_text)`` once per bot
    and reuse across nouns.
    """
    if not purpose_text:
        return "skip"
    domains = _noun_to_domains(noun, kw_map)
    if anti_domains and domains & anti_domains:
        return "contradicted"
    purpose_l = purpose_text.lower()
    for kw, tag in kw_map.items():
        if tag in domains and kw in purpose_l:
            return "confirmed"
    # No keyword for this noun's domain appears in purpose. Could be
    # that the noun has no recognized domain at all (rare nouns); we
    # still emit as ``emergent`` because the value of the
    # amplification proposal is "users keep coming back to this even
    # though it's not in the bot's stated scope."
    return "emergent"


# ─────────────────────────────────────────────────────────────────────────────
# Cluster math
# ─────────────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], pct: float) -> float:
    """Return the pct-th percentile via nearest-rank. Returns 0 on
    empty input. Inclusive on the upper bound, so the 75th percentile
    of a 4-element list is the 3rd element."""
    if not values:
        return 0.0
    if pct <= 0:
        return min(values)
    if pct >= 1:
        return max(values)
    sorted_vals = sorted(values)
    # nearest-rank: rank = ceil(pct * N), 1-indexed
    rank = max(1, math.ceil(pct * len(sorted_vals)))
    return float(sorted_vals[rank - 1])


def _positive_mood_share(cell) -> float:
    """Fraction of tuples tagged with an explicitly positive mood
    (``enthusiastic`` or ``reflective``). Used as a tie-breaker /
    confidence boost in the proposal narrative, not as a hard gate —
    neutral-mood clusters are valid amplification candidates."""
    if not cell.tuple_count:
        return 0.0
    dist = cell.mood_distribution or {}
    positive_count = sum(dist.get(m, 0) for m in POSITIVE_MOODS)
    return positive_count / cell.tuple_count


def _date_span_days(earliest: str, latest: str) -> int:
    """Calendar-day span (inclusive) covered by a cell. Coarse but
    enough for the MIN_DISTINCT_DAYS gate; see capability_gap_monitor
    for the same approach + rationale."""
    if not earliest or not latest:
        return 0
    try:
        e = date.fromisoformat(earliest[:10])
        l = date.fromisoformat(latest[:10])
        return max(0, (l - e).days) + 1
    except ValueError:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────


def detect_amplification_opportunities(
    bot_id: str,
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[dict]:
    """Compute detections for one bot. Returns dicts ready to pass to
    ``signals_store.observe(**d)``.

    Empty list when nothing clears the gates (a quiet bot, all
    clusters too small, or every candidate flunks the mood gate
    because the bot is having a bad month)."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=window_days - 1)
    # Pass shared_dir so Layer 2 dynamic vocab overlays the static map.
    kw_map = _domain_keywords(shared_dir)
    purpose_text = _read_agents_md(bot_id)
    # Anti-domain markers — parsed once per bot. Empty set when
    # AGENTS.md has no explicit "out of scope" markers (the common
    # case); the alignment path then can't return "contradicted" and
    # the pre-anti-domain behavior is preserved.
    from anti_domains import parse_anti_domains
    anti = parse_anti_domains(purpose_text, shared_dir)

    win = obs_window(
        bot_id, start=start, end=now, shared_dir=shared_dir
    )
    view = win.clusters(by=ClusterSpec(group_by=["noun", "verb"]))
    if not view.cells:
        return []

    # Per-bot baseline. Compute the engagement-total percentile from
    # all cells the bot has surfaced this window. Empty / single-cell
    # baselines fall back to "absolute floor only" — a bot that's only
    # active in one cluster shouldn't have nothing to amplify just
    # because there's no distribution.
    engagement_values = [
        float(c.engagement_total) for c in view.cells
        if c.engagement_total > 0
    ]
    relative_floor = (
        _percentile(engagement_values, RELATIVE_ENGAGEMENT_PERCENTILE)
        if len(engagement_values) >= 2
        else 0.0
    )

    # Score candidates that pass every gate. We compute the dict-shape
    # detection now but defer ``observe()`` so we can rank + cap first.
    candidates: list[tuple[float, dict]] = []
    for cell in view.cells:
        if not cell.key or len(cell.key) < 2:
            continue
        noun = str(cell.key[0]) if cell.key[0] is not None else ""
        verb = str(cell.key[1]) if cell.key[1] is not None else ""
        if not noun or not verb:
            continue

        # Mood gate — disqualify frustration-heavy clusters.
        if cell.frustrated_mood_share > MAX_FRUSTRATED_SHARE:
            continue

        # Engagement gates — both absolute and relative.
        eng = int(cell.engagement_total)
        if eng < ABSOLUTE_ENGAGEMENT_FLOOR:
            continue
        if eng < relative_floor:
            continue

        # Recurrence gate.
        if cell.distinct_sessions < MIN_DISTINCT_SESSIONS:
            continue
        if _date_span_days(cell.earliest, cell.latest) < MIN_DISTINCT_DAYS:
            continue

        # Objective alignment — requires AGENTS.md readable.
        alignment = _objective_alignment(
            purpose_text, noun, kw_map, anti_domains=anti
        )
        if alignment == "skip":
            continue

        positive_share = _positive_mood_share(cell)
        domain_tags = sorted(_noun_to_domains(noun, kw_map))
        signature = (
            f"engagement_amplification:{bot_id}:{noun}:{verb}"
        )
        # Title — keep operator-readable, no jargon. The Signal title
        # surfaces directly in alert lists, so make it count.
        verb_friendly = verb.removesuffix("ing").rstrip("-") or verb
        title = (
            f"Amplifiable pattern: {bot_id} {verb_friendly} "
            f"{noun} ({eng} engagement)"
        )[:120]
        body = (
            f"Cluster ({noun}, {verb}) on {bot_id}: {cell.distinct_sessions} "
            f"distinct sessions, engagement total {eng}, "
            f"frustration {int(round(cell.frustrated_mood_share*100))}%, "
            f"positive-mood {int(round(positive_share*100))}%. "
            f"Bot's AGENTS.md fit: {alignment}."
        )
        detection = {
            "signature": signature,
            "producer": PRODUCER,
            "type": SIGNAL_TYPE,
            "flavor": "activity",
            "severity": "info",
            "scope": "bot",
            "bot_id": bot_id,
            "title": title,
            "body": body,
            "details": {
                "bot_id": bot_id,
                "noun": noun,
                "verb": verb,
                "domain_tags": domain_tags,
                "objective_alignment": alignment,
                "engagement_total": eng,
                "distinct_sessions": int(cell.distinct_sessions),
                "distinct_days": _date_span_days(
                    cell.earliest, cell.latest
                ),
                "frustrated_share": round(
                    cell.frustrated_mood_share, 3
                ),
                "positive_mood_share": round(positive_share, 3),
                "earliest": cell.earliest,
                "latest": cell.latest,
                "window_days": window_days,
            },
        }
        # Score: engagement_total breaks ties by positive_mood_share.
        score = eng + positive_share
        candidates.append((score, detection))

    # Rank + cap.
    candidates.sort(key=lambda t: -t[0])
    return [d for _, d in candidates[:MAX_SIGNALS_PER_BOT]]


# ─────────────────────────────────────────────────────────────────────────────
# Runner — per-bot + pod-wide entry points
# ─────────────────────────────────────────────────────────────────────────────


def run_for_bot(
    bot_id: str,
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool = False,
) -> set[str]:
    """Observe detections for one bot. Returns kept signatures so the
    caller's sweep_resolve leaves them alone."""
    kept: set[str] = set()
    for d in detect_amplification_opportunities(
        bot_id, shared_dir, now=now, window_days=window_days
    ):
        kept.add(d["signature"])
        if dry_run:
            print(json.dumps({"would_observe": d}), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **d)
        except Exception as exc:
            print(
                f"[engagement_amplifier_monitor] observe failed for "
                f"{d['signature']}: {exc}",
                flush=True,
            )
    return kept


def run_for_pod(
    bot_ids: Iterable[str],
    shared_dir: Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: bool = False,
) -> dict:
    """Pod-wide entry point. Runs every bot, then sweep_resolves any
    Signal from this producer whose signature wasn't reproduced —
    i.e. the opportunity faded (engagement dropped, mood degraded,
    cluster aged out of the window)."""
    bots = list(bot_ids)
    all_kept: set[str] = set()
    per_bot_counts: dict[str, int] = {}
    for bot_id in bots:
        kept = run_for_bot(
            bot_id,
            shared_dir,
            now=now,
            window_days=window_days,
            dry_run=dry_run,
        )
        per_bot_counts[bot_id] = len(kept)
        all_kept.update(kept)
    resolved: list = []
    if not dry_run:
        resolved = signals_store.sweep_resolve(
            shared_dir,
            producer=PRODUCER,
            kept_signatures=all_kept,
            types={SIGNAL_TYPE},
            bot_ids=set(bots),
            reason="amplification opportunity faded",
        )
    return {
        "producer": PRODUCER,
        "bots_scanned": len(bots),
        "signals_emitted": len(all_kept),
        "signals_resolved": len(resolved),
        "per_bot_counts": per_bot_counts,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Detect high-engagement low-frustration conversation "
            "patterns worth amplifying."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path(CANONICAL_SHARED_DIR),
    )
    parser.add_argument(
        "--network",
        default=None,
        help="Path to network.json (default: platform-keyed canonical config).",
    )
    parser.add_argument("--bot", action="append", default=None)
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print detections; do not write Signals or sweep.",
    )
    args = parser.parse_args()
    if args.bot:
        bot_ids = list(args.bot)
    else:
        # Same pattern as cost_watchdog.main — read network.json for the
        # primary + member bots. Fail-loud rather than fall back to a
        # filesystem scan.
        try:
            config = load_config(args.network)
            primary = get_primary(config)
            members = get_members(config)
            bot_ids = (
                [primary] if primary and primary not in members else []
            ) + members
            bot_ids = [b for b in bot_ids if b]
        except Exception as exc:
            raise SystemExit(
                f"[engagement_amplifier_monitor] could not read "
                f"member bots: {exc}"
            )
    summary = run_for_pod(
        bot_ids,
        args.shared_dir,
        window_days=args.window_days,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
