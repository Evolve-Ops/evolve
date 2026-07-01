#!/usr/bin/env python3
"""
session_surface.py — Continuity Engine session-start surfacing

Injects several things into every session's system prompt:
  1. Pod conduct summary (always) — condensed behavioral rules with pointer
     to the full POD_CONDUCT.md for detail.
  2. Bot guide (when present) — primary-authored mission statement for
     this specific bot, read from {shared_dir}/bot_guides/{bot_id}.md.
     Spec: docs/spec-evo-wizard-2026-05-05.md §5.
  3. Home report banner (primary bot only) — the LLM-generated narrative
     the admin sees as a banner at the top of the Evolve admin home
     page, read from {shared_dir}/home-narrative-cache.json. Lets evo
     answer "what was that thing about Codex in the report?" without
     punting on banner content it didn't otherwise see. Injected PER
     TURN (not just session_start) so a regenerated cache mid-
     conversation reaches the next turn's system prompt — see
     ``docs/diagnosis-evo-briefing-context-gap-2026-05-26.md`` failure
     modes 2 (chat session pre-dates the cache) and 3 (cache regenerated
     between session_start and the next turn).
  4. Evolve notifications (when --user-key set + unread events) —
     forge build complete / failed / etc., from per-user queue at
     {shared_dir}/notifications/<user_key>.jsonl.
     Spec: docs/spec-forge-via-messaging-2026-05-07.md §5.
  5. Pending approval tasks (when present) — tasks queued from prior sessions
     that need the user's go-ahead before running.

Two invocation modes:

* **session_start** (default) — called by the bot's session_start hook via
  TurnObserver → handleSessionStart. Emits the full session prefix
  (conduct, runtime notes, guide, role scaffolds, posture, firing-signals
  snapshot, notifications, …). The plugin returns
  { systemAppend: <output> } which openclaw prepends to the system prompt
  before the first turn.

* **per-turn** (``--per-turn``) — called by the bot's before_prompt_build
  hook via TurnObserver. Emits ONLY the per-turn-eligible blocks (today:
  the home-narrative block) so a regenerated narrative cache reaches the
  LLM the very next turn, not at session_start time. The plugin returns
  { appendSystemContext: <output> } which pi-embedded threads into the
  system prompt for that single turn.

Usage:
    python3 session_surface.py --bot admin_bot
    python3 session_surface.py --bot admin_bot --user-key ext:telegram:12345
    python3 session_surface.py --bot admin_bot --json
    python3 session_surface.py --bot admin_bot --per-turn       # narrative only
    python3 session_surface.py --bot admin_bot --quiet   # exit 0, no output if empty

Exit codes:
    0 — nothing to surface (quiet mode, or no tasks and conduct already cached)
    1 — content surfaced (caller should inject the output)
    2 — error loading queue
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ── Pod conduct summary ───────────────────────────────────────────────────────
# Extracted from the <!-- evolve-pod-conduct:begin/end --> block in
# docs/system/POD_CONDUCT.md. These marker names match the content-scan
# allowlist (packages/analyzer/content_scan/default_patterns.py) so the
# scanner doesn't flag our own injected file as a prompt-injection vector.
# Edit the summary there — this constant is derived from that file at import time.

_CONDUCT_SOURCE = Path(__file__).parent.parent.parent / "docs" / "system" / "POD_CONDUCT.md"

_SUMMARY_BEGIN = "<!-- evolve-pod-conduct:begin -->"
_SUMMARY_END = "<!-- evolve-pod-conduct:end -->"


def _load_conduct_summary() -> str:
    """Extract the <!-- evolve-pod-conduct:begin/end --> block from POD_CONDUCT.md.

    Falls back to a minimal inline string if the file is missing or malformed,
    so a misconfigured deploy path never silently kills the session-start hook.
    """
    try:
        text = _CONDUCT_SOURCE.read_text()
        start = text.find(_SUMMARY_BEGIN)
        end = text.find(_SUMMARY_END)
        if start == -1 or end == -1:
            raise ValueError("evolve-pod-conduct markers not found in POD_CONDUCT.md")
        return text[start + len(_SUMMARY_BEGIN):end].strip()
    except Exception as e:
        print(f"[session_surface] WARNING: could not load conduct summary from {_CONDUCT_SOURCE}: {e}", file=sys.stderr)
        return "[POD CONDUCT] Rules unavailable — see POD_CONDUCT.md in your workspace for full rules."


POD_CONDUCT_SUMMARY = _load_conduct_summary()


# ── Runtime notes (platform facts) ───────────────────────────────────────────
# Sibling to POD_CONDUCT: behavioural norms live there, tactical platform
# facts (e.g. "OC's exec preflight blocks pipes after python") live here.
# Same injection channel — extracted from a marker block, prepended to every
# session's system prompt. See docs/system/RUNTIME_NOTES.md for the
# operator-facing context; the bot only sees the markered block.

_RUNTIME_NOTES_SOURCE = Path(__file__).parent.parent.parent / "docs" / "system" / "RUNTIME_NOTES.md"

_RUNTIME_NOTES_BEGIN = "<!-- evolve-runtime-notes:begin -->"
_RUNTIME_NOTES_END = "<!-- evolve-runtime-notes:end -->"


def _load_runtime_notes() -> str:
    """Extract the <!-- evolve-runtime-notes:begin/end --> block from
    RUNTIME_NOTES.md.

    Returns "" (not a fallback string) when the file is missing or malformed
    — runtime notes are additive guidance, not load-bearing rules, so the
    correct failure mode is to inject nothing rather than a placeholder.
    """
    try:
        text = _RUNTIME_NOTES_SOURCE.read_text()
        start = text.find(_RUNTIME_NOTES_BEGIN)
        end = text.find(_RUNTIME_NOTES_END)
        if start == -1 or end == -1:
            raise ValueError("evolve-runtime-notes markers not found in RUNTIME_NOTES.md")
        return text[start + len(_RUNTIME_NOTES_BEGIN):end].strip()
    except Exception as e:
        print(
            f"[session_surface] WARNING: could not load runtime notes from "
            f"{_RUNTIME_NOTES_SOURCE}: {e}",
            file=sys.stderr,
        )
        return ""


RUNTIME_NOTES = _load_runtime_notes()


# ── Coherence vocabulary ─────────────────────────────────────────────────────
# Sibling to RUNTIME_NOTES: documents the assertion-id and severity vocabulary
# the bot's app findings use. Lets a Telegram/Slack repair conversation reason
# about a finding by id ("C-A1 means recurring behavior with no trigger")
# without the bot having to guess. Extracted from a marker block, same as
# POD_CONDUCT and RUNTIME_NOTES.
#
# See docs/system/COHERENCE_VOCAB.md for the operator-facing context; the bot
# only sees the markered block. Sourced from
# packages/admin/evolve_admin/applications/coherence_pass_a.py + _c1.py.

_COHERENCE_VOCAB_SOURCE = (
    Path(__file__).parent.parent.parent / "docs" / "system" / "COHERENCE_VOCAB.md"
)

_COHERENCE_VOCAB_BEGIN = "<!-- evolve-coherence-vocab:begin -->"
_COHERENCE_VOCAB_END = "<!-- evolve-coherence-vocab:end -->"


def _load_coherence_vocab() -> str:
    """Extract the <!-- evolve-coherence-vocab:begin/end --> block.

    Returns "" when the file is missing or markers are absent — vocab is
    additive guidance, not load-bearing rules, so a missing file shouldn't
    block session startup.
    """
    try:
        text = _COHERENCE_VOCAB_SOURCE.read_text()
        start = text.find(_COHERENCE_VOCAB_BEGIN)
        end = text.find(_COHERENCE_VOCAB_END)
        if start == -1 or end == -1:
            raise ValueError("evolve-coherence-vocab markers not found in COHERENCE_VOCAB.md")
        return text[start + len(_COHERENCE_VOCAB_BEGIN):end].strip()
    except Exception as e:
        print(
            f"[session_surface] WARNING: could not load coherence vocab from "
            f"{_COHERENCE_VOCAB_SOURCE}: {e}",
            file=sys.stderr,
        )
        return ""


COHERENCE_VOCAB = _load_coherence_vocab()


def _shared_dir() -> Path:
    env = os.environ.get("EVOLVE_NETWORK")
    if env:
        return Path(env).parent
    return Path("/Users/Shared/evolve")


def load_notifications_block(user_key: str | None, shared_dir: Path) -> str:
    """Read unread notification events for ``user_key`` and render them
    as a system-prompt section. Returns "" when user_key is None
    (callers without a user identity get no notifications surfaced) or
    the queue is empty / unavailable.

    On successful render the queue is marked-read so the same events
    don't surface again next session. Failure paths are silent: if the
    notifications module isn't importable or the read errors, we return
    "" and the bot's session starts without that block — no crash.
    """
    if not user_key:
        return ""
    try:
        from evolve_admin.evo import notifications as _n  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"[session_surface] WARNING: notifications module unavailable "
            f"({e}) — skipping",
            file=sys.stderr,
        )
        return ""

    try:
        events = _n.read_unread(shared_dir, user_key)
    except Exception as e:
        print(
            f"[session_surface] WARNING: notifications read error "
            f"({e}) — skipping",
            file=sys.stderr,
        )
        return ""

    if not events:
        return ""

    block = _n.render_for_session_prompt(events)
    # Mark events read AFTER successfully composing the block. If the
    # composition fails or returns empty we don't mark — the events
    # surface on the next attempt instead of being silently lost.
    if block:
        try:
            _n.mark_read(shared_dir, user_key)
        except Exception as e:
            # Best-effort — losing mark_read just means the user sees
            # the same events again next session. Annoying but not lossy.
            print(
                f"[session_surface] WARNING: notifications mark_read failed "
                f"({e}) — events will repeat next session",
                file=sys.stderr,
            )
    return block


def load_bot_guide_block(bot_id: str | None, shared_dir: Path) -> str:
    """Render the bot guide as a system-prompt section, or "" if no guide.

    Soft-fails: a malformed guide returns "", never raises. The bot guide
    sits in the runtime hot path — a corrupt file should not block session
    startup. The guide module's ``load_for_session_surface`` already
    swallows parse errors, but we wrap the import too so a missing
    evolve_admin package (e.g. analyzer-only deploys, future
    refactors) doesn't break session_surface either.
    """
    if not bot_id:
        return ""
    try:
        from evolve_admin.evo import guide as _guide  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"[session_surface] WARNING: bot guide module unavailable ({e}) — "
            "guide will not be injected",
            file=sys.stderr,
        )
        return ""

    try:
        loaded = _guide.load_for_session_surface(shared_dir, bot_id)
    except Exception as e:  # belt-and-suspenders; load_for_session_surface is soft already
        print(
            f"[session_surface] WARNING: bot guide load error ({e}) — "
            "guide will not be injected",
            file=sys.stderr,
        )
        return ""
    if loaded is None:
        return ""

    return _format_guide_block(loaded)


def _format_guide_block(g) -> str:
    """Format a Guide as a labeled system-prompt section. The label
    distinguishes "what I am" (SOUL) from "how I'm being used here" (guide)
    so the bot can reason about both without conflating them."""
    fm = dict(g.frontmatter or {})
    audience = fm.get("audience")
    tone = fm.get("tone")
    do_say = fm.get("do_say") or []
    dont_say = fm.get("dont_say") or []
    body = (g.body or "").strip()

    lines = [
        "[BOT GUIDE — primary-authored mission for this bot]",
        "Read this alongside SOUL.md. SOUL describes what kind of bot you are; "
        "this guide describes what THIS specific bot is being used for and how "
        "the primary wants you to behave on the channels it's connected to.",
    ]
    if g.authored_by:
        lines.append(f"Authored by: {g.authored_by}")
    if audience:
        lines.append(f"Audience: {audience}")
    if tone:
        lines.append(f"Tone: {tone}")
    if isinstance(do_say, list) and do_say:
        lines.append("Do:")
        for item in do_say:
            lines.append(f"  - {item}")
    if isinstance(dont_say, list) and dont_say:
        lines.append("Don't:")
        for item in dont_say:
            lines.append(f"  - {item}")
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


def load_autonomy_block(bot_id: str | None, shared_dir: Path) -> str:
    """Per-integration autonomy guidance — the procedural enforcement
    surface of the autonomy ladder.

    Spec: docs/spec-autonomy-ladder-2026-06-10.md §2.3 (bot_guidance
    surface). Reads ``{shared_dir}/bots/<bot_id>/autonomy.json`` (the
    operator-intent file, same directory convention as the Slack
    policy/directory files) and renders the rung guidance for every
    operator-confirmed integration posture. Injected at session start
    so the instruction is guaranteed in context — no AGENTS.md write,
    no bot-owned file involved.

    Backfill-inferred postures are skipped: they describe observed
    state, not operator intent, and instructing a previously-free bot
    to start asking would be a behavior change nobody decided on
    (observe-first, spec §5.2).

    Soft-fails to "" on every error path — a missing or malformed
    posture file must never block session start.
    """
    if not bot_id:
        return ""
    try:
        from autonomy import catalog as _acat  # type: ignore[import-not-found]
        from autonomy import renderer as _arend  # type: ignore[import-not-found]
        from autonomy import store as _astore  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"[session_surface] WARNING: autonomy module unavailable ({e}) — "
            "autonomy block will not be injected",
            file=sys.stderr,
        )
        return ""
    try:
        doc = _astore.load(shared_dir, bot_id)
    except Exception as e:  # malformed file: warn, never block session start
        print(
            f"[session_surface] WARNING: autonomy posture load error for "
            f"{bot_id} ({e}) — autonomy block will not be injected",
            file=sys.stderr,
        )
        return ""
    if doc is None or not doc.integrations:
        return ""

    # Rung-3 daily-cap pauses (Phase B, spec §1.3): tell a paused bot it
    # is paused so the blocked tools read as the operator's limit, not a
    # malfunction to work around. Soft-fails to "not paused".
    try:
        from autonomy import limits as _alimits  # type: ignore[import-not-found]
        paused = _alimits.paused_integrations(shared_dir, bot_id)
    except Exception:  # noqa: BLE001 — derived state only
        paused = set()

    sections: list[str] = []
    for iid in sorted(doc.integrations.keys()):
        posture = doc.integrations[iid]
        try:
            if not _arend.posture_is_render_eligible(posture):
                continue
            spec = _acat.kind_spec(posture.kind)
            if spec is None:
                continue
            guidance = _acat.guidance_for(spec, posture.rung, posture.rules)
            if not guidance:
                continue
            binding = _acat.binding_for(iid)
            display = binding.display_name if binding else iid
            if iid in paused:
                guidance += (
                    "\nNOTE: you reached today's action limit here — "
                    "outward actions are paused until tomorrow (UTC). "
                    "Reading and drafting continue; do not retry blocked "
                    "tools today."
                )
            sections.append(f"For {spec.operator_noun} on {display}:\n{guidance}")
        except Exception:  # noqa: BLE001 — one bad entry can't kill the block
            continue
    if not sections:
        return ""
    return (
        "[AUTONOMY — operator-set limits on what you may do without asking]\n"
        "These are deliberate, operator-set boundaries. They are not "
        "suggestions; do not work around them or ask the user to widen "
        "them.\n\n" + "\n\n".join(sections)
    )


def load_app_posture_block(bot_id: str | None, shared_dir: Path) -> str:
    """Load the weekly app_posture/<bot>.md document and render it as a
    labeled system-prompt section.

    Spec: docs/spec-manifest-reflex.md §"App posture review (PR4)". The
    document is auto-generated weekly by app_posture_review.py with
    inventory data — apps changed this week, scanner-discovered apps,
    workspace orphans. PR5 will add an LLM reflection section on top of
    the same document.

    Returns "" if the doc is missing, empty, or the file is unreadable —
    session start must never fail because of a missing posture doc.

    The block has a hard cap (~3KB injected) so even the largest posture
    docs don't blow the system-prompt budget. Operators / Evolve devs
    reading the full posture should look at the on-disk doc, not what
    landed in the systemAppend.
    """
    if not bot_id:
        return ""
    doc = shared_dir / "app_posture" / f"{bot_id}.md"
    try:
        text = doc.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not text:
        return ""

    INJECTION_CAP = 3000
    if len(text) > INJECTION_CAP:
        # Truncate at a section boundary (## heading) where possible so we
        # don't cut mid-sentence. The renderer's section order goes from
        # most actionable (this-week changes, signals) to least
        # (inventory summary), so truncation drops the tail naturally.
        truncated = text[:INJECTION_CAP]
        last_section = truncated.rfind("\n## ")
        if last_section > 1000:  # only honor the section boundary if it leaves real content
            truncated = truncated[:last_section]
        text = truncated.rstrip() + "\n\n_(truncated for systemAppend; full doc at " + str(doc) + ")_"

    return (
        "[APP POSTURE — auto-generated weekly inventory of your apps]\n"
        "Read this alongside POD_CONDUCT §8 (manifest reflex). The posture "
        "below is what the system observed about your app inventory this "
        "week — use it to spot orphan files that should be folded into a "
        "manifest, apps you forgot to self-record, and patterns worth "
        "anticipating next time.\n\n"
        + text
    )


def load_slack_directory_block(bot_id: str | None, shared_dir: Path) -> str:
    """Slack workspace identity directory (the team_bot_a-2026-05-15 fix).

    Reads ``{shared_dir}/bots/<bot_id>/slack-directory.json`` (written
    by ``evolve_admin.integrations.slack.directory``) and renders a
    compact markdown table mapping every Slack user's full identity
    tuple — Slack ID, legacy ``name``, ``display_name``, ``real_name``,
    optional ``email``.

    Returns ``""`` (no injection) when:
    - bot_id is None
    - directory file is absent (bot isn't Slack-enabled, or directory
      hasn't been generated yet — operator runs
      ``evolve-admin slack-directory <bot> --refresh``)
    - directory exists but loads malformed (warn to stderr, return "")

    Length cap matches ``app_posture``: ~3KB injected. Larger
    workspaces get truncated with a tail note pointing to the
    on-disk file.

    Spec: team_bot_a-2026-05-15 incident — team_bot_a treated ``"pod_admin"`` (Slack
    legacy username) and ``"Pod_admin"`` (real name) and ``U0PLKKXV0``
    (Slack ID) as three different people. This injection makes the
    identity tuple available in the system prompt so the bot doesn't
    have to ask the user to disambiguate the same identifiers.
    """
    if not bot_id:
        return ""
    # Lazy import — analyzer should not require admin module at import
    # time. (analyzer is the smaller, leaner half of the codebase; we
    # only need this when a bot is Slack-enabled.)
    try:
        from evolve_admin.integrations.slack.directory import (
            load_directory,
            render_directory_markdown,
        )
    except ImportError as exc:
        print(
            f"[session_surface] slack-directory import unavailable ({exc}) — "
            "directory block will not be injected",
            file=sys.stderr,
        )
        return ""

    try:
        directory = load_directory(shared_dir, bot_id)
    except ValueError as exc:
        # Malformed file: warn loudly but don't fail session start.
        print(
            f"[session_surface] WARNING: slack-directory load error for "
            f"{bot_id} ({exc}) — directory block will not be injected",
            file=sys.stderr,
        )
        return ""
    if directory is None or not directory.users:
        return ""
    try:
        return render_directory_markdown(directory)
    except Exception as exc:  # render must not crash session_start
        print(
            f"[session_surface] WARNING: slack-directory render error for "
            f"{bot_id} ({exc})",
            file=sys.stderr,
        )
        return ""


def load_home_narrative_block(role: str | None, shared_dir: Path) -> str:
    """Inject the rendered Home-page report banner so evo can converse
    about its own report.

    The Evolve admin Home page renders a friendly LLM-generated summary
    of pod state at the top (the "evo report" banner). That summary is
    generated server-side and cached at
    ``{shared_dir}/home-narrative-cache.json`` — same file the
    ``/api/home/narrative`` endpoint serves the UI from. Without this
    block, evo sees the structured pod-state digest at chat time but
    not the narrative paragraph the admin is actually looking at, so
    questions like "what was that thing about Codex you mentioned in
    the report?" land with evo punting to "I can't see the banner."

    **Per-turn injection (2026-06-01).** Called from the per-turn
    ``--per-turn`` invocation, threaded into the LLM's system prompt
    via the plugin's ``before_prompt_build`` hook on every turn. That
    way a regenerated cache (operator refreshes Home mid-conversation,
    a new digest hash mints a fresh narrative) reaches the next turn
    instead of being stuck at the session_start snapshot. Failure modes
    2 and 3 from
    ``docs/diagnosis-evo-briefing-context-gap-2026-05-26.md`` —
    chat-session pre-dates the cache write, and cache regenerated
    between session_start and the chat turn — are structurally
    subsumed by per-turn injection.

    Gated on the primary role: the home narrative is admin-UI content
    aimed at the pod operator, so member bots get nothing here. Soft-
    fails to "" on every error path so a missing/malformed cache file
    never blocks the turn. Caps stale narratives — if the cached
    text is older than ``_HOME_NARRATIVE_MAX_AGE_S``, we drop it
    rather than inject something that no longer matches reality (the
    structured pod-state tools remain authoritative for live numbers);
    this is a guardrail against pages opened long ago with very stale
    narratives, not the primary freshness mechanism.
    """
    if not _is_primary(role):
        return ""
    cache_path = shared_dir / "home-narrative-cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    text = (payload.get("text") or "").strip()
    if not text:
        return ""
    generated_at_raw = (payload.get("generated_at") or "").strip()
    if generated_at_raw and _narrative_is_stale(generated_at_raw):
        return ""
    timestamp_line = (
        f"(Generated {generated_at_raw}. May be moments older than the "
        "live pod state — for current numbers, prefer the pod-state tools.)"
        if generated_at_raw
        else ""
    )
    parts = [
        "[CURRENT POD REPORT — shown to admin above this chat on the home page]",
        "This is the friendly summary the admin sees as a banner at the top of",
        "the Evolve admin home page right now. When admin references \"the",
        "report\", \"the banner\", or asks about something in it (\"what was that",
        "about Codex?\"), this is what they mean — answer from this text rather",
        "than punting.",
        "",
        text,
    ]
    if timestamp_line:
        parts.append("")
        parts.append(timestamp_line)
    return "\n".join(parts)


# Drop cached narratives older than this — better to inject nothing than to
# anchor evo on a hours-stale summary that no longer matches the visible
# banner (which re-fetches on the operator's next page visit).
_HOME_NARRATIVE_MAX_AGE_S = 6 * 60 * 60  # 6 hours


def _narrative_is_stale(generated_at_iso: str) -> bool:
    """True when the cache's ``generated_at`` is older than the max-age
    window. Unparseable timestamps fall through as not-stale — we'd
    rather inject the text than drop it on a date-format quirk."""
    try:
        from datetime import datetime, timezone

        # The cache writer emits ISO with a trailing "Z"; fromisoformat
        # only accepts "+00:00" pre-3.11, so normalize.
        normalized = generated_at_iso.replace("Z", "+00:00")
        ts = datetime.fromisoformat(normalized)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > _HOME_NARRATIVE_MAX_AGE_S
    except (ValueError, TypeError):
        return False


# ── Firing signals block ──────────────────────────────────────────────────────
#
# Sibling to load_home_narrative_block. The Haiku-generated home-page banner
# is one rendering of pod state; the Signal store IS the underlying state.
# Without this block, evo's chat-page session only sees the prose paragraph
# (when it's fresh and the session post-dates the cache write) — and the
# prose is deliberately summarized ("don't enumerate every signal — group
# them"), so even when present it omits the structured findings the operator
# may follow up on.
#
# Injecting the top firing signals at session_start lets chat-page evo
# ground "what does unpinned npm spec mean?" in the live audit finding,
# regardless of whether the briefing prose is in context.
#
# Spec: docs/diagnosis-evo-briefing-context-gap-2026-05-26.md (Option B1).

_FIRING_SIGNALS_TOP_N = 10
# Total render hard-cap. Comfortably above the typical render (10 short
# bullets ~ 600-900 bytes) but bounded so a pod with very long signal
# titles can't blow the system-prompt budget.
_FIRING_SIGNALS_RENDER_CAP_BYTES = 2048

# Higher numbers = higher priority for the top-N sort. Anything we don't
# recognize ranks below info — better to drop an oddball severity than
# promote it past real alerts.
_SEVERITY_RANK = {
    "alert": 3,
    "critical": 3,  # legacy / external producers may emit "critical"
    "warn": 2,
    "info": 1,
}

_SEVERITY_GLYPH = {
    "alert": "[!]",
    "critical": "[!]",
    "warn": "[~]",
    "info": "[.]",
}


def _signal_severity_rank(sig) -> int:
    """Rank a Signal by severity for top-N sort. Unknown severities = 0."""
    sev = getattr(sig, "severity", None)
    if not isinstance(sev, str):
        return 0
    return _SEVERITY_RANK.get(sev.strip().lower(), 0)


def _signal_recency_key(sig) -> str:
    """Sort key for recency — ISO timestamps sort lexicographically, so a
    raw string compare works. Missing/empty falls through as "" (oldest)."""
    last = getattr(sig, "last_observed_at", "") or ""
    return last if isinstance(last, str) else ""


def _signal_short_summary(sig) -> str:
    """Pick the most-informative human-readable line for one Signal.

    Prefers explicit summary fields (``details.headline`` / ``details.title``
    / ``title``) when present; falls back to the body's first line, then to
    the signature truncated. Always returns a single line — newlines collapse
    so a stray multi-line title can't blow the row format.
    """
    details = getattr(sig, "details", None) or {}
    candidates: list = []
    if isinstance(details, dict):
        candidates.append(details.get("headline"))
        candidates.append(details.get("title"))
    candidates.append(getattr(sig, "title", None))
    for cand in candidates:
        if isinstance(cand, str) and cand.strip():
            return _one_line(cand)
    body = getattr(sig, "body", None)
    if isinstance(body, str) and body.strip():
        # Body is operator-facing prose; take only the first non-empty line
        # so multi-paragraph bodies don't dominate the block.
        for line in body.splitlines():
            line = line.strip()
            if line:
                return _one_line(line)
    sig_sig = getattr(sig, "signature", None) or getattr(sig, "id", "")
    if not isinstance(sig_sig, str):
        sig_sig = ""
    return _one_line(sig_sig[:80]) or "(no summary)"


def _one_line(s: str) -> str:
    """Collapse to a single line and trim, so a stray newline can't
    desynchronize the bullet format."""
    return " ".join(s.split()).strip()


def load_firing_signals_block(role: str | None, shared_dir: Path) -> str:
    """Inject the top firing Signals as a parallel block to the home
    narrative. Primary-bot only.

    The home-narrative block is operator-facing prose; it can be stale,
    pre-dated by the session, or summarized past the underlying findings.
    The Signal store IS the live observation layer — every firing audit /
    mcp_admin / session_economics / cost_watchdog signal lives there, and
    a signal in ``firing/`` is by definition currently firing. No
    staleness gate.

    Top 10 by (severity, last_observed) so the block stays useful in
    pod-states with hundreds of firing signals; everything below the cut
    is still reachable via ``pod_state.signals.firing``. Grouped by
    bot_id when known; pod / host / integration-scoped signals go in an
    ungrouped section.

    Soft-fails to "" on every error path so a missing/malformed signal
    file never blocks session start.
    """
    if not _is_primary(role):
        return ""

    try:
        signals = _load_firing_signals_safe(shared_dir)
    except Exception as e:  # belt-and-suspenders — never crash session_start
        print(
            f"[session_surface] WARNING: firing-signals load error ({e}) — "
            "block will not be injected",
            file=sys.stderr,
        )
        return ""

    if not signals:
        return ""

    total = len(signals)
    # Sort: severity rank desc, then last_observed desc (most-recent first).
    signals.sort(
        key=lambda s: (_signal_severity_rank(s), _signal_recency_key(s)),
        reverse=True,
    )
    top = signals[:_FIRING_SIGNALS_TOP_N]
    shown = len(top)

    return _format_firing_signals_block(top, total=total, shown=shown)


def _load_firing_signals_safe(shared_dir: Path) -> list:
    """Read every firing Signal under ``{shared_dir}/signals/firing/``.

    Malformed files are already skipped silently by ``load_signal_file``
    inside the store. Returns an empty list when the dir doesn't exist
    or when the signals.store module isn't importable (e.g. an analyzer-
    only deploy or a partial install).
    """
    # Lazy import — keeps session_surface light when the signals module
    # isn't on the path (eg unrelated analyzer-only tests).
    try:
        from signals.store import iter_active  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"[session_surface] WARNING: signals.store unavailable ({e}) — "
            "firing-signals block will not be injected",
            file=sys.stderr,
        )
        return []
    try:
        return list(iter_active(shared_dir, state="firing"))
    except Exception as e:
        # iter_active itself swallows per-file decode errors via
        # load_signal_file. A failure here means the dir walk or filter
        # itself blew up — log and return empty so session_start still
        # succeeds.
        print(
            f"[session_surface] WARNING: firing-signals iter error ({e}) — "
            "block will not be injected",
            file=sys.stderr,
        )
        return []


def _format_firing_signals_block(
    signals: list, *, total: int, shown: int
) -> str:
    """Render the top-N signals as a labeled block, grouped by bot.

    Format::

        [FIRING SIGNALS — live Signal-store snapshot at session start; ...]
        <bot_id>:
          [!] <bot_id>: short summary
          [~] <bot_id>: short summary
        <other bot_id>:
          ...
        Pod-wide / host / integration:
          [!] pod: short summary
        (N signals total; top X shown by severity. ...)

    Bot-scoped signals (with ``bot_id``) get grouped under their bot;
    pod / host / integration scope signals go in an ungrouped section
    at the end.
    """
    by_bot: dict[str, list] = {}
    ungrouped: list = []
    for sig in signals:
        bot_id = getattr(sig, "bot_id", None)
        if isinstance(bot_id, str) and bot_id.strip():
            by_bot.setdefault(bot_id, []).append(sig)
        else:
            ungrouped.append(sig)

    lines = [
        "[FIRING SIGNALS — live Signal-store snapshot at session start; "
        "use pod_state.signals.firing for an update mid-session]",
    ]

    for bot_id in sorted(by_bot.keys()):
        lines.append(f"{bot_id}:")
        for sig in by_bot[bot_id]:
            lines.append(f"  {_signal_bullet(sig)}")

    if ungrouped:
        lines.append("Pod-wide / host / integration:")
        for sig in ungrouped:
            lines.append(f"  {_signal_bullet(sig)}")

    if total > shown:
        lines.append(
            f"({total} signals total; top {shown} shown by severity. "
            "For full list call pod_state.signals.firing.)"
        )
    else:
        lines.append(
            f"({total} signal{'s' if total != 1 else ''} total. "
            "For details call pod_state.signals.firing.)"
        )

    rendered = "\n".join(lines)
    # Hard-cap the render so a pathological pod (very long titles, many
    # signals) can't blow the system-prompt budget. Truncate at a line
    # boundary so we never cut a bullet in the middle.
    if len(rendered.encode("utf-8")) > _FIRING_SIGNALS_RENDER_CAP_BYTES:
        rendered = _truncate_at_line_boundary(
            rendered, _FIRING_SIGNALS_RENDER_CAP_BYTES
        )
    return rendered


def _signal_bullet(sig) -> str:
    """Format one signal as a single bullet line."""
    sev = getattr(sig, "severity", None)
    sev_str = sev.strip().lower() if isinstance(sev, str) else ""
    glyph = _SEVERITY_GLYPH.get(sev_str, "[.]")
    bot_id = getattr(sig, "bot_id", None)
    scope = getattr(sig, "scope", None)
    if isinstance(bot_id, str) and bot_id.strip():
        prefix = bot_id
    elif isinstance(scope, str) and scope.strip():
        prefix = scope
    else:
        prefix = "pod"
    summary = _signal_short_summary(sig)
    return f"{glyph} {prefix}: {summary}"


def _truncate_at_line_boundary(text: str, cap_bytes: int) -> str:
    """Trim ``text`` so its UTF-8 encoding fits in ``cap_bytes``, at the
    last full-line boundary that fits. Appends a one-line truncation
    notice. Falls through unmodified if the text already fits."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    tail = "\n(...truncated; call pod_state.signals.firing for the full list.)"
    tail_bytes = len(tail.encode("utf-8"))
    budget = max(cap_bytes - tail_bytes, 0)
    # Take prefix bytes, then back off to the last newline so we don't
    # split mid-line (or mid-codepoint).
    head = encoded[:budget]
    last_nl = head.rfind(b"\n")
    if last_nl > 0:
        head = head[:last_nl]
    return head.decode("utf-8", errors="ignore") + tail


