"""agent_bypass_audit — Signal producer for app-routed responses bypassed by general tools.

Why this exists
---------------

The 2026-06-05 live test of ``atlas-on-demand-research`` in a Telegram
group exposed a class gap (see
``internal/spec-agent-freelance-bypass-2026-06-05.md``): when ``bot_guidance``
in an app's manifest tells the LLM agent to invoke a bot-local script in
response to a triggering message, and the invocation fails for any
reason (OC exec preflight, missing file, etc.), the agent silently falls
back to its general tools (Brave Search, native LLM reasoning) and
**answers freelance**. None of the script's controls run — scope gates,
source grounding, opt-out honor, per-sender rate limit, privacy log.

``PR #2192`` added a per-app instruction ("do NOT freelance with general
tools if the script fails") to ``bot_guidance`` for the two affected
atlas apps. ``POD_CONDUCT.md`` rule 11 (added 2026-06-05) generalises
that instruction pod-wide. Both are *policy*, not enforcement — an
instruction-following LLM under pressure can still violate them.

This daemon closes the proactive *visibility* gap: every day, it walks
recent session transcripts for bots that have an at-risk app installed,
counts trigger-pattern messages that did NOT cause the declared script
to be invoked, and emits one Signal per (bot, app) when a meaningful
number of those bypass candidates exist.

The Phase 2 enforcement layer (subagent narrowing, install-time
validator) is tracked separately in the spec.

What it does
------------

Once per day (04:35 UTC, after ``reconcile_audit`` at 04:30, ordered to
avoid bunching with other daemons that share the daily-sweep window):

  1. For each at-risk app in :data:`AT_RISK_APPS`, enumerate bots whose
     installed manifests include that app. Read manifests via
     ``evolve_admin.applications.manifest.list_manifests`` (per-bot,
     ``/Users/<bot>/.openclaw/workspace/manifests/``).

  2. For each (bot, app) pair, walk the bot's session transcripts whose
     activity falls within the lookback window. Sessions are discovered
     via the bot's ``turns-<date>.jsonl`` records (cheap), then read from
     the OC session-transcript paths (``home/.openclaw/agents/main/agent/
     sessions/<id>/transcript.jsonl`` + fallbacks — same probe pattern as
     ``turn_detail.py``).

  3. For each transcript, count
     :class:`SessionAuditResult` shape:

       - ``triggers_seen``    — user messages matching the app's trigger pattern
       - ``script_invoked``   — assistant ``tool_use`` blocks whose exec
         command references the app's script basename

     A session is a **bypass candidate** if ``triggers_seen >
     script_invoked`` — at least one triggering message did not produce
     a script invocation.

  4. Per (bot, app): if bypass candidates exist over the window, emit a
     Signal with signature
     ``agent_bypass_audit:agent_bypass:{bot_id}:{app_id}``. Body cites
     the count and an example excerpt.

  5. ``signals.store.sweep_resolve()`` clears any prior Signals for
     (bot, app) pairs whose bypass count over the current window is
     zero.

Producer:  ``agent_bypass_audit``
Signal:    ``agent_bypass`` (severity=warn, flavor=behavior)
Signature: ``agent_bypass_audit:agent_bypass:{bot_id}:{app_id}``
Scope:     pod (one Signal per (bot, app); operator triages each)
Severity:  warn — behavioral integrity is operationally important but
           not page-the-operator urgent. Backstop work (Phase 2) closes
           the gap structurally.

Pure Python, no LLM. Runs as the ``evolve`` user (which has ACL read on
``/Users/<bot>/.openclaw/`` per CLAUDE.md). Daily cadence.

V1 heuristic notes
------------------

As of Phase 2.2 of the spec
(internal/spec-agent-freelance-bypass-phase2-2026-06-06.md), the audit
discovers at-risk apps by walking each installed manifest's
``event_triggers[]`` block. Each trigger contributes a
``(pattern, exclude_pattern, channel)`` rule to a per-(manifest, script)
predicate; the audit treats a manifest as covered whenever the rule
fires on user text in a session where the declared script wasn't
invoked.

For manifests that haven't migrated yet (no structured
``event_triggers``), the audit falls back to the hardcoded
:data:`AT_RISK_APPS` catalogue. This preserves detection of the two
known v1 offenders during the rollout window:

  - ``atlas-on-demand-research`` (``atlas_research.py``)
  - ``atlas-article-capture`` (``atlas_capture.py``)

The hardcoded catalogue is **deprecated** — once gallery audit shows
zero unmigrated at-risk manifests, it can be removed.

Session-scoped detection is intentionally coarse: a session with one
script invocation and N trigger messages counts as one bypass candidate
(N-1 triggers unaccounted for is suspicious; surfacing it is the point).
A finer turn-scoped pass is left for a future iteration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from evolve_admin.applications.app_identity import (  # pyright: ignore[reportMissingImports]
    APP_ID_RESOLUTION_ORDER,
    resolve_app_id,
)
from evolve_config import bot_home as _bot_home
from schema.signal import make_signature
from signals import store as signals_store


PRODUCER = "agent_bypass_audit"
SIGNAL_TYPE = "agent_bypass"

# Default window. 24h matches the daily cadence — every run covers the
# previous day. Wider windows would surface lingering bypass histories
# even after the operator migrated the app; sweep_resolve handles
# clearing once the window comes up clean.
DEFAULT_LOOKBACK_HOURS = 24

# Cap session-transcript I/O per (bot, app). A bot under sustained noise
# can produce hundreds of sessions per day; reading every transcript on a
# slow disk would bloat the daemon's runtime. We sort by activity recency
# (newest first) so the cap drops the oldest, least-relevant sessions
# first. Realistic per-day session volume for the atlas bot is < 100; the
# cap exists for the runaway case.
MAX_SESSIONS_PER_BOT_APP = 500


# ─────────────────────────────────────────────────────────────────────────────
# At-risk app catalogue
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AtRiskApp:
    """Static descriptor for one app whose bot_guidance routes a chat
    trigger through a bot-local script.

    Phase 2 of the spec promotes this into a structured ``triggers[]``
    block on the manifest. Until then, the catalogue is maintained here.
    """
    app_id: str
    # Basename of the script the agent is supposed to invoke. Match
    # against tool_use commands by substring — paths vary (absolute /
    # relative / quoted) but the basename is stable.
    script_basename: str
    # Per-channel trigger detectors. Each callable takes (user_text,
    # channel_kind) and returns True if the message should have routed
    # to the script. Channel kind is lowercased ("telegram", "slack",
    # "discord", "dm", "group", "unknown"). When we can't tell, default
    # to broad-match.
    trigger_predicate: "TriggerPredicate"


# ── Detector functions ──────────────────────────────────────────────────────


# Telegram username mentions like @atlas_evo_bot. Atlas's specific
# handle is configurable per-pod via network.json, but the @-token shape
# is stable; if any token in the user message begins with `@` and the
# rest is a plausible bot handle, treat as @-mention.
_AT_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_]{2,}\b")

_ASK_PREFIX_RE = re.compile(r"^\s*/ask\b", re.IGNORECASE)
_OPTOUT_PREFIX_RE = re.compile(r"^\s*/optout(?:-all)?\b", re.IGNORECASE)

# URLs in body text. Permissive (any http/https), bounded so we don't
# blow up on garbage. Matches the atlas-capture trigger surface.
_URL_RE = re.compile(r"\bhttps?://\S{4,2000}")


def _atlas_research_trigger(text: str, channel: str) -> bool:
    """True when this user message should have routed to atlas_research.

    Triggers per the app's bot_guidance:
      (a) @-mention in group, OR
      (b) ``/ask <question>`` prefix in group, OR
      (c) any DM that isn't an ``/optout*`` slash command.
    """
    if not text:
        return False
    if _OPTOUT_PREFIX_RE.match(text):
        return False
    if _ASK_PREFIX_RE.match(text):
        return True
    if _AT_MENTION_RE.search(text):
        return True
    if channel in ("dm", "telegram_dm"):
        return True
    return False


def _atlas_capture_trigger(text: str, channel: str) -> bool:
    """True when this user message should have routed to atlas_capture.

    Triggers per the app's bot_guidance:
      (a) any group message containing one or more URLs, OR
      (b) an ``/optout <URL>`` or ``/optout-all`` slash command.

    DM URLs do NOT trigger capture — atlas-capture is group-only.
    """
    if not text:
        return False
    if _OPTOUT_PREFIX_RE.match(text):
        return True
    if channel in ("dm", "telegram_dm"):
        return False
    return bool(_URL_RE.search(text))


# Type alias for the trigger callable (declared after the functions to
# satisfy the dataclass forward ref).
TriggerPredicate = "Callable[[str, str], bool]"  # noqa: F821 — documentation alias


AT_RISK_APPS: tuple[AtRiskApp, ...] = (
    AtRiskApp(
        app_id="atlas-on-demand-research",
        script_basename="atlas_research.py",
        trigger_predicate=_atlas_research_trigger,
    ),
    AtRiskApp(
        app_id="atlas-article-capture",
        script_basename="atlas_capture.py",
        trigger_predicate=_atlas_capture_trigger,
    ),
)


# Map app_id → AtRiskApp for O(1) lookup when iterating installed
# manifests. We accept either the manifest's ``id`` (``app_atlas_*``)
# or ``pkg_id`` style ids — different layers use different shapes; the
# canonical match is on the manifest's spec id, but we also fall back
# to display_name keywords.
#
# identity: see resolve_app_id — the catalogue keys are hand-written app
# names, not resolved ids, so this map is matched against rather than looked
# up by identity. ``_match_at_risk_app`` explains why that stays a loop.
_BY_APP_ID: dict[str, AtRiskApp] = {a.app_id: a for a in AT_RISK_APPS}


def _match_at_risk_app(manifest_meta: dict) -> AtRiskApp | None:
    """Map an installed-manifest dict to its at-risk catalogue entry, if any.

    identity: see ``applications.app_identity.resolve_app_id`` — NOT a
    resolution. This is a permissive MATCHER: it tries every id-shaped field
    against the hardcoded catalogue (and a ``_``→``-`` normalisation of each)
    because the catalogue was written against whichever shape a given layer
    happened to emit. A resolver returns one string and would stop at the
    first non-empty field, so a manifest whose ``pkg_id`` is a ``p-<hex>``
    that is not in the catalogue would stop matching on its ``id`` — silently
    dropping the app out of the bypass audit. Keep the loop.
    """
    # Prefer pkg_id-style ids when present.
    for key in ("pkg_id", "app_id", "id"):
        val = manifest_meta.get(key) or ""
        for app in AT_RISK_APPS:
            if val == app.app_id:
                return app
            # Manifest ``id`` is sometimes ``app_atlas_on_demand_research``;
            # accept a normalised match too.
            normalised = val.replace("_", "-").replace("app-", "")
            if normalised == app.app_id:
                return app
    # Fallback: substring of display_name when the id is ambiguous.
    name = (manifest_meta.get("display_name") or "").lower()
    if "on-demand research" in name or "on demand research" in name:
        return _BY_APP_ID["atlas-on-demand-research"]
    if "article capture" in name:
        return _BY_APP_ID["atlas-article-capture"]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Transcript probe (mirrors turn_detail.py's strategy)
# ─────────────────────────────────────────────────────────────────────────────


def _shared_turns_paths(shared_dir: Path, bot_id: str, window_dates: list[date]) -> list[Path]:
    """Shared turns files for the lookback window."""
    return [
        shared_dir / bot_id / "turns" / f"turns-{d.isoformat()}.jsonl"
        for d in window_dates
    ]


def _read_jsonl(path: Path) -> Iterable[dict]:
    """Yield dict records from a JSONL file. Skip malformed lines silently."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _session_ids_in_window(
    shared_dir: Path,
    bot_id: str,
    *,
    cutoff: datetime,
    window_dates: list[date],
) -> list[str]:
    """Return session ids with activity strictly newer than ``cutoff``.

    Reads from the shared turns dir (TurnObserver.writeTurnToShared
    writes here for every agent_end). Falls back to the bot's
    workspace turn-collector path if the shared file is empty.
    """
    seen: dict[str, str] = {}  # session_id → latest ts seen (for ordering)
    for path in _shared_turns_paths(shared_dir, bot_id, window_dates):
        for rec in _read_jsonl(path):
            sid = rec.get("session_id")
            ts = rec.get("ts") or ""
            if not sid or not isinstance(sid, str):
                continue
            try:
                rec_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if rec_dt < cutoff:
                continue
            prior = seen.get(sid)
            if prior is None or ts > prior:
                seen[sid] = ts
    # Newest-first; cap at MAX_SESSIONS_PER_BOT_APP. The cap is per
    # bot-app and applies after this point — we return all and let the
    # caller cap, so multi-app iteration shares the same cap budget
    # implicitly via the same input set.
    return sorted(seen.keys(), key=lambda s: seen[s], reverse=True)


