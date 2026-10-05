"""rsi_dry_run — Exercise the Phase 2 RSI substrate against a sample
AGENTS.md and synthetic conversation patterns.

Purpose. Before this tool, an operator who wanted to know "given this
AGENTS.md and this conversation pattern, what would my Recommendations
page surface?" had two options:

  1. Wait a day for the daemons to run against real observations and
     hope the pattern fires.
  2. Read the monitor source code and the spec doc and reason it
     through manually.

Neither is fast enough to iterate on AGENTS.md conventions —
especially the explicit ``## Out of scope`` markers that anti-domain
detection introduced. This tool closes that gap.

Usage:

    python3 -m tools.rsi_dry_run \\
        --agents-md ./samples/fitness-bot.md \\
        --pattern workout:tracking:8:8 \\
        --pattern fitness:exploring:4:4

Each ``--pattern`` adds a synthetic conversation cluster with the
shape ``noun:verb:n_sessions:n_days``. Engagement-per-session
defaults to 4 (clear of MIN_ENGAGEMENT_TOTAL for both monitors); pass
``noun:verb:n_sessions:n_days:engagement_each`` to override.

The tool writes synthetic ObservationTuples + the operator's
AGENTS.md to a tempdir, runs every Phase 2 producer that's loadable
on the current main, and prints what would emit:

  - Producer Signals (with details)
  - Per-bot Proposals (when a consumer can be exercised inline)

Nothing is written to ``{shared_dir}/signals`` — everything stays in
the tempdir and gets cleaned up.

What's exercised vs skipped is reported at startup. As open Phase 2
PRs merge to main (engagement_amplifier, pod_capability_lift,
anti-domain detection), the tool gracefully picks them up via
conditional imports.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Substrate availability — conditional imports report
# ─────────────────────────────────────────────────────────────────────────────


def _detect_available_substrate() -> dict[str, Any]:
    """Probe what Phase 2 modules are importable on the current main.

    Returns a dict of {module_name: imported_module_or_None}. The CLI
    prints this at startup so the operator can see what's exercised
    vs skipped — a hedge against silently producing a partial report.
    """
    available: dict[str, Any] = {}
    try:
        import capability_gap_monitor as cap_mod  # type: ignore

        available["capability_gap_monitor"] = cap_mod
    except ImportError:
        available["capability_gap_monitor"] = None
    try:
        from generators.app_suggester import (  # type: ignore
            AppSuggesterContext,
            observe as app_suggester_observe,
        )

        available["app_suggester"] = (
            AppSuggesterContext,
            app_suggester_observe,
        )
    except ImportError:
        available["app_suggester"] = None
    try:
        import engagement_amplifier_monitor as amp_mod  # type: ignore

        available["engagement_amplifier_monitor"] = amp_mod
    except ImportError:
        available["engagement_amplifier_monitor"] = None
    try:
        from generators.engagement_amplifier import (  # type: ignore
            EngagementAmplifierContext,
            observe as amp_generator_observe,
        )

        available["engagement_amplifier"] = (
            EngagementAmplifierContext,
            amp_generator_observe,
        )
    except ImportError:
        available["engagement_amplifier"] = None
    try:
        from generators.pod_capability_lift import (  # type: ignore
            PodCapabilityLiftContext,
            observe as pod_lift_observe,
        )

        available["pod_capability_lift"] = (
            PodCapabilityLiftContext,
            pod_lift_observe,
        )
    except ImportError:
        available["pod_capability_lift"] = None
    try:
        from anti_domains import parse_anti_domains  # type: ignore

        available["anti_domains"] = parse_anti_domains
    except ImportError:
        available["anti_domains"] = None
    return available


# ─────────────────────────────────────────────────────────────────────────────
# Pattern + fixture writers
# ─────────────────────────────────────────────────────────────────────────────


class _PatternSpec:
    """One synthetic conversation pattern. Fields parse from the
    ``noun:verb:n_sessions:n_days[:engagement_each]`` CLI string."""

    __slots__ = ("noun", "verb", "n_sessions", "n_days", "engagement_each", "mood")

    def __init__(
        self,
        noun: str,
        verb: str,
        n_sessions: int,
        n_days: int,
        engagement_each: int = 4,
        mood: str | None = "enthusiastic",
    ):
        self.noun = noun
        self.verb = verb
        self.n_sessions = n_sessions
        self.n_days = n_days
        self.engagement_each = engagement_each
        self.mood = mood

    @classmethod
    def parse(cls, raw: str) -> "_PatternSpec":
        parts = raw.split(":")
        if len(parts) < 4 or len(parts) > 6:
            # Exit 2 — Unix convention for usage errors (matches
            # argparse's own behavior). Test contract: usage errors
            # all share the same exit code so operators can
            # ``if cli ... ; then ... ; fi`` cleanly.
            print(
                f"Invalid --pattern {raw!r}: expected "
                f"'noun:verb:n_sessions:n_days[:engagement_each[:mood]]'",
                file=sys.stderr,
            )
            sys.exit(2)
        noun, verb = parts[0], parts[1]
        try:
            n_sessions = int(parts[2])
            n_days = int(parts[3])
            engagement_each = int(parts[4]) if len(parts) >= 5 else 4
        except ValueError as exc:
            print(
                f"Invalid --pattern {raw!r}: numeric field not an int "
                f"({exc})",
                file=sys.stderr,
            )
            sys.exit(2)
        mood = parts[5] if len(parts) >= 6 else "enthusiastic"
        return cls(noun, verb, n_sessions, n_days, engagement_each, mood)


def _write_synthetic_tuples(
    shared_dir: Path, bot_id: str, patterns: list[_PatternSpec], now: datetime
) -> int:
    """Write synthetic ObservationTuples to {shared_dir}/observations.

    Returns the total tuple count written, useful for the startup
    report so the operator can sanity-check the fixture density."""
    from observations.tuples import write_tuples
    from schema.observation import ObservationTuple

    total = 0
    for p in patterns:
        for i in range(p.n_sessions):
            day = now - timedelta(days=(i % p.n_days))
            t = ObservationTuple(
                id=f"dry-run-{p.noun}-{p.verb}-{i}",
                bot_id=bot_id,
                session_id=f"dry-sess-{p.noun}-{p.verb}-{i}",
                segment_id=f"dry-seg-{i}",
                noun=p.noun,
                verb=p.verb,
                mood=p.mood,
                engagement=p.engagement_each,
                timestamp_start=day.isoformat(),
                timestamp_end=(day + timedelta(minutes=5)).isoformat(),
                source_hash=f"dry-hash-{p.noun}-{p.verb}-{i}",
            )
            write_tuples([t], shared_dir=shared_dir, bot_id=bot_id, day=day)
            total += 1
    return total


def _patch_agents_md(modules: dict[str, Any], agents_md_path: Path) -> None:
    """Redirect every available monitor's AGENTS.md reader at the
    operator-supplied sample. We monkey-patch each module's
    ``_bot_workspace_agents_md`` to return the sample path —
    side-effect-free because this script only runs in a tempdir."""
    for name in (
        "capability_gap_monitor",
        "engagement_amplifier_monitor",
    ):
        mod = modules.get(name)
        if mod is None:
            continue
        mod._bot_workspace_agents_md = lambda _bid, _p=agents_md_path: _p


def _stub_empty_manifests(shared_dir: Path, bot_id: str) -> None:
    """Empty applications dir so coverage-check returns empty — every
    candidate counts as uncovered, which is the "clean slate" state
    most useful for dry-runs."""
    (shared_dir / "applications" / bot_id).mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Run + report
# ─────────────────────────────────────────────────────────────────────────────


def _run_cap_gap(
    cap_mod: Any, bot_id: str, shared_dir: Path, now: datetime
) -> list[dict]:
    return cap_mod.detect_capability_gaps(bot_id, shared_dir, now=now)


def _run_amp_monitor(
    amp_mod: Any, bot_id: str, shared_dir: Path, now: datetime
) -> list[dict]:
    return amp_mod.detect_amplification_opportunities(
        bot_id, shared_dir, now=now
    )


def _drop_signals_into_store(
    detections: list[dict], shared_dir: Path
) -> int:
    """Materialize detection dicts as actual Signals so downstream
    consumers (app_suggester, engagement_amplifier generator,
    pod_capability_lift) can read them. Stays inside the tempdir."""
    from signals import store as signals_store

    n = 0
    for d in detections:
        try:
            signals_store.observe(shared_dir, **d)
            n += 1
        except Exception as exc:
            print(
                f"  ⚠ signals_store.observe failed for "
                f"{d.get('signature', '?')}: {exc}",
                file=sys.stderr,
            )
    return n


def _run_consumer(consumer_pair, bot_id: str, shared_dir: Path) -> list:
    """Construct the consumer's context + call observe(). Returns
    a list of Proposals (may be empty). Consumer-pair shape is
    (ContextCls, observe_fn)."""
    if consumer_pair is None:
        return []
    ContextCls, observe_fn = consumer_pair
    ctx = ContextCls(bot_ids=[bot_id], shared_dir=shared_dir)
    return observe_fn(ctx)


def _report_substrate_status(modules: dict[str, Any]) -> None:
    print("Phase 2 substrate availability:")
    for name in (
        "capability_gap_monitor",
        "app_suggester",
        "engagement_amplifier_monitor",
        "engagement_amplifier",
        "pod_capability_lift",
        "anti_domains",
    ):
        status = "✓" if modules.get(name) is not None else "—"
        print(f"  {status} {name}")
    print()


def _report_anti_domains(
    parse_fn, agents_md_text: str
) -> None:
    if parse_fn is None:
        print(
            "Anti-domain parser not loadable on this main; skipping "
            "exclusion report."
        )
        return
    excluded = parse_fn(agents_md_text)
    if excluded:
        print(
            "AGENTS.md exclusions detected: "
            + ", ".join(sorted(excluded))
        )
    else:
        print(
            "AGENTS.md exclusions detected: (none — no `## Out of scope` "
            "markers found)"
        )
    print()


def _report_detection(label: str, detections: list[dict]) -> None:
    if not detections:
        print(f"{label}: 0 detections")
        return
    print(f"{label}: {len(detections)} detection(s)")
    for d in detections:
        sig = d.get("signature", "?")
        title = d.get("title", "?")
        details = d.get("details") or {}
        keys_of_interest = [
            "objective_fit",
            "objective_alignment",
            "distinct_sessions",
            "distinct_days",
            "engagement_total",
        ]
        ev = ", ".join(
            f"{k}={details[k]}" for k in keys_of_interest if k in details
        )
        print(f"  • {title}")
        print(f"    signature: {sig}")
        if ev:
            print(f"    {ev}")
    print()


def _report_proposals(label: str, proposals: list) -> None:
    if not proposals:
        print(f"{label}: 0 proposals")
        return
    print(f"{label}: {len(proposals)} proposal(s)")
    for p in proposals:
        head = getattr(p, "admin_surface_summary", "") or "?"
        summary = (getattr(p, "summary", "") or "")[:200]
        action_label = getattr(p, "action_label", "") or ""
        print(f"  • {head}")
        if summary:
            print(f"    summary: {summary}")
        if action_label:
            print(f"    action_label: {action_label}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the Phase 2 RSI substrate against a sample "
            "AGENTS.md and synthetic conversation patterns."
        )
    )
    parser.add_argument(
        "--bot",
        default="team-bot-a",
        help="Synthetic bot id (default: team-bot-a).",
    )
    parser.add_argument(
        "--agents-md",
        type=Path,
        required=True,
        help="Path to a sample AGENTS.md file.",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        default=[],
        help=(
            "Synthetic conversation pattern, repeatable. Shape: "
            "noun:verb:n_sessions:n_days[:engagement_each[:mood]]. "
            "Example: workout:tracking:8:8."
        ),
    )
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "Override 'now' for the synthetic observations (ISO8601). "
            "Defaults to the real UTC now."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the substrate-availability + fixture report.",
    )
    args = parser.parse_args()

    if not args.agents_md.exists():
        print(
            f"AGENTS.md not found at {args.agents_md}",
            file=sys.stderr,
        )
        return 2
    agents_md_text = args.agents_md.read_text(encoding="utf-8")

    if not args.pattern:
        print(
            "No --pattern given; nothing to exercise. Example: "
            "--pattern workout:tracking:8:8",
            file=sys.stderr,
        )
        return 2
    patterns = [_PatternSpec.parse(p) for p in args.pattern]

    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            print(f"Invalid --now {args.now!r}: {exc}", file=sys.stderr)
            return 2
    else:
        # Anchored to a fixed date for determinism — using the real
        # now() would make this script's output drift with calendar
        # time. The window math is relative so the anchor doesn't
        # matter for the report.
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    modules = _detect_available_substrate()

    with tempfile.TemporaryDirectory(prefix="rsi-dry-run-") as td:
        shared_dir = Path(td)
        # AGENTS.md is read by the monitors via their
        # _bot_workspace_agents_md helper — patch it to point at the
        # operator's sample.
        _patch_agents_md(modules, args.agents_md)
        _stub_empty_manifests(shared_dir, args.bot)

        if not args.quiet:
            _report_substrate_status(modules)
            _report_anti_domains(
                modules.get("anti_domains"), agents_md_text
            )

        # Materialize synthetic observations.
        tuple_count = _write_synthetic_tuples(
            shared_dir, args.bot, patterns, now
        )
        if not args.quiet:
            print(
                f"Wrote {tuple_count} synthetic ObservationTuples across "
                f"{len(patterns)} pattern(s) for bot {args.bot}."
            )
            print()

        # ── Run available producers ─────────────────────────────────
        cap_mod = modules.get("capability_gap_monitor")
        if cap_mod is not None:
            cap_dets = _run_cap_gap(cap_mod, args.bot, shared_dir, now)
            _report_detection("capability_gap_monitor", cap_dets)
            _drop_signals_into_store(cap_dets, shared_dir)

        amp_mod = modules.get("engagement_amplifier_monitor")
        if amp_mod is not None:
            amp_dets = _run_amp_monitor(amp_mod, args.bot, shared_dir, now)
            _report_detection("engagement_amplifier_monitor", amp_dets)
            _drop_signals_into_store(amp_dets, shared_dir)

        # ── Run available consumers ─────────────────────────────────
        suggester_pair = modules.get("app_suggester")
        if suggester_pair is not None:
            props = _run_consumer(suggester_pair, args.bot, shared_dir)
            _report_proposals("app_suggester proposals", props)

        amp_gen_pair = modules.get("engagement_amplifier")
        if amp_gen_pair is not None:
            props = _run_consumer(amp_gen_pair, args.bot, shared_dir)
            _report_proposals(
                "engagement_amplifier proposals", props
            )

        pod_lift_pair = modules.get("pod_capability_lift")
        if pod_lift_pair is not None:
            # Pod-wide synthesis requires multiple bots. Single-bot
            # dry-run doesn't exercise it meaningfully — note that
            # to the operator rather than silently report 0.
            print(
                "pod_capability_lift skipped: single-bot fixture; "
                "cross-bot synthesis requires the pattern on ≥ 3 bots."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