def _is_primary(role: str | None) -> bool:
    """Case-insensitive role check. The TS side normalizes too
    (`packages/plugin/src/config.ts:resolveConfig`); doing it again here
    is belt-and-suspenders — a hand-edited `openclaw.json` with a typoed
    role would otherwise silently strip the primary-bot scaffold."""
    return isinstance(role, str) and role.strip().lower() == "primary"


def load_primary_block(role: str | None) -> str:
    """Anti-hallucination scaffolding for the primary bot.

    Spec: docs/spec-primary-bot-interface-2026-05-14.md §7. Tells the
    LLM to call the help-retrieval and pod-state read tools instead of
    guessing about Evolve internals.

    Returns "" for non-primary bots (member bots are unaffected by this
    feature) and for unset roles (legacy pods).
    """
    if not _is_primary(role):
        return ""
    return _PRIMARY_BLOCK


def load_member_block(role: str | None) -> str:
    """Standing instruction for non-primary bots about the ``evo`` keyword.

    The Evolve plugin intercepts ``evo`` / ``evo <subcommand>`` keywords
    in the gateway before the LLM sees them, runs the command via the
    admin API, and direct-sends the response over the channel transport.
    The bot's LLM is supposed to stay silent — the per-turn ``stay-quiet``
    directive from before_prompt_build asks for a single period.

    That directive only loads when the plugin successfully intercepts.
    When it doesn't (transport hiccup, unrecognized envelope, a future
    routing bug), the LLM is left with the bare ``evo`` message and no
    guidance — and tends to fabricate a recovery path (running
    ``python3 …/session_surface.py``, offering to disable exec policy,
    etc.). This block is the standing instruction that prevents that
    fallback regardless of whether the per-turn directive arrives.

    Returns "" only for the primary bot (which has its own block at
    ``_PRIMARY_BLOCK``). Returns the member block for "member", unknown
    roles, and unset roles — those are all non-primary cases and the
    advice is safe across all of them.
    """
    if _is_primary(role):
        return ""
    return _MEMBER_BLOCK