def _transcript_probes(home: Path, session_id: str) -> list[Path]:
    """Candidate transcript paths in priority order. Mirrors turn_detail.py."""
    return [
        home / ".openclaw" / "agents" / "main" / "agent" / "sessions" / session_id / "transcript.jsonl",
        home / ".openclaw" / "agents" / "main" / "agent" / "sessions" / f"{session_id}.jsonl",
        home / ".openclaw" / "sessions" / session_id / "transcript.jsonl",
        home / ".openclaw" / "sessions" / f"{session_id}.jsonl",
        home / ".openclaw" / "workspace" / "sessions" / session_id / "transcript.jsonl",
    ]


def _read_transcript(home: Path, session_id: str) -> list[dict]:
    """Return parsed transcript records for ``session_id``, or [] on miss.

    Tries JSONL first (the common shape); falls back to a single JSON
    document with a ``messages`` array. Records that don't parse as
    dicts are silently dropped.
    """
    for path in _transcript_probes(home, session_id):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        records: list[dict] = []
        # JSONL fast-path
        is_jsonl = True
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                is_jsonl = False
                records = []
                break
            if isinstance(rec, dict):
                records.append(rec)

        if not is_jsonl or not records:
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict) and isinstance(doc.get("messages"), list):
                records = [m for m in doc["messages"] if isinstance(m, dict)]
            elif isinstance(doc, list):
                records = [m for m in doc if isinstance(m, dict)]

        if records:
            return records
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Message + tool-use extraction
# ─────────────────────────────────────────────────────────────────────────────


