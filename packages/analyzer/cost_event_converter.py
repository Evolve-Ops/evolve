#!/usr/bin/env python3
"""
cost_event_converter.py — Convert OC turn-collector records into cost_event records.

Replaces the broken llm_output → cost_event emission path. OC 2026.4.29's
embedded runner does not invoke `llm_output` on plugin handlers (the
plugin's `api.on("llm_output", ...)` registration is accepted but never
fires on channel-driven turns), so the plugin's CostLedger writer has been
silent for ~12 days across the pod.

The data we need is, however, present on disk: turn-collector.py
(/Users/Shared/openclaw-usage/turn-collector.py — runs daily for some bots)
parses each bot's session JSONLs and gateway log into per-turn records at
``/Users/<bot>/.openclaw/workspace/memory/turns-<date>.jsonl``. Schema:

    {
        "ts": ISO,                              # turn end time
        "instance": "admin_bot",
        "channel": "telegram"|"slack"|"cron"|"subagent"|"unknown",
        "user_id": str,
        "model": "anthropic/claude-sonnet-4-6", # provider/model
        "source": "human"|"cron"|"subagent",
        "auth_mode": "token"|"api_key"|"unknown",
        "auth_note": str,
        "cost": 0.045,                          # USD
        "input_tokens": int,
        "output_tokens": int,
        "cache_read_tokens": int,
        "cache_write_tokens": int,
        "session_id": uuid
    }

This script reads those records and writes the canonical ``cost_event``
schema (see packages/plugin/src/observer/CostLedger.ts) to a sibling file
in the shared annotations dir:

    {sharedDir}/annotations/{bot}/cost_events-{date}.jsonl

`observations.access.cost_events()` reads both this new path and the
legacy ``{date}.jsonl`` file (filtering by ``type == cost_event``), so
historical data emitted by the now-deleted plugin path remains readable.

Idempotent: dedup by (ts, session_id) — re-runs on the same source
produce no duplicate records. Safe to run on a 15-min cadence.

Per-bot job — runs as the bot user via launchd
(``ai.openclaw.evolve.cost-converter.<bot>``). The bot user has read
access to its own workspace/memory and creates output files owned by
itself in the shared annotations dir.

Usage:
    python3 cost_event_converter.py --bot-id admin_bot
    python3 cost_event_converter.py --bot-id admin_bot --date 2026-04-22
    python3 cost_event_converter.py --bot-id admin_bot --backfill 14
    python3 cost_event_converter.py --bot-id admin_bot --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from evolve_config import bot_home as _bot_home
import forge_sessions


# Schema v2 adds: timestamp_local, user_id, user_display_name, channel_id,
# channel_kind. All optional / nullable. Old v1 records (no new fields)
# still parse cleanly via observations.access.cost_events() — readers must
# treat absent values as None and not assume the key exists. See
# packages/plugin/src/observer/CostLedger.ts for the canonical schema doc.
COST_EVENT_SCHEMA_VERSION = 2

# Default fallback when the pod's network.json doesn't carry a timezone.
# Mirrors measure.py's DEFAULT_TZ_NAME — keep in sync.
_DEFAULT_TZ_NAME = "America/Los_Angeles"

# Default backfill window when --backfill is given without a value.
DEFAULT_BACKFILL_DAYS = 14


# ─────────────────────────────────────────────────────────────────────────────
# Schema mapping (turn-collector → cost_event)
# ─────────────────────────────────────────────────────────────────────────────


def _split_provider_model(model_string: str) -> tuple[str, str]:
    """Split 'anthropic/claude-sonnet-4-6' → ('anthropic', 'claude-sonnet-4-6').

    Falls back to ('unknown', model_string) when the prefix is absent.
    Real-world records always carry the provider prefix; the fallback
    mostly protects against malformed lines.
    """
    if not model_string or "/" not in model_string:
        return ("unknown", model_string or "unknown")
    provider, model = model_string.split("/", 1)
    return (provider, model)


def _infer_trigger_kind(source: str | None, channel: str | None) -> str:
    """Map turn-collector's source/channel to the cost_event trigger_kind enum.

    cost_event accepts: user_turn | heartbeat | cron_app | subagent |
    summarizer | classifier | task_extractor | fallback | unknown.

    turn-collector populates ``source`` ∈ {user, heartbeat, cron, subagent}
    (note: ``user`` is normalised to ``human`` upstream by
    ``_normalize_evolve_turn`` for shared-turns input). ``channel`` carries
    the surface (telegram / slack / discord / webchat / heartbeat /
    cron-event / exec-event / unknown). The mapping favours the source
    field since it's the most direct classification.

    History note: the previous mapping collapsed cron→heartbeat and
    subagent→unknown, and missed source=heartbeat entirely (fell through
    to unknown). After fixing those, ~95% of security_bot's events that were
    previously tagged "unknown" land in their proper buckets.
    """
    src = (source or "").lower()
    ch = (channel or "").lower()
    if src == "human":
        return "user_turn"
    if src == "heartbeat":
        return "heartbeat"
    if src == "cron":
        return "cron_app"
    if src == "subagent":
        return "subagent"
    if src == "forge":
        # Set by the forge_sessions retag pass — distinguishes Forge
        # build/critique/refine dispatches (which would otherwise fall
        # under user_turn) from real human input.
        return "forge"
    if src in {"summarizer", "classifier", "task_extractor", "fallback"}:
        # Evolve's own subagent analysis calls. OC's plugin-subagent lane
        # never fires agent_end, so TurnObserver captures these at
        # llm_output and writes the shared-turn record with source set to
        # the canonical trigger_kind directly (tag propagated from the
        # runPinnedSubagent idempotencyKey — see subagentRun.ts). Pass
        # through verbatim: these are the kinds context_health --overhead's
        # EVOLVE_TRIGGER_KINDS bucket bills as Evolve overhead (Phase A2).
        return src
    # Channel-based fallback — for records where source is missing/unknown
    # but the channel still tells us where the call came from.
    if ch == "heartbeat":
        return "heartbeat"
    if ch == "cron-event" or ch == "cron":
        return "cron_app"
    if ch == "subagent" or ch == "exec-event":
        return "subagent"
    return "unknown"


def _infer_cache_state(
    cache_read_tokens: int, cache_write_tokens: int, input_tokens: int
) -> str:
    """Mirror inferCacheState from packages/plugin/src/observer/CostLedger.ts.

    Keeping the rules byte-identical means historical events emitted by
    the plugin and new events emitted by this converter are directly
    comparable in rollups — no schema-version branch required.
    """
    if cache_read_tokens == 0 and cache_write_tokens == 0:
        return "fresh" if input_tokens > 0 else "unknown"
    total_prompt = input_tokens + cache_read_tokens + cache_write_tokens
    if total_prompt == 0:
        return "unknown"
    read_ratio = cache_read_tokens / total_prompt
    if read_ratio >= 0.80:
        return "warm"
    if cache_write_tokens > 0 and read_ratio <= 0.10:
        return "invalidated"
    return "unknown"


def _normalize_ts(raw: str | None) -> str | None:
    """Coerce a turn-collector timestamp to ISO-8601 UTC ('Z' suffix).

    turn-collector writes timestamps like ``2026-05-03T03:22:09.688000+00:00``.
    cost_event consumers expect ISO-8601; either tz form works for parsers
    in observations/access.py, but we normalize to 'Z' for consistency
    with the plugin-emitted historical records.
    """
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw
    if candidate.endswith("+00:00"):
        candidate = candidate[:-6] + "Z"
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# Schema v2 enrichment helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_pod_tz(shared_dir: Path | None = None) -> ZoneInfo:
    """Return the pod's local timezone from network.json, falling back to default.

    Mirrors measure.py::_resolve_tz but without the module-level cache —
    the converter runs as a fresh subprocess per bot, so caching wouldn't
    save anything anyway. Failures (missing file, bad json, bad tz name)
    all fall back to ``_DEFAULT_TZ_NAME``.
    """
    name = _DEFAULT_TZ_NAME
    candidates: list[Path] = []
    if shared_dir is not None:
        candidates.append(Path(shared_dir) / "network.json")
    candidates.append(Path("/Users/Shared/evolve/network.json"))
    for net in candidates:
        try:
            if not net.exists():
                continue
            cfg = json.loads(net.read_text())
            cand = cfg.get("timezone")
            if isinstance(cand, str) and cand.strip():
                name = cand.strip()
                break
        except (json.JSONDecodeError, OSError):
            continue
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_TZ_NAME)


def _to_local_iso(ts_utc: str | None, tz: ZoneInfo) -> str | None:
    """Convert a UTC ISO-8601 timestamp to ISO-8601 in ``tz``.

    Accepts both ``...Z`` and ``...+00:00`` suffixes. Returns None when
    the input can't be parsed — callers treat that as "(unknown)" rather
    than guess.
    """
    if not ts_utc or not isinstance(ts_utc, str):
        return None
    try:
        # datetime.fromisoformat handles "+00:00" natively; replace Z first.
        candidate = ts_utc[:-1] + "+00:00" if ts_utc.endswith("Z") else ts_utc
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).isoformat(timespec="seconds")


# Telegram chat ids matching this pattern are groups (negative integers)
# vs DMs (positive integers). Slack channel ids carry a prefix:
#   D / d → DM
#   C / G → channel / private channel
# Discord doesn't expose DM-vs-channel in the id; default to channel.
_SLACK_DM_PREFIXES = ("d",)
_SLACK_CHANNEL_PREFIXES = ("c", "g")


def _infer_channel_kind(
    channel: str | None,
    channel_id: str | None,
    trigger_kind: str,
) -> str | None:
    """Map (turn-collector channel, channel_id, trigger_kind) → channel_kind.

    Returns one of:
      slack_dm, slack_channel, telegram_dm, telegram_group,
      discord_dm, discord_channel, internal, or None when undecidable.

    Internal triggers (heartbeat / cron / subagent / summarizer / etc.)
    map to ``"internal"`` regardless of channel — they're bot-bot, no
    external user. User-facing turns get the channel-specific bucket
    based on the id shape.
    """
    if trigger_kind in {
        "heartbeat", "cron_app", "subagent", "summarizer",
        "classifier", "task_extractor", "fallback", "forge",
    }:
        return "internal"
    ch = (channel or "").lower().strip()
    cid = (channel_id or "").strip()
    if ch == "telegram":
        if not cid:
            return "telegram_dm"  # default — most personal-bot Telegram is DM
        # Telegram group chats use negative integers; DMs use positive.
        if cid.lstrip("-").isdigit():
            return "telegram_group" if cid.startswith("-") else "telegram_dm"
        return "telegram_dm"
    if ch == "slack":
        if not cid:
            return None  # operator pattern unknown; let the renderer say "slack"
        c0 = cid[:1].lower()
        if c0 in _SLACK_DM_PREFIXES:
            return "slack_dm"
        if c0 in _SLACK_CHANNEL_PREFIXES:
            return "slack_channel"
        return None
    if ch == "discord":
        # Discord ids don't encode DM vs channel; default to channel.
        return "discord_channel"
    if ch in {"webchat", "web", "admin"}:
        return None  # operator-side surfaces don't fit the chat buckets
    if ch in {"heartbeat", "cron", "cron-event", "subagent", "exec-event"}:
        return "internal"
    return None


def _user_id_from_turn(turn: dict) -> str | None:
    """Extract the channel-native user id from a turn-collector record.

    turn-collector populates ``user_id`` as a string (sometimes the
    literal "unknown"). Returns None when the field is empty, "unknown",
    or otherwise unusable so downstream consumers don't have to filter
    for sentinel strings.
    """
    raw = turn.get("user_id")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or cleaned.lower() == "unknown":
        return None
    return cleaned


def _channel_id_from_turn(turn: dict) -> str | None:
    """Extract the channel-native channel id from a turn-collector record.

    Prefers an explicit ``channel_id`` field (added in the TS-side schema
    v2 enrichment). Falls back to deriving from sessionKey shape for the
    Telegram-DM case where channel_id == user_id == the trailing chat id.
    """
    explicit = turn.get("channel_id")
    if isinstance(explicit, str) and explicit.strip():
        cleaned = explicit.strip()
        if cleaned.lower() != "unknown":
            return cleaned
    # Telegram DM: chat_id == user_id when the turn-collector populated
    # user_id from the OC envelope. Use the user_id as channel_id so the
    # operator sees something even on the fallback path.
    ch = (turn.get("channel") or "").lower()
    if ch == "telegram":
        return _user_id_from_turn(turn)
    return None


def _display_name_dnt_enabled(
    bot_id: str, network: dict | None,
) -> bool:
    """Return True if the operator opted OUT of display-name caching for this bot.

    Mirrors the per-bot DNT pattern used by the user-pushback detector
    (TurnObserver.ts::_isPushbackEnabled). Default-OFF (DNT disabled) →
    display names are populated. Set ``network.json::bots[botId].
    costAlertDisplayName = false`` to opt out for a specific bot, or
    ``network.json::costAlertDisplayName = false`` for pod-wide opt-out.

    Fail-open (returns False) on any malformed input — the safer default
    is "operator wants to know who triggered the alert."
    """
    if not isinstance(network, dict):
        return False
    pod_pref = network.get("costAlertDisplayName")
    if pod_pref is False:
        return True
    bots = network.get("bots") or {}
    if not isinstance(bots, dict):
        return False
    bot_cfg = bots.get(bot_id) or {}
    if not isinstance(bot_cfg, dict):
        return False
    return bot_cfg.get("costAlertDisplayName") is False


def _resolve_display_name(
    *,
    network: dict | None,
    channel: str | None,
    channel_kind: str | None,
    user_id: str | None,
    bot_id: str,
) -> str | None:
    """Look up a cached display name for (channel, user_id). Cache-only.

    Strategy:
      1. If DNT is set for the bot, return None unconditionally.
      2. Otherwise, consult name_resolver.cached_name() with the
         underlying platform name (slack / telegram / discord).
      3. Return the resolved ``name`` field, or None when no cache hit.

    Importantly, this NEVER calls out to Slack / Telegram / Discord APIs
    — display-name resolution is best-effort cache-only. The Identity
    page and the wizard already populate the cache for any user the
    operator has interacted with; cost-alert rendering reads through
    that cache. Calling APIs from the converter (which runs every 15
    min per bot) would be the wrong shape for cost-of-observability.
    """
    if not user_id or not isinstance(user_id, str):
        return None
    if _display_name_dnt_enabled(bot_id, network):
        return None
    # Map turn-collector channel → name_resolver platform name.
    platform: str | None = None
    if channel_kind in {"slack_dm", "slack_channel"} or channel == "slack":
        platform = "slack"
    elif channel_kind in {"telegram_dm", "telegram_group"} or channel == "telegram":
        platform = "telegram"
    elif channel_kind in {"discord_dm", "discord_channel"} or channel == "discord":
        platform = "discord"
    if platform is None:
        return None
    if not isinstance(network, dict):
        return None
    # Defer import — name_resolver lives in the admin package; fall back
    # to None silently when evolve_admin isn't importable.
    try:
        from evolve_admin.evo import name_resolver  # noqa: WPS433
    except Exception:
        return None
    try:
        entry = name_resolver.cached_name(
            network, platform, user_id, max_age_days=0,
        )
    except Exception:
        return None
    if not isinstance(entry, dict):
        return None
    name = entry.get("name") or entry.get("username")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def turn_to_cost_event(
    turn: dict,
    *,
    bot_id: str,
    network: dict | None = None,
    pod_tz: ZoneInfo | None = None,
) -> dict | None:
    """Map one turn-collector record to a cost_event record.

    Returns None for records that lack the minimum fields required for
    a billable event (a ts and at least one nonzero token count). Zero-
    token rows are noise and would skew rollups; the plugin path filtered
    them too (see TurnObserver.ts:299).

    Schema v2: populates optional ``timestamp_local``, ``user_id``,
    ``user_display_name``, ``channel_id``, and ``channel_kind`` when the
    source record carries them. Internal triggers (heartbeat / cron /
    subagent) have ``channel_kind="internal"`` and the user/channel ids
    stay None — those events have no external user.
    """
    ts = _normalize_ts(turn.get("ts"))
    if ts is None:
        return None

    input_tokens = int(turn.get("input_tokens") or 0)
    output_tokens = int(turn.get("output_tokens") or 0)
    cache_read = int(turn.get("cache_read_tokens") or 0)
    cache_write = int(turn.get("cache_write_tokens") or 0)
    if input_tokens + output_tokens + cache_read + cache_write <= 0:
        return None

    provider, model = _split_provider_model(turn.get("model") or "")

    raw_channel = turn.get("channel")
    raw_source = turn.get("source")
    trigger_kind = _infer_trigger_kind(raw_source, raw_channel)

    user_id = _user_id_from_turn(turn)
    channel_id = _channel_id_from_turn(turn)
    channel_kind = _infer_channel_kind(raw_channel, channel_id, trigger_kind)
    # Internal triggers never carry external user/channel ids — defensive
    # zeroing so a stray field on the source record can't accidentally
    # tag a heartbeat with someone's Slack id.
    if channel_kind == "internal":
        user_id = None
        channel_id = None

    if pod_tz is None:
        pod_tz = _resolve_pod_tz()
    timestamp_local = _to_local_iso(ts, pod_tz)
    user_display_name = _resolve_display_name(
        network=network,
        channel=(raw_channel.lower() if isinstance(raw_channel, str) else None),
        channel_kind=channel_kind,
        user_id=user_id,
        bot_id=bot_id,
    )

    # trigger_subkind (2026-06-03) — only present on retagged forge turns
    # whose dispatch window was opcoded with a subkind (currently only
    # "operator_confirmed_install" from the spec-review / install-modal
    # confirmation paths). Surfaced as a sibling of trigger_kind so
    # spend_alert can honour the daily-cap exemption without re-deriving
    # the forge phase from session_id windows.
    trigger_subkind = turn.get("forge_subkind")
    if trigger_subkind is not None and not isinstance(trigger_subkind, str):
        trigger_subkind = None

    # App attribution (AL-1.1, design-app-attribution-2026-08-15 §6.2):
    # pass-through of the plugin-stamped fields as siblings of trigger_kind.
    # Tolerant defaults — older turn records (and the OC turn-collector, which
    # never carries the fields) read as app_id=None / attribution="none".
    # Fail toward "no signal": an unrecognized grade, or a grade with no id
    # (and vice versa), normalizes to the honest none/None pair rather than
    # inventing an attribution the writer never made.
    app_id = turn.get("app_id")
    if not (isinstance(app_id, str) and app_id.strip()):
        app_id = None
    app_attribution = turn.get("app_attribution")
    if app_id is None or app_attribution not in (
        "scheduled", "explicit", "inferred", "none",
    ):
        app_attribution = "none"
    if app_attribution == "none":
        app_id = None

    return {
        "schema_version": COST_EVENT_SCHEMA_VERSION,
        "type": "cost_event",
        "ts": ts,
        "timestamp_local": timestamp_local,
        "bot_id": bot_id,
        "session_id": turn.get("session_id") or None,
        "trigger_kind": trigger_kind,
        "trigger_subkind": trigger_subkind,
        "app_id": app_id,
        "app_attribution": app_attribution,
        "model": model,
        "provider": provider,
        "cache_state": _infer_cache_state(cache_read, cache_write, input_tokens),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": float(turn.get("cost") or 0.0),
        "user_id": user_id,
        "user_display_name": user_display_name,
        "channel_id": channel_id,
        "channel_kind": channel_kind,
    }


# ─────────────────────────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────────────────────────


def _turn_log_path(bot_id: str, target_date: date) -> Path:
    """Path to the OC turn-collector record for one bot and date.

    Uses bot_home() (which resolves bot_id → macOS user via the network
    config) so bot_id="team_bot_b" correctly maps to /Users/personal_bot_user/.
    """
    return (
        _bot_home(bot_id) / ".openclaw" / "workspace" / "memory"
        / f"turns-{target_date.isoformat()}.jsonl"
    )


def _evolve_turns_path(shared_dir: Path, bot_id: str, target_date: date) -> Path:
    """Fallback path: TurnObserver.writeTurnToShared output in the evolve shared dir.

    TurnObserver (in the plugin) writes a per-turn record for every agent_end
    event to {sharedDir}/{bot}/turns/turns-{date}.jsonl.  This exists even
    when the OC-native turn-collector is not deployed for a bot, so it serves
    as a reliable fallback source for the converter.
    """
    return shared_dir / bot_id / "turns" / f"turns-{target_date.isoformat()}.jsonl"


def _normalize_evolve_turn(turn: dict) -> dict:
    """Normalize an evolve-shared-turns record to the OC turn-collector schema.

    TurnObserver.writeTurnToShared uses a slightly different field layout:
      - ``model``: bare model name, no provider prefix (e.g. "claude-sonnet-4-6")
      - ``provider``: separate top-level field (e.g. "anthropic")
      - ``source``: "user" instead of "human" for human-driven turns

    v2 fields (user_id, channel_id) flow through unchanged when present —
    the TS writer now emits both via the schema v2 enrichment path
    (CostLedger.ts), and turn_to_cost_event picks them up by name.
    """
    out = dict(turn)
    provider = turn.get("provider") or "unknown"
    model = turn.get("model") or "unknown"
    if "/" not in model and provider not in ("unknown", ""):
        out["model"] = f"{provider}/{model}"
    if out.get("source") == "user":
        out["source"] = "human"
    return out


def _cost_events_path(shared_dir: Path, bot_id: str, target_date: date) -> Path:
    """Path the converter writes cost_event records to.

    Sibling of the legacy ``{date}.jsonl`` annotations file. Splitting
    cost_events into a separate file avoids ownership conflicts with the
    plugin's write path (the plugin runs as the bot user; this converter
    also runs as the bot user, but mixing append-streams from two
    long-running writers on the same file is asking for races).
    """
    return (
        shared_dir / "annotations" / bot_id
        / f"cost_events-{target_date.isoformat()}.jsonl"
    )


def _read_jsonl(path: Path) -> Iterator[dict]:
    """Yield dict records from a JSONL file. Skip malformed lines silently.

    Returns nothing if the file is missing — callers that need to know
    "is there source data?" should stat the path themselves.
    """
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def _existing_keys(path: Path) -> set[tuple[str, str | None]]:
    """Read existing cost_event records and build the dedup key set.

    Key: ``(ts, session_id)``. Two cost events with the same (ts, session)
    are treated as the same call — turn-collector emits at most one record
    per LLM call so this is sufficient.
    """
    keys: set[tuple[str, str | None]] = set()
    for rec in _read_jsonl(path):
        if rec.get("type") != "cost_event":
            continue
        ts = rec.get("ts")
        if not isinstance(ts, str):
            continue
        keys.add((ts, rec.get("session_id")))
    return keys


def _atomic_append(path: Path, lines: list[str]) -> None:
    """Append `lines` to `path` via tempfile + rename concat.

    JSONL append is naturally line-atomic on POSIX, but we can race with
    a concurrent invocation. mkstemp + os.replace gives us proper write
    isolation without the locking headache. Old contents are concatenated
    via read-modify-write rather than appendmode because we want the
    final file to be owned by the running process for chmod.
    """
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = b""
    if path.exists():
        existing = path.read_bytes()
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".cost-events-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(existing)
            f.write(("".join(lines)).encode("utf-8"))
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Conversion driver
# ─────────────────────────────────────────────────────────────────────────────


def _load_network_for_enrichment(shared_dir: Path) -> dict | None:
    """Read ``network.json`` once per convert_day call for v2 enrichment.

    Used for display-name cache lookups + the per-bot DNT flag. Returns
    None on any failure — the converter then writes cost_events with
    ``user_display_name=None`` (caller sees "(unknown)") rather than
    crashing the rollup. The pod-wide tz lookup is separate (see
    _resolve_pod_tz) so a missing/malformed network.json still gives us
    a valid timezone for the timestamp_local field.
    """
    net = Path(shared_dir) / "network.json"
    try:
        return json.loads(net.read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


def convert_day(
    bot_id: str,
    target_date: date,
    shared_dir: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Convert one (bot, date) into appended cost_event records.

    Returns a small report dict for logging: ``{found, written, skipped,
    out_path}``. Missing source files yield ``found=0`` rather than an
    error — quiet bots are normal.

    Source priority:
      1. OC turn-collector: ``{bot_home}/.openclaw/workspace/memory/turns-{date}.jsonl``
      2. Evolve shared turns (fallback): ``{sharedDir}/{bot}/turns/turns-{date}.jsonl``
         Written by TurnObserver.writeTurnToShared; present even when the OC
         turn-collector is not deployed for a given bot.
    """
    oc_src = _turn_log_path(bot_id, target_date)
    evolve_src = _evolve_turns_path(shared_dir, bot_id, target_date)
    dst = _cost_events_path(shared_dir, bot_id, target_date)

    if oc_src.exists():
        source_records: Iterable[dict] = _read_jsonl(oc_src)
    elif evolve_src.exists():
        source_records = (_normalize_evolve_turn(t) for t in _read_jsonl(evolve_src))
    else:
        return {"found": 0, "written": 0, "skipped": 0, "out_path": str(dst)}

    # Forge dispatches enter the system as channel=unknown / source=user.
    # Retag them to source=forge so trigger_kind becomes "forge" instead
    # of "user_turn" and the Usage tab buckets them out of the Human row.
    # Includes the previous day's windows so cross-midnight dispatches
    # still match (a 23:50 build with 1200s timeout reaches into day+1).
    forge_windows = forge_sessions.load_windows(shared_dir, bot_id, target_date)

    # v2 enrichment: load network.json + pod tz once per convert_day so the
    # per-turn loop doesn't re-read them on every record.
    network = _load_network_for_enrichment(shared_dir)
    pod_tz = _resolve_pod_tz(shared_dir)

    seen = _existing_keys(dst)
    new_lines: list[str] = []
    found = 0
    skipped = 0

    for turn in source_records:
        found += 1
        turn = forge_sessions.retag_turn_source(turn, forge_windows)
        event = turn_to_cost_event(
            turn, bot_id=bot_id, network=network, pod_tz=pod_tz,
        )
        if event is None:
            skipped += 1
            continue
        key = (event["ts"], event.get("session_id"))
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        new_lines.append(json.dumps(event, separators=(",", ":")) + "\n")

    if new_lines and not dry_run:
        _atomic_append(dst, new_lines)

    return {
        "found": found,
        "written": len(new_lines),
        "skipped": skipped,
        "out_path": str(dst),
    }