_MEMBER_BLOCK = (
    "[EVOLVE PLUGIN — `evo` keyword]\n"
    "The `evo` keyword (bare `evo`, and `evo <subcommand>` like `evo help`,\n"
    "`evo wizard`, `evo better`, etc.) is handled by the Evolve plugin\n"
    "running in this bot's gateway. The plugin intercepts the keyword,\n"
    "runs the command via the admin API, and replies to the user directly\n"
    "via the channel transport (Telegram Bot API, Slack API, etc.) — the\n"
    "delivered message is framed by `═══ evo ═══` markers.\n"
    "\n"
    "You do not handle `evo`. You do not investigate what it means, look\n"
    "up its docs, run Python or shell scripts, or read files under\n"
    "`/Users/Shared/evolve`, `/Users/Shared/evolve-repo`, or the analyzer\n"
    "and admin Python packages — those are plugin infrastructure, not\n"
    "commands you should invoke. In particular, do NOT try to run\n"
    "`python3 …/session_surface.py` or `python3 …/apply.py` — those are\n"
    "scripts the plugin and admin server run themselves.\n"
    "\n"
    "If a per-turn directive from the plugin tells you to emit a single\n"
    "period (`.`), follow it exactly and make zero tool calls — the\n"
    "plugin has already delivered the user-visible reply.\n"
    "\n"
    "If the plugin's intercept failed for some reason and you see a\n"
    "bare `evo` message with no per-turn directive, do not attempt to\n"
    "handle it yourself. Reply briefly (one short line) that you saw\n"
    "the keyword and suggest the user try again. Never substitute your\n"
    "own investigation.\n"
    "\n"
    "If any command is blocked by a gateway security policy\n"
    "(`exec.security`, sandbox restrictions, etc.), state that it was\n"
    "blocked and stop. Never offer to disable, weaken, or route around a\n"
    "security policy to make a command work — that is a stop sign, not a\n"
    "problem to solve."
)