def _text_of_content(content) -> str:
    """Flatten a message ``content`` field into a string.

    Anthropic-style content can be a string or an array of
    typed blocks. We concatenate all ``type == "text"`` blocks; non-text
    blocks (tool_use, tool_result, image) contribute nothing to the user
    text channel.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        return " ".join(chunks)
    return ""


def _is_user_message(rec: dict) -> bool:
    return rec.get("role") == "user"


def _is_assistant_message(rec: dict) -> bool:
    return rec.get("role") == "assistant"


def _tool_use_blocks(rec: dict) -> list[dict]:
    """Return ``tool_use`` content blocks in an assistant message."""
    content = rec.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _tool_use_command_text(block: dict) -> str:
    """Render a tool_use block's input as a single command string for substring search.

    Tool inputs vary per tool (bash gets a ``command``; write gets a
    ``file_path``; etc.). We don't care which tool — only whether the
    script basename appears anywhere in the serialised input. JSON-dumping
    the whole thing is robust to schema changes.
    """
    inp = block.get("input")
    try:
        return json.dumps(inp, default=str)
    except (TypeError, ValueError):
        return str(inp)


# ─────────────────────────────────────────────────────────────────────────────
# Per-session audit
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SessionAuditResult:
    session_id: str
    triggers_seen: int = 0
    script_invoked: int = 0
    example_trigger_text: str = ""

    @property
    def is_bypass(self) -> bool:
        return self.triggers_seen > self.script_invoked


def _audit_one_session(
    records: list[dict],
    app: AtRiskApp,
    channel_hint: str,
) -> SessionAuditResult:
    """Walk a transcript's records and produce per-session counts."""
    result = SessionAuditResult(session_id="")  # caller sets id
    for rec in records:
        if _is_user_message(rec):
            text = _text_of_content(rec.get("content"))
            if app.trigger_predicate(text, channel_hint):
                result.triggers_seen += 1
                if not result.example_trigger_text:
                    result.example_trigger_text = text[:240]
        elif _is_assistant_message(rec):
            for block in _tool_use_blocks(rec):
                cmd = _tool_use_command_text(block)
                if app.script_basename in cmd:
                    result.script_invoked += 1
    return result


