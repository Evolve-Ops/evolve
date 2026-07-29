"""fit_review — the L2 capability-ideation engine ("Fit Reviewer").

Spec: docs/spec-fit-reviewer-2026-06-12.md (buildable / authoritative).
Conceptual parent: docs/spec-effectiveness-layer-2026-06-09.md.

The Fit Reviewer reads a bot's *real usage against its declared purpose*,
picks the *one* capability the evidence demands, maps it to a *real installable
gallery app*, and emits a single high-altitude proposal — or, when the evidence
is thin, emits nothing.

The engine mirrors the App-Audit precedent: a pure-Python TARGETING stage
(purpose × tuple rollup) GATES a later bounded in-bot LLM reflection.

Modules:
  * `targeting.py` (Bite 1) — pure-Python targeting report. Given a bot's
    declared purpose + observation tuples, is there a recurring need that clears
    the support floor, lacks a covering installed app, and maps to a real gallery
    package? No LLM. (Spec §3.3 / §7.)
  * `archetypes.py` (Bite 1) — per-archetype targeting playbooks.
  * `reflection.py` (Bite 3) — the one bounded, I/O-free LLM reflection (injected
    callable, mirrors `user_profile_inferrer/extractor.py`). Cite-or-don't is
    enforced in code: a non-verbatim quote, an unattestable session, or an
    off-shortlist pkg_id ⇒ no candidate. (Spec §3.4 / §3.6.)
  * `runner.py` (Bite 3) — the bot-side orchestrator: opt-out gate → targeting
    gate (no LLM unless `targets_found`) → reflection → deterministic value
    (Gate B) → atomic candidate write to `{shared_dir}/fit_review/outbox/`.
    Weekly cadence, riding the hourly Tier-3 audit tick (no new launchd job).
    (Spec §3 / §7.)
  * `candidate.py` (Bite 4) — the tolerant *reader* the poller uses
    (`parse_candidate`); its alias map is the single reconciliation point for the
    runner's emitted field names.
  * `gates.py` (Bite 4) — the poller's deterministic Gate A / Gate B.

Soft verification (Bite 5) is still spec-only.
"""

from __future__ import annotations