_PRIMARY_BLOCK = (
    "[PRIMARY BOT — Evolve interface]\n"
    "You are this pod's primary bot. Admin (the human pod sysadmin) talks\n"
    "to you for two things:\n"
    "\n"
    "  1. Operational questions about Evolve — how something works, what\n"
    "     state the pod is in, why something fired.\n"
    "  2. Filing bugs and feature requests against Evolve itself.\n"
    "\n"
    "For (1):\n"
    "  - For \"what is X / how do I do Y\" questions: call `evolve_help_search`\n"
    "    first. If a doc matches, ground your answer in it. If nothing\n"
    "    matches closely, say \"I don't have a doc on that — try `evo help`\n"
    "    for commands, or the admin UI.\" Never invent file paths, command\n"
    "    flags, or behaviors.\n"
    "  - For \"what's the pod doing / show me X\" questions: prefer the\n"
    "    matching `evo <sub>` command and tell admin to run it. Do not\n"
    "    synthesize numbers from prose.\n"
    "  - For \"why did X happen\" questions: combine — point admin at the\n"
    "    relevant `evo <sub>` for the live state, then use\n"
    "    `evolve_help_search` for grounded context.\n"
    "\n"
    "For (2):\n"
    "  - If admin says \"filing a bug,\" \"feature request,\" or describes a\n"
    "    misbehavior, confirm intent (\"Want me to file this as a bug?\"),\n"
    "    then call `submit_intake` with kind=bug|feature and the body.\n"
    "    Echo the intake id back.\n"
    "  - If admin explicitly says \"post this to GitHub\" or \"file this as\n"
    "    an issue,\" confirm once (\"Posting to GitHub — confirm? (transcript\n"
    "    will be stripped)\"), then call `submit_intake` with promote=true.\n"
    "    Echo the GitHub URL back on success.\n"
    "\n"
    "Style: short header, one fact per line, conversational close-out.\n"
    "Never label findings as \"CRITICAL\" / \"Security\" / \"P0\" unless they\n"
    "actually came from a security-warden signal. Plain verbs only — admin\n"
    "is technical but doesn't speak Evolve internals.\n"
    "\n"
    "If you don't know, say so. Don't guess."
)