@dataclass
class BotAppAuditTotals:
    bot_id: str
    app_id: str
    sessions_audited: int = 0
    bypass_sessions: int = 0
    triggers_total: int = 0
    invocations_total: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.bypass_sessions == 0


# ─────────────────────────────────────────────────────────────────────────────
# Installed-manifest enumeration
# ─────────────────────────────────────────────────────────────────────────────


def _make_manifest_trigger_predicate(
    triggers: list[dict],
) -> "TriggerPredicate":
    """Build a (text, channel) predicate from a list of event_triggers items.

    Each trigger contributes one ``(pattern, exclude_pattern, channel)``
    rule. The predicate fires when ANY rule matches: channel constraint
    satisfied, exclude_pattern doesn't match, pattern does match.

    Channel matching: the manifest declares precise channels
    (``telegram_dm``, ``telegram_group``, etc.); the audit's channel
    hint at this writing is coarser (``telegram``, ``dm``, ``group``,
    ``unknown``). Match conservatively — if either side is unknown, OR
    if the manifest channel is ``any``, OR if the hint substring-matches
    the manifest channel, accept. Overcounting bypasses is preferable
    to missing them.
    """
    compiled: list[tuple] = []
    for trig in triggers:
        if not isinstance(trig, dict):
            continue
        match = trig.get("match") or {}
        pattern_str = match.get("pattern")
        if not pattern_str:
            continue
        try:
            pat = re.compile(pattern_str)
        except re.error:
            continue
        excl = None
        excl_str = match.get("exclude_pattern")
        if excl_str:
            try:
                excl = re.compile(excl_str)
            except re.error:
                pass
        channel = (match.get("channel") or "any").lower()
        compiled.append((pat, excl, channel))

    def predicate(text: str, channel_hint: str) -> bool:
        if not text:
            return False
        hint = (channel_hint or "unknown").lower()
        for pat, excl, channel in compiled:
            if channel != "any" and hint not in ("", "unknown"):
                # Liberal substring match: hint "telegram" passes channel
                # "telegram_group" or "telegram_dm"; hint "group" passes
                # "telegram_group". Direction matters — manifest channel
                # contains hint, OR hint contains manifest channel parts.
                hint_in_channel = hint in channel
                channel_parts_in_hint = any(
                    p in hint for p in channel.split("_") if p
                )
                if not (hint_in_channel or channel_parts_in_hint):
                    continue
            if excl and excl.search(text):
                continue
            if pat.search(text):
                return True
        return False

    return predicate