def convert_range(
    bot_id: str,
    end_date: date,
    days: int,
    shared_dir: Path,
    *,
    dry_run: bool = False,
) -> list[tuple[date, dict]]:
    """Run convert_day for the trailing ``days`` ending at ``end_date``."""
    out: list[tuple[date, dict]] = []
    for offset in range(days):
        d = end_date - timedelta(days=offset)
        out.append((d, convert_day(bot_id, d, shared_dir, dry_run=dry_run)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bot-id", required=True, help="Bot id (e.g. admin_bot, team_bot_a)")
    p.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        type=Path,
        help="Evolve shared directory root",
    )
    p.add_argument(
        "--date",
        help="Single date to process (YYYY-MM-DD); defaults to today (UTC).",
    )
    p.add_argument(
        "--backfill",
        type=int,
        nargs="?",
        const=DEFAULT_BACKFILL_DAYS,
        default=None,
        metavar="DAYS",
        help=f"Process the last DAYS days (default {DEFAULT_BACKFILL_DAYS}).",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Compute but do not write."
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.date and args.backfill is not None:
        print("error: --date and --backfill are mutually exclusive", file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).date()
    end_date = today
    if args.date:
        try:
            end_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"error: bad --date {args.date!r}", file=sys.stderr)
            return 2

    if args.backfill is not None:
        results = convert_range(
            args.bot_id, today, args.backfill, args.shared_dir, dry_run=args.dry_run
        )
    else:
        results = [(end_date, convert_day(args.bot_id, end_date, args.shared_dir, dry_run=args.dry_run))]

    total_found = sum(r["found"] for _, r in results)
    total_written = sum(r["written"] for _, r in results)
    total_skipped = sum(r["skipped"] for _, r in results)
    print(
        f"[cost_event_converter] bot={args.bot_id} dates={len(results)} "
        f"found={total_found} written={total_written} skipped={total_skipped}"
        + (" [dry-run]" if args.dry_run else "")
    )
    for d, r in results:
        if r["found"] or r["written"]:
            print(
                f"  {d.isoformat()}: found={r['found']} "
                f"written={r['written']} skipped={r['skipped']} -> {r['out_path']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