# Hard cap for the help sidebar so even a large help corpus can't blow the
# system-prompt budget. ~30 lines / ~400 tokens per spec §4.2.
_HELP_SIDEBAR_DOC_CAP = 24


def load_help_sidebar_block(role: str | None, shared_dir: Path) -> str:
    """Render a short table-of-contents sidebar listing the help docs the
    primary bot can ground answers from. Generated from the on-disk help
    index, so it stays in sync as docs are added.

    Spec: docs/spec-primary-bot-interface-2026-05-14.md §4.2.

    Returns "" for non-primary bots, and "" when the index is missing /
    unreadable. The bot is instructed (via load_primary_block) to fall
    back to ``evo help`` / admin UI in that case — silently dropping the
    sidebar is safer than injecting a malformed one.
    """
    if not _is_primary(role):
        return ""

    index = _load_help_index_silently(shared_dir)
    if index is None or not index.docs:
        return ""

    # Group docs by category in spec order, then list ``doc_id — title``
    # for each. Caps total entries to keep the block bounded.
    category_order = ("help", "operator", "applications")
    grouped: dict[str, list] = {c: [] for c in category_order}
    for doc in index.docs:
        bucket = doc.category if doc.category in grouped else "help"
        grouped[bucket].append(doc)

    lines = [
        "[EVOLVE HELP DOCS — for admin questions about this pod]",
        "Use the `evolve_help_search` tool before answering admin questions",
        "about how Evolve works. If no doc matches closely, say so — never",
        "invent file paths, flags, or behaviors.",
        "",
    ]
    shown = 0
    category_labels = {
        "help": "Topics",
        "operator": "Operator docs",
        "applications": "App framework",
    }
    for cat in category_order:
        docs = grouped[cat]
        if not docs:
            continue
        if shown >= _HELP_SIDEBAR_DOC_CAP:
            break
        lines.append(f"{category_labels[cat]}:")
        shown_in_cat = 0
        for doc in docs:
            if shown >= _HELP_SIDEBAR_DOC_CAP:
                # Remaining count is per-category, not running total —
                # the running total mixed across categories used to leak
                # a wrong number here.
                lines.append(f"  …and {len(docs) - shown_in_cat} more")
                break
            title = (doc.title or doc.doc_id).strip()
            lines.append(f"  - {doc.doc_id} — {title}")
            shown += 1
            shown_in_cat += 1
        lines.append("")

    lines.append(
        "For live-state queries (current spend, firing alerts, recent audits,"
    )
    lines.append(
        "…) tell admin to run the matching `evo <sub>` rather than synthesizing."
    )
    return "\n".join(lines).rstrip()