def _build_at_risk_apps_from_manifest(manifest: Any) -> list[AtRiskApp]:
    """Synthesize AtRiskApp entries from a manifest's event_triggers[].

    Returns one AtRiskApp per distinct ``invocation.script`` basename
    declared on the manifest. Multiple triggers calling the same script
    collapse into one AtRiskApp with a union predicate.

    Returns [] when the manifest declares no usable triggers (no
    invocation.script and no pattern).
    """
    triggers_raw = getattr(manifest, "event_triggers", None) or []
    if not isinstance(triggers_raw, list):
        return []

    # Group by script basename.
    by_basename: dict[str, list[dict]] = {}
    for trig in triggers_raw:
        if not isinstance(trig, dict):
            continue
        invocation = trig.get("invocation") or {}
        script = invocation.get("script") or ""
        if not script:
            continue
        match = trig.get("match") or {}
        if not match.get("pattern"):
            continue
        basename = script.rsplit("/", 1)[-1]
        by_basename.setdefault(basename, []).append(trig)

    if not by_basename:
        return []

    # Use the manifest's stable identifier in the synthesized app_id so
    # the audit's per-(bot, app) Signal signature stays stable across
    # script renames on the same manifest.
    #
    # AL-1.4b: identity via the ONE resolver. ``manifest`` is an
    # ``ApplicationManifest`` dataclass, not a dict, so the fields the
    # resolver reads are projected onto one first — ``resolve_app_id`` is
    # dict-only by design (it must answer identically for the raw JSON the
    # plugin sees). The projection is the full canonical order, so the
    # previous ``pkg_id or id`` chain is a prefix of it and answers the same
    # for every manifest that carried either field. ``display_name`` stays as
    # the last-resort label below: it is not an identity, but this signature
    # has always preferred a human string over "unknown".
    manifest_id = resolve_app_id({
        k: getattr(manifest, k, "") or "" for k in APP_ID_RESOLUTION_ORDER
    }) or getattr(manifest, "display_name", None) or "unknown"

    out: list[AtRiskApp] = []
    for basename, group in by_basename.items():
        out.append(AtRiskApp(
            app_id=manifest_id,
            script_basename=basename,
            trigger_predicate=_make_manifest_trigger_predicate(group),
        ))
    return out


