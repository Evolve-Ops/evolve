"""fit_review — the L2 capability-ideation engine ("Fit Reviewer").

Spec: internal/spec-fit-reviewer-2026-06-12.md (buildable / authoritative).
Conceptual parent: internal/spec-effectiveness-layer-2026-06-09.md.

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

identity: see ``applications.app_identity.resolve_app_id`` (AL-1.4b). Every
``pkg_id`` in this package is a GALLERY CATALOG key, never a manifest read.
The whole point of fit_review (spec §1.3) is that it may only name apps that
really exist in ``gallery/index.json``, so the id it carries is that file's
primary key — the string an ``InstallApp`` action must be given. Two further
reasons the resolver must not be substituted here:

  * the catalog row is not a manifest. It has carried an ``app_id`` column
    since #3413 holding the app SCRIPT name (``app_task_manager``), which is
    not the package key; PR #3681 made non-conforming values fall through to
    ``pkg_id``, so a resolver would return the right string today and the
    wrong one the day a row gains a conforming ``app_id``.
  * the installed-set builder in ``targeting`` is a permissive multi-key
    ACCUMULATOR, not a resolution — see its annotation.
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