def _load_help_index_silently(shared_dir: Path):
    """Load the help index, returning None on any failure. Kept private
    so the import gymnastics don't leak into callers' tracebacks."""
    try:
        from evolve_admin.help_index import build as _hi  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"[session_surface] WARNING: help_index module unavailable ({e}) — "
            "sidebar will not be injected",
            file=sys.stderr,
        )
        return None
    try:
        return _hi.load_index(shared_dir)
    except Exception as e:
        print(
            f"[session_surface] WARNING: help index load error ({e}) — "
            "sidebar will not be injected",
            file=sys.stderr,
        )
        return None


def load_app_repair_prompt_block(bot_id: str | None, role: str | None) -> str:
    """Channel-aware app-repair SYSTEM block.

    Built from :mod:`analyzer.app_repair_prompt`. Audience/channel
    awareness is conservative at session_start: we know the bot_id and
    role, but not which channel the user will arrive on. The default
    audience is the bot's primary user (the most common in-situ
    repair-conversation participant); the default channel is "other"
    which yields neutral language ("via this channel") that's
    accurate in every transport.

    Returns "" if no bot_id (the prompt is bot-specific because the
    apply path inlines `--on-behalf-of <bot_id>`).
    """
    if not bot_id:
        return ""
    try:
        from app_repair_prompt import (  # type: ignore[import-not-found]
            AppRepairPromptContext,
            build_app_repair_system_prompt,
        )
    except ImportError as e:
        print(
            f"[session_surface] WARNING: app_repair_prompt module unavailable "
            f"({e}) — repair prompt block will not be injected",
            file=sys.stderr,
        )
        return ""
    audience: str = "primary_user"
    if isinstance(role, str) and role.strip().lower() == "primary":
        # Primary bot's user IS the pod operator in personal-bot setups.
        # Calling them "pod_operator" matches the audience field that
        # arbiter Proposals use, and lines up with the admin-UI route
        # if/when a primary-bot conversation moves to that surface.
        audience = "pod_operator"
    ctx = AppRepairPromptContext(
        bot_id=bot_id,
        channel="other",
        audience=audience,  # type: ignore[arg-type]
    )
    return build_app_repair_system_prompt(ctx)


def load_app_findings_block(bot_id: str | None, shared_dir: Path) -> str:
    """Coherence + reconciliation findings on this bot's apps.

    Spec: docs/spec-app-coherence-and-reconciliation-2026-06-05.md §10.9.

    Reads ``manifest.coherence.findings[]`` + ``manifest.reconciliation``
    from each manifest on this bot (Tier 2 runner writes these), maps to
    ``FindingForBlock`` entries, and renders via ``build_apps_block()``
    so the bot can mention drift / coherence findings conversationally.

    Loud failures here would break session_start, so this function never
    raises — returns empty string on any error.
    """
    try:
        # Bot reads its own manifests; same path the Tier 2 runner writes.
        manifests_dir = Path.home() / ".openclaw" / "workspace" / "manifests"
        if not manifests_dir.exists():
            return ""

        # Lazy-import the build_apps_block helper from evolve_admin.
        from evolve_admin.applications.session_surface_apps import (  # type: ignore
            FindingForBlock, build_apps_block,
        )

        findings_for_block: list = []
        for path in sorted(manifests_dir.glob("*.json")):
            if path.name.startswith("."):
                continue   # skip .scan-status.json
            try:
                manifest = json.loads(path.read_text())
            except Exception:
                continue
            app_id = manifest.get("id") or path.stem

            # Coherence findings (Pass A's ``id`` field + Pass C1's ``check_id``).
            for f in (manifest.get("coherence") or {}).get("findings", []):
                severity = (f.get("severity") or "info").lower()
                if severity == "info":
                    continue   # info is noise for session-start
                msg = (f.get("description") or f.get("message") or "").strip()
                if not msg:
                    continue
                # Truncate long messages so the block stays bounded.
                if len(msg) > 120:
                    msg = msg[:117] + "..."
                check_id = f.get("id") or f.get("check_id") or ""
                findings_for_block.append(FindingForBlock(
                    app_id=app_id,
                    severity=severity,
                    summary=msg,
                    signature=f.get("signature") or "",
                    repair_hint=(
                        f"Repair via `evo app-repair {app_id}`."
                        if severity in ("critical", "major") else ""
                    ),
                ))

            # Confirmed orphan = a separate kind of finding worth mentioning.
            rec = manifest.get("reconciliation") or {}
            if rec.get("status") == "orphan":
                findings_for_block.append(FindingForBlock(
                    app_id=app_id, severity="major",
                    summary="all infrastructure files are missing; app may have been retired",
                    repair_hint=(
                        f"Reinstall via `evo install {app_id}` or "
                        f"archive via `evo apps archive {app_id}`."
                    ),
                ))

        if not findings_for_block:
            return ""
        block = build_apps_block(findings_for_block)
        if not block:
            return ""
        return block + "\n\n" + _APP_FINDINGS_DEEP_DIVE_FOOTER
    except Exception:
        # session_start must not fail. Soft-empty on any unexpected error.
        return ""