def _installed_at_risk_apps_for_bot(bot_id: str) -> list[AtRiskApp]:
    """Return at-risk apps installed on ``bot_id``.

    Discovery is manifest-driven where possible (the Phase 2 path) and
    falls back to the hardcoded :data:`AT_RISK_APPS` catalogue for any
    manifest that doesn't yet declare ``event_triggers[]`` with structured
    invocation contract.

    Uses ``evolve_admin.applications.manifest.list_manifests`` which is
    the canonical reader and resolves the per-bot manifests directory
    via ``get_bot_workspace`` (handles bot_id vs macOS user mismatches).
    Failure paths return [] — the daemon should never crash because one
    bot's manifests dir is malformed.
    """
    try:
        from evolve_admin.applications.manifest import list_manifests
    except ImportError:
        return []
    try:
        manifests = list_manifests(Path("/Users/Shared/evolve"), bot_id)
    except Exception:  # noqa: BLE001 — defensive against any reader failure
        return []

    matched: list[AtRiskApp] = []
    for m in manifests:
        # Phase 2 path: prefer structured event_triggers when declared.
        manifest_derived = _build_at_risk_apps_from_manifest(m)
        if manifest_derived:
            matched.extend(manifest_derived)
            continue

        # Legacy fallback: ApplicationManifest exposes canonical id-style
        # fields; check against the hardcoded catalogue.
        #
        # identity: see resolve_app_id — this builds the INPUT to the
        # permissive matcher above, so it must carry every candidate field
        # rather than one resolved id. See ``_match_at_risk_app``'s docstring.
        meta: dict = {}
        for k in ("pkg_id", "app_id", "id", "display_name"):
            v = getattr(m, k, None)
            if v is not None:
                meta[k] = v
        legacy_app = _match_at_risk_app(meta)
        if legacy_app is not None:
            matched.append(legacy_app)
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# Channel hinting
# ─────────────────────────────────────────────────────────────────────────────