# Deep-dive footer appended whenever load_app_findings_block has any findings
# to surface. Tells the bot where the structured detail lives on disk + which
# fields to read for the per-finding context the summary line truncates. Used
# for in-situ repair conversations (Telegram, Slack) where the bot needs to
# go from "journal has 2 findings" to "C-A1 critical: cron is missing" without
# the admin server packaging context for it. Spec: §10.9.
_APP_FINDINGS_DEEP_DIVE_FOOTER = (
    "For details on any finding: read\n"
    "  ~/.openclaw/workspace/manifests/<app>.json\n"
    "and inspect:\n"
    "  .coherence.findings[]               — each finding (id, severity, "
    "assertion, description, evidence)\n"
    "  .reconciliation                     — drift state (added_files, "
    "removed_files, drifted_fields, status)\n"
    "  .coherence.last_capability_check    — Pass C3 verdict\n"
    "  .last_audit                         — most recent Tier 3 run summary\n"
    "  .audit_eligible                     — whether structural audit applies\n"
    "\n"
    "See the [COHERENCE VOCAB] block earlier in this prompt for assertion ids "
    "(C-A1…C-A8, C1-1…C1-4), severity meanings, and reconciliation status "
    "values."
)


def _load_network_for_capabilities(shared_dir: Path) -> dict | None:
    """Read ``{shared_dir}/network.json`` as a plain dict for capability
    detection. Returns ``None`` on any error.

    Plain read — not ``evolve_config.load_config`` — because that path
    auto-migrates and writes the file back, which session_surface (running
    as the bot, not evolve) must never do. ``network.json`` lives under the
    world-readable ``shared_dir`` tree (``a+rX``), so the bot can read it;
    only the ``google_integration`` block is consulted downstream.
    """
    try:
        payload = json.loads((shared_dir / "network.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_capabilities_block(bot_id: str | None, shared_dir: Path) -> str:
    """Build the ``[INSTALLED CAPABILITIES]`` push block for this bot.

    Spec: docs/spec-bot-capability-awareness-2026-06-22.md §3 (PUSH). Lists
    the **skills** and **configured-integration tools** (Google + future) the
    bot ACTUALLY has — each with name, purpose, example triggers, and an
    explicit "use this; don't improvise" — so the agent stops confabulating
    tools it doesn't have. Applies to EVERY bot (not role-gated): the
    motivating confabulation was a member/consumer bot.

    Sources:
      * skills      — ``~/.openclaw/skills/*/SKILL.md`` (bot reads its own)
      * integration — ``network.json`` → provider registry (Google today)

    Apps are deliberately NOT included here: they are already delivered
    durably via the bot's ``AGENTS.md`` Installed-Apps section (the apps
    precedent) + ``INSTALLED_APPS.md``. This block fills the gap the spec
    names — "skills are injected nowhere, integration tools injected
    nowhere" — and stays small enough to ship on EVERY turn (it is injected
    per-turn via the plugin's ``before_prompt_build`` hook, not just at
    session_start; see ``--capabilities-only`` below and the migration note
    on ``main()``).

    Returns ``""`` when the bot has no skills/tools (gate on count > 0) and
    soft-fails to ``""`` on every error path — a missing/malformed source
    must never block a turn. Capped via the renderer.
    """
    if not bot_id:
        return ""
    try:
        from capability_block import (  # type: ignore[import-not-found]
            build_capabilities_block,
        )
    except ImportError as e:
        print(
            f"[session_surface] WARNING: capability_block unavailable ({e}) — "
            "capability block will not be injected",
            file=sys.stderr,
        )
        return ""

    # The bot reads its OWN skills dir; session_surface runs as the bot.
    skills_dir = Path.home() / ".openclaw" / "skills"
    network = _load_network_for_capabilities(shared_dir)

    try:
        # app_items omitted on purpose — apps ship via AGENTS.md, not here.
        return build_capabilities_block(
            bot_id,
            skills_dir=skills_dir,
            network=network,
        )
    except Exception as e:  # noqa: BLE001 — belt-and-suspenders
        print(
            f"[session_surface] WARNING: capability block assembly error for "
            f"{bot_id} ({e}) — block will not be injected",
            file=sys.stderr,
        )
        return ""


def build_session_prefix(
    guide_block: str = "",
    notifications_block: str = "",
    app_posture_block: str = "",
    app_findings_block: str = "",
    app_repair_prompt_block: str = "",
    primary_block: str = "",
    help_sidebar_block: str = "",
    member_block: str = "",
    slack_directory_block: str = "",
    home_narrative_block: str = "",
    firing_signals_block: str = "",
    # New parameters land at the END: existing callers pass the first
    # three positionally (pinned by test_backwards_compatible_positional_call)
    # and an insertion would silently re-bind them.
    autonomy_block: str = "",
    capabilities_block: str = "",
) -> str:
    """
    Build the full system-prompt prefix for session start.

    Always includes the pod conduct summary; appends the bot guide when
    present, then app posture (auto-generated weekly), then the role-
    specific scaffold (primary block + help sidebar for the primary bot;
    member block for every other bot), then notifications (forge build
    done / failed / etc.).

    Order: conduct (rules of the road) → runtime notes (platform facts
    like exec idioms) → guide (this bot's mission per its primary) → app
    posture (what's in your app inventory this week) →
    role scaffold (primary block + help sidebar OR member block; never both
    — they're mutually exclusive by role) → home narrative (primary-only: the
    operator-facing report prose the admin sees on the home page right
    now; **note** main() no longer passes this at session_start as of
    2026-06-01 — it's emitted on every turn via the ``--per-turn`` mode
    instead, so a regenerated cache reaches the next turn; the parameter
    stays here for backwards-compat with callers like tests that still
    exercise the in-prefix ordering) → firing signals (primary-only:
    structured live snapshot of the underlying Signal store; parallel to
    the narrative, grounds follow-ups in live data when the prose is
    stale, sparse, or not in context) → notifications (things that
    landed since last session that often need attention up-front).

    Continuity v1's "pending approval tasks" block was removed when the
    old extractor/runner stack was deleted — under Continuity v2 the bot
    schedules its own follow-ups via the `defer` tool, so there's no
    operator-approval queue to surface at session start.
    """
    parts = [POD_CONDUCT_SUMMARY]
    if RUNTIME_NOTES:
        # Runtime notes land right after conduct: both are pod-wide
        # standing rules. Conduct = how to behave; runtime notes = facts
        # about the platform you're running on (exec preflight idioms,
        # tool quirks, etc.). Soft-empty when the file is missing.
        parts.append(RUNTIME_NOTES)
    if COHERENCE_VOCAB:
        # Coherence vocab lands right after runtime notes: another pod-
        # wide standing reference, scoped to the assertion-id vocabulary
        # the bot's app findings use. Sits ABOVE the per-bot
        # app_findings block so the bot can already interpret a `C-A1`
        # reference by the time it reads the block listing findings.
        parts.append(COHERENCE_VOCAB)
    if guide_block:
        parts.append(guide_block)
    if autonomy_block:
        # Operator-set autonomy limits land right after the bot's
        # mission guide: both are "how this bot is being used here"
        # context, and the limits must be read before any tool use —
        # ahead of the role scaffolds and per-session state blocks.
        parts.append(autonomy_block)
    if slack_directory_block:
        # Surface the workspace directory before the role scaffolds —
        # identity confusion (team_bot_a-2026-05-15) is something the bot
        # needs to read EARLY, before it starts processing the first
        # user message. After POD_CONDUCT + the bot's mission guide,
        # before the role-specific scaffolds.
        parts.append(slack_directory_block)
    if app_posture_block:
        parts.append(app_posture_block)
    if app_findings_block:
        # App findings (coherence + reconciliation drift) right after
        # the posture (which lists the app inventory). Order: inventory
        # → findings about that inventory → role scaffolds. Keeps the
        # "what apps you have / what's wrong with them" pair together.
        parts.append(app_findings_block)
    if app_repair_prompt_block:
        # Bot-side repair-conversation scaffolding (Channel B). Lands
        # after the findings block so the bot has the live findings
        # AND the apply-path instructions paired, and before the role
        # scaffolds (which are about Evolve identity, not app repair).
        # In-situ Telegram/Slack/Discord conversations route through
        # this block.
        parts.append(app_repair_prompt_block)
    if capabilities_block:
        # The [INSTALLED CAPABILITIES] push block — skills + configured-
        # integration tools the bot actually has. **Note**: main() no longer
        # passes this at session_start as of 2026-06-22 — it ships on every
        # turn via the plugin's before_prompt_build hook
        # (``--capabilities-only`` mode) instead, because session_start fires
        # ONCE per OC session and never re-fires for existing long-running
        # Telegram chats (CA-P1 shipped non-functional for exactly that
        # reason — see docs/spec-bot-capability-awareness §5 P1). The
        # parameter stays here for backwards-compat with callers/tests that
        # exercise the in-prefix ordering. When present it lands after the
        # apps cluster (posture/findings), before the role scaffolds. Spec:
        # bot-capability-awareness §3 (PUSH).
        parts.append(capabilities_block)
    if primary_block:
        parts.append(primary_block)
    if help_sidebar_block:
        parts.append(help_sidebar_block)
    if member_block:
        parts.append(member_block)
    if home_narrative_block:
        # Land after the role scaffolds (which define behaviour) but
        # before notifications (which describe things that landed since
        # last session). The narrative is what's happening NOW from the
        # admin's view; pairing it with notifications keeps the
        # "current world state" content grouped at the end of the prefix.
        parts.append(home_narrative_block)
    if firing_signals_block:
        # Parallel to the narrative: operator-facing prose first
        # (when present), then the structured live snapshot. The two
        # answer different operator follow-ups — the prose covers
        # "what did you say?", the signal list covers "where is this
        # firing? / which bots?". Both primary-only.
        parts.append(firing_signals_block)
    if notifications_block:
        parts.append(notifications_block)
    return "\n\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Surface pod conduct, guide, and notifications at session start")
    parser.add_argument("--bot", help="Bot ID to load the per-bot guide for")
    parser.add_argument("--role", help="Bot role (primary|member); gates the primary-bot blocks")
    parser.add_argument("--user-key", dest="user_key",
                        help="Per-user identity (e.g. ext:telegram:12345); "
                             "enables notification queue surfacing")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output JSON instead of text")
    parser.add_argument("--quiet", action="store_true", help="(Legacy flag — kept for backwards compat; always exits 0 now)")
    parser.add_argument("--shared-dir", help="Override shared directory path")
    parser.add_argument(
        "--per-turn",
        action="store_true",
        dest="per_turn",
        help=(
            "Per-turn invocation: emit ONLY the per-turn-eligible blocks "
            "(today: the home-narrative block). Called from the plugin's "
            "before_prompt_build hook on every turn so a regenerated "
            "narrative cache reaches the LLM the next turn. Without this "
            "flag the script emits the session_start prefix (conduct + "
            "role scaffold + firing signals + …) and SKIPS the narrative "
            "block — it now ships exclusively via the per-turn path."
        ),
    )
    parser.add_argument(
        "--capabilities-only",
        action="store_true",
        dest="capabilities_only",
        help=(
            "Emit ONLY the [INSTALLED CAPABILITIES] block (skills + "
            "configured-integration tools). Called from the plugin's "
            "before_prompt_build hook (TTL-cached) so the block reaches "
            "EVERY turn — including long-running Telegram sessions, which "
            "session_start never re-fires for. Applies to every bot (not "
            "role-gated). Empty output (exit 0) when the bot has no "
            "skills/tools."
        ),
    )
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir) if args.shared_dir else _shared_dir()

    if not shared_dir.exists():
        if args.quiet:
            sys.exit(0)
        print(f"[session_surface] Shared dir not found: {shared_dir}", file=sys.stderr)
        sys.exit(2)

    # ── Per-turn mode ─────────────────────────────────────────────────────────
    # Emit only the per-turn-eligible blocks so the plugin's
    # before_prompt_build hook can thread the current narrative into
    # the LLM's system prompt on every turn. Today this is just the
    # home-narrative block; if other content moves from session_start
    # to per-turn in the future, add it here (same primary-role gating).
    if args.per_turn:
        narrative = load_home_narrative_block(args.role, shared_dir)
        if args.as_json:
            print(json.dumps({"home_narrative": narrative}))
        else:
            if narrative:
                print(narrative)
        sys.exit(0)

    # ── Capabilities-only mode ────────────────────────────────────────────────
    # Emit ONLY the [INSTALLED CAPABILITIES] block. The plugin invokes this
    # from before_prompt_build (TTL-cached) so the block reaches EVERY turn,
    # including existing long-running Telegram sessions that session_start
    # never re-fires for — the delivery gap that left CA-P1 non-functional.
    # Every bot, not role-gated; empty output when the bot has no skills/tools.
    if args.capabilities_only:
        block = load_capabilities_block(args.bot, shared_dir)
        if args.as_json:
            print(json.dumps({"capabilities": block}))
        else:
            if block:
                print(block)
        sys.exit(0)

    # ── Session-start mode (default) ──────────────────────────────────────────
    guide_block = load_bot_guide_block(args.bot, shared_dir)
    autonomy_block = load_autonomy_block(args.bot, shared_dir)
    app_posture_block = load_app_posture_block(args.bot, shared_dir)
    app_findings_block = load_app_findings_block(args.bot, shared_dir)
    app_repair_prompt_block = load_app_repair_prompt_block(args.bot, args.role)
    primary_block = load_primary_block(args.role)
    help_sidebar_block = load_help_sidebar_block(args.role, shared_dir)
    member_block = load_member_block(args.role)
    notifications_block = load_notifications_block(args.user_key, shared_dir)
    slack_directory_block = load_slack_directory_block(args.bot, shared_dir)
    # Capabilities block is NOT loaded here. As of 2026-06-22 it ships via
    # the per-turn ``--capabilities-only`` invocation (before_prompt_build
    # hook, TTL-cached) so it reaches EVERY turn — including existing
    # long-running Telegram sessions, which session_start fires for only
    # ONCE per OC session and never re-fires for. Injecting it here too
    # would double up on fresh sessions; the per-turn path is authoritative.
    # (Same migration the home narrative made on 2026-06-01.)
    # Home narrative is NOT loaded here. As of 2026-06-01 it ships via
    # the per-turn ``--per-turn`` invocation (before_prompt_build hook)
    # so a cache regenerated mid-conversation reaches the LLM on the
    # very next turn. Loading + injecting it here would race against
    # the per-turn injection without adding value — and the old
    # session_start-only path is exactly the source of failure modes 2
    # and 3 in the briefing-context-gap diagnosis (2026-05-26).
    firing_signals_block = load_firing_signals_block(args.role, shared_dir)

    if args.as_json:
        print(json.dumps({
            "conduct": POD_CONDUCT_SUMMARY,
            "runtime_notes": RUNTIME_NOTES,
            "coherence_vocab": COHERENCE_VOCAB,
            "guide": guide_block,
            "autonomy": autonomy_block,
            "slack_directory": slack_directory_block,
            "app_posture": app_posture_block,
            "app_findings": app_findings_block,
            "app_repair_prompt": app_repair_prompt_block,
            "primary": primary_block,
            "help_sidebar": help_sidebar_block,
            "member": member_block,
            # capabilities + home_narrative are intentionally omitted from
            # session_start JSON output — capabilities ship via
            # --capabilities-only and the narrative via --per-turn. Tests
            # that exercise the in-prefix ordering still pass these blocks
            # to build_session_prefix() directly.
            "firing_signals": firing_signals_block,
            "notifications": notifications_block,
        }))
    else:
        print(build_session_prefix(
            guide_block=guide_block,
            autonomy_block=autonomy_block,
            notifications_block=notifications_block,
            app_posture_block=app_posture_block,
            app_findings_block=app_findings_block,
            app_repair_prompt_block=app_repair_prompt_block,
            # capabilities_block intentionally omitted — per-turn now (above)
            primary_block=primary_block,
            help_sidebar_block=help_sidebar_block,
            member_block=member_block,
            slack_directory_block=slack_directory_block,
            # home_narrative_block intentionally omitted — see above
            firing_signals_block=firing_signals_block,
        ))

    sys.exit(0)  # always exit 0 — we always have content (conduct summary)


if __name__ == "__main__":
    main()