def _infer_channel_hint(turn_record: dict) -> str:
    """Best-effort channel kind for a session, used to gate DM-only triggers."""
    ch = turn_record.get("channel")
    if not isinstance(ch, str):
        return "unknown"
    ch_l = ch.lower()
    # Heuristic: when source/channel suggests cron/heartbeat, treat as
    # non-chat — those won't have trigger patterns to begin with.
    if ch_l in ("cron", "heartbeat", "subagent"):
        return ch_l
    # We don't have a reliable DM vs group hint in the turn record;
    # default to broad-match (treat as group). The trigger predicates
    # already handle this: atlas_research treats DM-or-group both as
    # triggers; atlas_capture suppresses DM URLs via a fallback rule
    # the OC transcript doesn't expose. Until Phase 2 adds reliable
    # channel.kind to the turn record, "unknown" is the conservative
    # default — overcount on capture in DM is preferable to missing
    # all capture bypasses in groups.
    return ch_l or "unknown"


def _channel_hint_for_session(
    shared_dir: Path, bot_id: str, session_id: str, window_dates: list[date]
) -> str:
    """Return the most recent channel hint we can find for this session."""
    for path in _shared_turns_paths(shared_dir, bot_id, window_dates):
        for rec in _read_jsonl(path):
            if rec.get("session_id") == session_id:
                hint = _infer_channel_hint(rec)
                if hint not in ("", "unknown"):
                    return hint
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Per-(bot, app) collection
# ─────────────────────────────────────────────────────────────────────────────


def _window_dates(now: datetime, lookback_hours: int) -> list[date]:
    """Calendar dates that overlap the lookback window, ordered oldest→newest."""
    cutoff = now - timedelta(hours=lookback_hours)
    days: list[date] = []
    d = cutoff.date()
    end = now.date()
    while d <= end:
        days.append(d)
        d = d + timedelta(days=1)
    return days


def _audit_bot_app(
    shared_dir: Path,
    bot_id: str,
    network: dict,
    app: AtRiskApp,
    *,
    now: datetime,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> BotAppAuditTotals:
    """Run the audit for one (bot, app) pair over the lookback window."""
    totals = BotAppAuditTotals(bot_id=bot_id, app_id=app.app_id)
    cutoff = now - timedelta(hours=lookback_hours)
    dates = _window_dates(now, lookback_hours)

    session_ids = _session_ids_in_window(
        shared_dir, bot_id, cutoff=cutoff, window_dates=dates,
    )
    if not session_ids:
        return totals

    # Cap before reading transcripts so the daemon's runtime is bounded
    # even when a bot has thousands of sessions in 24h.
    session_ids = session_ids[:MAX_SESSIONS_PER_BOT_APP]

    home = _bot_home(bot_id, network)

    for sid in session_ids:
        records = _read_transcript(home, sid)
        if not records:
            # Either transcript dir not present, or the OC version
            # uses a layout we don't probe. Counts as "audited but
            # no data" so we don't false-positive on a probe miss.
            continue
        channel_hint = _channel_hint_for_session(shared_dir, bot_id, sid, dates)
        per_sess = _audit_one_session(records, app, channel_hint)
        per_sess.session_id = sid

        totals.sessions_audited += 1
        totals.triggers_total += per_sess.triggers_seen
        totals.invocations_total += per_sess.script_invoked
        if per_sess.is_bypass:
            totals.bypass_sessions += 1
            if per_sess.example_trigger_text and len(totals.examples) < 3:
                totals.examples.append(
                    f"session {sid[:8]}: {per_sess.triggers_seen} trigger(s), "
                    f"{per_sess.script_invoked} invocation(s) — "
                    f"e.g. {per_sess.example_trigger_text!r}"
                )
    return totals


# ─────────────────────────────────────────────────────────────────────────────
# Signal spec builder
# ─────────────────────────────────────────────────────────────────────────────


def _spec_for_bypass(totals: BotAppAuditTotals, lookback_hours: int) -> dict:
    """Render a Signal spec for one (bot, app) with bypass candidates."""
    signature = make_signature(
        PRODUCER, SIGNAL_TYPE,
        scope_key=f"{totals.bot_id}:{totals.app_id}",
    )

    title = (
        f"{totals.app_id} on {totals.bot_id}: "
        f"{totals.bypass_sessions} session(s) with trigger but no script invocation"
    )

    body_lines = [
        f"**{totals.app_id}** on bot **{totals.bot_id}** had "
        f"**{totals.bypass_sessions} session(s)** in the last "
        f"{lookback_hours}h where a triggering message arrived but the "
        f"declared script was not invoked. The agent likely answered "
        f"freelance using its general tools, bypassing the script's "
        f"controls (scope gates, source grounding, opt-out honor, rate limit).",
        "",
        f"Window totals: {totals.triggers_total} trigger(s) seen, "
        f"{totals.invocations_total} script invocation(s) across "
        f"{totals.sessions_audited} session(s) audited.",
        "",
    ]
    if totals.examples:
        body_lines.append("Example sessions:")
        for ex in totals.examples:
            body_lines.append(f"  - {ex}")
        body_lines.append("")

    body_lines.append(
        "See `internal/spec-agent-freelance-bypass-2026-06-05.md` for the "
        "underlying gap and the phased remediation plan. PR #2192 added "
        "the per-app JSON-request-file workaround for the OC exec preflight "
        "failure that motivated the spec; this Signal surfaces the residual "
        "bypass surface after the workaround landed."
    )

    return dict(
        signature=signature,
        producer=PRODUCER,
        type=SIGNAL_TYPE,
        flavor="behavior",
        severity="warn",
        scope="pod",
        title=title,
        body="\n".join(body_lines),
        details={
            "bot_id":             totals.bot_id,
            "app_id":             totals.app_id,
            "lookback_hours":     lookback_hours,
            "sessions_audited":   totals.sessions_audited,
            "bypass_sessions":    totals.bypass_sessions,
            "triggers_total":     totals.triggers_total,
            "invocations_total":  totals.invocations_total,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def collect(
    shared_dir: Path,
    *,
    network: dict | None = None,
    now: datetime | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> list[dict]:
    """Walk every bot, return Signal specs for (bot, app) with bypass candidates."""
    if network is None:
        try:
            network = json.loads(
                (shared_dir / "network.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            network = {}

    if now is None:
        now = datetime.now(timezone.utc)

    bots = (network or {}).get("bots") or {}
    specs: list[dict] = []
    for bot_id in bots:
        installed = _installed_at_risk_apps_for_bot(bot_id)
        if not installed:
            continue
        for app in installed:
            totals = _audit_bot_app(
                shared_dir,
                bot_id,
                network or {},
                app,
                now=now,
                lookback_hours=lookback_hours,
            )
            if totals.bypass_sessions > 0:
                specs.append(_spec_for_bypass(totals, lookback_hours))
    return specs


def run(
    shared_dir: Path,
    *,
    dry_run: bool = False,
    network: dict | None = None,
    now: datetime | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> tuple[set[str], int, int]:
    """Collect specs, write Signals, sweep-resolve cleared (bot, app) pairs.

    Returns ``(kept_signatures, n_fired, n_resolved)`` — same shape as
    every other Signal-producing monitor under packages/analyzer/.
    """
    specs = collect(
        shared_dir,
        network=network,
        now=now,
        lookback_hours=lookback_hours,
    )
    kept: set[str] = set()
    n_fired = 0
    for spec in specs:
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{PRODUCER}] observe failed for {spec['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: no bypass candidates in current window",
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{PRODUCER}] sweep_resolve failed: {exc}",
                flush=True,
            )
    return kept, n_fired, n_resolved


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"{PRODUCER} — daily Signal producer for app-routed responses "
            "bypassed by agent freelance."
        ),
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path("/Users/Shared/evolve"),
        help="Pod-wide shared dir (default: /Users/Shared/evolve).",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Audit window in hours (default: {DEFAULT_LOOKBACK_HOURS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Signal specs instead of writing them.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="No-op flag for compatibility with the standard launchd invocation.",
    )
    args = parser.parse_args(argv)

    kept, n_fired, n_resolved = run(
        args.shared_dir,
        dry_run=args.dry_run,
        lookback_hours=args.lookback_hours,
    )
    print(
        f"[{PRODUCER}] fired={n_fired} resolved={n_resolved} kept={len(kept)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
