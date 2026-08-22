"""
usage_analytics.py — Turn-based usage & cost analytics.

Ported from docs/reference/openclaw-admin.py (_load_turns, _print_usage_summary,
_turn_key, _model_family, _model_color) with the following changes:
  - Reads from {shared_dir}/{bot}/turns/ first (world-readable shared dir; the
    shared dir is platform-keyed via CANONICAL_SHARED_DIR)
  - Falls back to bot workspace/memory (read directly; evolve has ACL on .openclaw/)
  - Uses resolve_bot_paths() for path resolution — no hardcoded paths
  - Returns structured dicts for JSON serialisation (no print statements)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any  # noqa: F401 — used in compute_summary annotations

# Minimum billed tokens (input + output) a model must accumulate in the
# window before its observed $/1k figure is treated as trustworthy. Below
# this, a single big-context call or a stray estimate dominates the ratio,
# so the per-model $/1k carries a ``low_confidence`` flag and the UI shows
# "insufficient data" instead of a noisy number. 10k billed tokens ≈ a
# handful of real turns — enough to average out one-off spikes without
# hiding a model that's genuinely only been used lightly.
MODEL_COST_MIN_TOKENS = 10_000

# ── Model color mapping ────────────────────────────────────────────────────────
# CSS hex values matching the ANSI colors in openclaw-admin.py
# Used by both the Python API (color field in by_model) and the JS chart.

MODEL_COLORS_HEX: dict[str, str] = {
    # Anthropic — haiku = green, sonnet = cyan, opus = blue
    "anthropic/claude-haiku-4-6":          "#22c55e",
    "anthropic/claude-haiku-4-5":          "#22c55e",
    "anthropic/claude-haiku-3-5":          "#22c55e",
    "anthropic/claude-3-haiku":            "#22c55e",
    "anthropic/claude-sonnet-4-6":         "#06b6d4",
    "anthropic/claude-sonnet-4-5":         "#06b6d4",
    "anthropic/claude-sonnet-4-20250514":  "#06b6d4",
    "anthropic/claude-sonnet-4-20250219":  "#06b6d4",
    "anthropic/claude-3-5-sonnet":         "#06b6d4",
    "anthropic/claude-3-sonnet":           "#06b6d4",
    "anthropic/claude-opus-4-6":           "#3b82f6",
    "anthropic/claude-opus-4-5":           "#3b82f6",
    "anthropic/claude-3-opus":             "#3b82f6",
    # Unexpected billing mode (any provider) — warning red
    "unexpected_billing":                  "#ef4444",
    # OpenAI — white/grey family
    "openai/gpt-4o":                       "#e5e7eb",
    "openai/gpt-4o-mini":                  "#9ca3af",
    "openai/gpt-4.1":                      "#e5e7eb",
    "openai/gpt-4.1-mini":                 "#9ca3af",
    "openai/gpt-5.1-codex":               "#f9fafb",
    "openai/o3":                           "#f9fafb",
    "openai/o4-mini":                      "#9ca3af",
    # Google — yellow family
    "google/gemini-3.1-pro-preview":       "#eab308",
    "google/gemini-2.5-pro-preview":       "#eab308",
    "google/gemini-1.5-pro":              "#fde047",
    "google/gemini-2.0-flash":            "#a16207",
    "google/gemini-2.0-flash-lite":       "#a16207",
    # xAI — magenta family
    "xai/grok-4-1-fast":                   "#a855f7",
    "xai/grok-3":                          "#d946ef",
    "xai/grok-3-mini":                     "#7c3aed",
    # Provider fallbacks
    "anthropic":                           "#06b6d4",
    "openai":                              "#e5e7eb",
    "google":                              "#eab308",
    "xai":                                 "#a855f7",
    "mistral":                             "#3b82f6",
    "runway":                              "#d946ef",
    "unknown":                             "#6b7280",
}


# ── Per-token pricing table (USD per million tokens) ──────────────────────────
# Used to estimate cost from token counts when the turn record has cost=0
# (pre-API-key era turns always had cost hardcoded to 0 regardless of usage).
# Prices are approximate public list prices; update when Anthropic/OpenAI/Google
# publish new rates.  Format: model_key → (input, output, cache_write, cache_read)
# All values are USD per 1,000,000 tokens.
_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Anthropic — (input, output, cache_write, cache_read) per MTok
    "anthropic/claude-haiku-4-6":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-haiku-4-5":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-haiku-3-5":          (0.80,   4.00,  1.00, 0.08),
    "anthropic/claude-3-haiku":            (0.25,   1.25,  0.30, 0.03),
    "anthropic/claude-sonnet-4-6":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-5":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-20250514":  (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-sonnet-4-20250219":  (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-3-5-sonnet":         (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-3-sonnet":           (3.00,  15.00,  3.75, 0.30),
    "anthropic/claude-opus-4-6":           (15.00, 75.00, 18.75, 1.50),
    "anthropic/claude-opus-4-5":           (15.00, 75.00, 18.75, 1.50),
    "anthropic/claude-3-opus":             (15.00, 75.00, 18.75, 1.50),
    # OpenAI — no prompt caching price difference, use 0 for cache fields
    "openai/gpt-4o":                       (2.50,  10.00,  0.00, 1.25),
    "openai/gpt-4o-mini":                  (0.15,   0.60,  0.00, 0.075),
    "openai/gpt-4.1":                      (2.00,   8.00,  0.00, 1.00),
    "openai/gpt-4.1-mini":                 (0.40,   1.60,  0.00, 0.20),
    "openai/gpt-4.1-nano":                 (0.10,   0.40,  0.00, 0.05),
    "openai/o3":                           (10.00,  40.00, 0.00, 2.50),
    "openai/o4-mini":                      (1.10,   4.40,  0.00, 0.275),
    # Google — Gemini
    "google/gemini-3.1-pro-preview":       (1.25,  10.00,  0.00, 0.00),
    "google/gemini-2.5-pro-preview":       (1.25,  10.00,  0.00, 0.00),
    "google/gemini-1.5-pro":               (1.25,   5.00,  0.00, 0.00),
    "google/gemini-2.0-flash":             (0.10,   0.40,  0.00, 0.00),
    "google/gemini-2.0-flash-lite":        (0.075,  0.30,  0.00, 0.00),
    # xAI
    "xai/grok-3":                          (3.00,  15.00,  0.00, 0.00),
    "xai/grok-3-mini":                     (0.30,   0.50,  0.00, 0.00),
    "xai/grok-4-1-fast":                   (5.00,  25.00,  0.00, 0.00),
}

# Provider-level fallback pricing when exact model not in table.
# Using a mid-range estimate so unknowns aren't silently free.
_PROVIDER_PRICING_FALLBACK: dict[str, tuple[float, float, float, float]] = {
    "anthropic": (3.00,  15.00, 3.75, 0.30),
    "openai":    (2.50,  10.00, 0.00, 1.25),
    "google":    (1.25,   5.00, 0.00, 0.00),
    "xai":       (3.00,  15.00, 0.00, 0.00),
    "mistral":   (2.00,   6.00, 0.00, 0.00),
    "groq":      (0.59,   0.79, 0.00, 0.00),
}

_MTok = 1_000_000.0


def _estimate_turn_cost(turn: dict) -> float:
    """
    Estimate cost for a turn from token counts when the recorded cost is 0.

    Uses the model field (with provider prefix) or the provider field to look
    up per-token pricing.  Returns 0.0 if no pricing is available (rather than
    raising), so unknown models are counted but not double-billed.
    """
    model    = (turn.get("model") or "").strip()
    provider = (turn.get("provider") or "").strip()

    # Normalise: if model lacks a "/" prefix but provider is known, prepend it
    if "/" not in model and provider and provider != "unknown":
        model = f"{provider}/{model}"

    pricing = _MODEL_PRICING.get(model)
    if pricing is None and "/" in model:
        pricing = _PROVIDER_PRICING_FALLBACK.get(model.split("/")[0])
    if pricing is None and provider and provider != "unknown":
        pricing = _PROVIDER_PRICING_FALLBACK.get(provider)
    if pricing is None:
        return 0.0

    input_p, output_p, cw_p, cr_p = pricing
    inp  = int(turn.get("input_tokens")       or 0)
    out  = int(turn.get("output_tokens")      or 0)
    cw   = int(turn.get("cache_write_tokens") or 0)
    cr   = int(turn.get("cache_read_tokens")  or 0)

    return (
        inp  * input_p  / _MTok
        + out  * output_p / _MTok
        + cw   * cw_p    / _MTok
        + cr   * cr_p    / _MTok
    )


def _turn_provider(turn: dict) -> str:
    """
    Derive the provider for a turn.

    Prefers the explicit 'provider' field written by TurnObserver.
    Falls back to extracting the prefix from the model string (e.g.
    'anthropic/claude-sonnet-4-5' → 'anthropic').
    Falls back to 'unknown' if neither yields a real value.
    """
    provider = (turn.get("provider") or "").strip()
    if provider and provider != "unknown":
        return provider
    model = (turn.get("model") or "").strip()
    if "/" in model:
        return model.split("/")[0]
    return "unknown"


def _model_color(key: str) -> str:
    """Return a CSS hex color for a turn key (full model name, possibly :unexpected_billing suffixed)."""
    if key in MODEL_COLORS_HEX:
        return MODEL_COLORS_HEX[key]
    # Unexpected billing turns shown in warning red regardless of provider
    if key.endswith(":unexpected_billing"):
        return "#ef4444"
    # Try provider prefix
    provider = key.split("/")[0] if "/" in key else key.split(":")[0]
    return MODEL_COLORS_HEX.get(provider, MODEL_COLORS_HEX["unknown"])


def _model_family(model: str) -> str:
    return model.split("/")[0] if "/" in model else "unknown"


def _turn_key(turn: dict) -> str:
    """
    Return a display key for a turn: full model name.
    For turns flagged as unexpected billing mode (e.g. Anthropic MAX drift to
    API key), appends ':unexpected_billing' so they show as a distinct series.
    """
    model = turn.get("model", "unknown")
    if turn.get("unexpected_billing_mode"):
        return f"{model}:unexpected_billing"
    return model


def _read_jsonl_direct(path: Path) -> list[dict]:
    """Read a JSONL file directly; return [] on any error."""
    try:
        text = path.read_text()
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records


def _find_turns_dirs(bot_id: str, network_path: str | None = None) -> list[Path]:
    """
    Return list of dirs to search for turns files, in priority order.

    Deployments vary — bots may run as separate OS users with standard
    ~/.openclaw paths, or as the same user with custom workspace paths, or
    with a shared dir that hasn't been created yet.  We probe all known
    candidate locations and let the caller try each one, so a single
    misconfigured path doesn't block access to data that lives elsewhere.

    Priority:
      1. {shared_dir}/{bot_id}/turns             (new-style shared dir, world-readable)
      2. {workspace}/memory                        (from openclaw.json; under .openclaw/, evolve-readable)
      3. {home}/.openclaw/workspace/memory         (home-derived fallback)
    """
    # Try to get the full candidate list from resolve_bot_paths (reads openclaw.json)
    try:
        from evolve_admin.web.server import resolve_bot_paths
        paths = resolve_bot_paths(bot_id)
        candidates = paths.get("turns_dir_candidates") or [
            paths["turns_dir"],
            paths["turns_dir_fallback"],
        ]
        return [Path(c) for c in candidates]
    except Exception:
        pass

    # Fallback: construct candidates without server.py.
    # Shared dir is platform-keyed (CANONICAL_SHARED_DIR = /Users/Shared/evolve
    # on macOS, /var/lib/evolve on Linux) — a macOS literal here would miss every
    # turn on a Linux pod, where the plugin writes to /var/lib/evolve/{bot}/turns.
    from evolve_config import bot_home as _bot_home, CANONICAL_SHARED_DIR
    home = _bot_home(bot_id)
    shared = CANONICAL_SHARED_DIR / bot_id / "turns"
    workspace_memory = home / ".openclaw" / "workspace" / "memory"
    return [shared, workspace_memory]


def load_turns(
    bot_id: str | None,
    days: int = 7,
    end_date: datetime | None = None,
    channel_filter: str | None = None,
    source_filter: str | None = None,
    network_path: str | None = None,
) -> list[dict]:
    """
    Load turn records for one or all bots over a date range.

    bot_id       — None loads all bots in the network; otherwise loads a single bot.
    days         — number of days to look back (inclusive of end_date).
    end_date     — last day to include (defaults to today).
    channel_filter — if set, only return turns with matching channel.
    source_filter  — if set, only return turns with matching source.
    network_path   — path to network.json (for discovering bot list when bot_id=None).
    """
    if end_date is None:
        end_date = datetime.now()

    date_strs = [
        (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days - 1, -1, -1)
    ]

    # Determine which bots to load
    bot_ids: list[str] = []
    if bot_id is not None:
        bot_ids = [bot_id]
    else:
        # Discover bots from network.json
        try:
            from evolve_config import CANONICAL_NETWORK_JSON
            _np = Path(network_path) if network_path else CANONICAL_NETWORK_JSON
            data = json.loads(_np.read_text())
            bots = data.get("bots") or {}
            bot_ids = list(bots.keys()) if bots else data.get("members", [])
        except Exception:
            bot_ids = []

    turns: list[dict] = []
    for bid in bot_ids:
        dirs = _find_turns_dirs(bid, network_path)
        for date_str in date_strs:
            fname = f"turns-{date_str}.jsonl"
            # Read from the first candidate dir that has data. Every candidate
            # is readable directly: the shared dir is world-readable and the
            # workspace/memory fallback lives under .openclaw/ (evolve ACL).
            # The former `sudo -u <bot>` fallback only ever failed silently —
            # evolve has no such grant (CLAUDE.md §"File Access Pattern").
            for turns_dir in dirs:
                path = turns_dir / fname
                recs = _read_jsonl_direct(path)
                if recs:
                    for rec in recs:
                        rec.setdefault("instance", bid)
                    turns.extend(recs)
                    break

    # Normalise source field: the OC turn-collector path emits
    # ``source: "human"`` while the evolve-shared fallback path
    # (TurnObserver.writeTurnToShared) emits ``source: "user"``. Both
    # mean the same thing — direct human input. The cost_event_converter
    # already does this normalisation for cost_events; we mirror it here
    # so downstream callers (Usage page, by_source rollups, source_filter)
    # see a single canonical value.
    for rec in turns:
        if rec.get("source") == "user":
            rec["source"] = "human"
    if source_filter == "user":
        source_filter = "human"

    # Forge dispatches (`sudo -u <bot> openclaw agent --local --agent main`)
    # land here as channel=unknown / source=human. Without retagging, every
    # Forge build/critique/refine shows up on the Usage tab as Human chat,
    # falsely inflating the Human bucket. The forge_sessions index lets us
    # match turns against the dispatch windows the forge engine stamped at
    # spawn time. See packages/analyzer/forge_sessions.py.
    _apply_forge_retag(turns, bot_ids, end_date, days)

    # Apply filters
    if channel_filter:
        turns = [t for t in turns if t.get("channel") == channel_filter]
    if source_filter:
        turns = [t for t in turns if t.get("source") == source_filter]

    return turns


def _apply_forge_retag(
    turns: list[dict],
    bot_ids: list[str],
    end_date: datetime,
    days: int,
) -> None:
    """Retag in-place any turn that falls inside a forge dispatch window.

    Loads forge windows once per (bot, date) across the loaded date range
    so the overall cost is O(turns × windows-per-bot). Quiet bots have
    no forge_sessions/ dir at all and the lookup short-circuits.

    Best-effort: if forge_sessions can't be imported or the shared dir
    can't be read, leave turns unchanged rather than fail the whole
    Usage page.
    """
    try:
        import forge_sessions as _fs  # type: ignore
    except Exception:
        return

    try:
        from evolve_config import CANONICAL_SHARED_DIR  # noqa: F401
        shared = CANONICAL_SHARED_DIR
    except Exception:
        shared = Path("/Users/Shared/evolve")

    if not bot_ids:
        return

    # Build a per-bot list of windows across the loaded date range plus
    # one day on either side (forge dispatches can cross UTC midnight).
    # Loaded turns span end_d - (days-1) .. end_d; we pull windows over
    # end_d - days .. end_d + 1 so windows that started before midnight
    # or ended after midnight are still seen.
    end_d = end_date.date()
    windows_by_bot: dict[str, list] = {}
    date_offsets = range(-1, days + 1)  # -1 → tomorrow, days → days+1 ago
    for bid in bot_ids:
        merged: list = []
        for offset in date_offsets:
            d = end_d - timedelta(days=offset)
            try:
                merged.extend(_fs.load_windows(shared, bid, d, include_prev_day=False))
            except Exception:
                continue
        windows_by_bot[bid] = merged

    for rec in turns:
        bid = rec.get("instance") or ""
        windows = windows_by_bot.get(bid)
        if not windows:
            continue
        if _fs.is_forge_turn(
            windows, rec.get("ts"), rec.get("channel"), rec.get("source")
        ):
            rec["source"] = "forge"


def _observed_per_1k(
    cost: float, input_tokens: int, output_tokens: int
) -> dict:
    """Compute observed $/1k-token figures for one model from its window totals.

    Returns a dict with:
      usd_per_1k_input    — total $ ÷ (input tokens / 1000)
      usd_per_1k_output   — total $ ÷ (output tokens / 1000)
      usd_per_1k_blended  — total $ ÷ ((input + output) / 1000)
      billed_tokens       — input + output (the min-sample denominator)
      low_confidence      — True when billed_tokens < MODEL_COST_MIN_TOKENS

    These figures are NOT a clean per-token unit price (that would need
    the input/output cost broken out per call, which the turn records do
    not carry). Each is "total spend attributed to this model (cache cost
    INCLUDED), normalised by its input (or output) token volume" — i.e. an
    EFFECTIVE cost per 1k I/O tokens, the right shape for the operator
    question "what is this model actually costing me per 1k tokens of
    work?" The blended figure is the most directly comparable across
    models; the split figures show whether a model's traffic is input- or
    output-heavy.

    Cache-read and cache-write tokens are deliberately excluded from the
    DENOMINATOR (not the numerator): cache reads are real spend that stays
    in the total, so a heavily-cached model honestly reads higher per 1k
    I/O tokens — folding cache tokens into the denominator would make
    cached models look artificially cheap. The detailed cache economics
    have their own surface (session_economics + the By Model cache
    columns). $/1k here is an effective cost per unit of actual I/O work,
    not a clean per-token list price.
    """
    billed = max(0, int(input_tokens)) + max(0, int(output_tokens))
    low_conf = billed < MODEL_COST_MIN_TOKENS

    def _rate(tokens: int) -> float | None:
        if tokens <= 0 or cost <= 0:
            return None
        return round(cost / (tokens / 1000.0), 4)

    return {
        "usd_per_1k_input":   _rate(input_tokens),
        "usd_per_1k_output":  _rate(output_tokens),
        "usd_per_1k_blended": _rate(input_tokens + output_tokens),
        "billed_tokens":      billed,
        "low_confidence":     low_conf,
    }


# ── Channel / platform shape helpers (read-layer, provider-neutral) ──────────
# Turn records carry no `platform` field today (backlog: stamp it at the gateway
# TurnObserver — same family as the model-prefix inconsistency note). Until then
# the read layer infers the provider from an id's SHAPE. This mirrors the
# per-platform branching already in roster_resolver.meta_to_display_name — it is
# shape-based (no provider literals in routing logic) and handles ALL historical
# turns regardless of any future gateway stamp.

_THREAD_MARKER = ":thread:"

# source → human-legible system category for By Channel's no-conversation rows.
_SYSTEM_CATEGORY: dict[str, str] = {
    "heartbeat": "Heartbeat",
    "cron":      "Scheduled",
    "forge":     "Forge builds",
    "subagent":  "Subagents",
    "evo":       "Evo",
}

# Channel sentinels that are NOT a real conversation id (no human on the other
# end of these). Their volume is relabeled into named system categories.
_SYSTEM_CHANNELS: frozenset[str] = frozenset({"", "unknown", "heartbeat"})


def _infer_platform(conversation_or_user_id: "str | None") -> str:
    """Best-effort provider slug from an id's shape — no network, no catalog.

    Slack object ids are prefixed C/D/G/U/W (channel / DM / private-group /
    user / enterprise-user) or carry a ``:thread:`` marker; Telegram chat & user
    ids are all-digits (a leading ``-`` marks a group). Unknown shapes return
    ``"unknown"``. Case-insensitive on the leading letter so a lower-cased
    conversation stem (seen in some threaded ids) still resolves to slack.

    The Slack-id branch additionally requires a digit in the tail. Real Slack
    object ids are always alphanumeric *with* digits (``U9ZL3JYR3``, ``C0AK…``),
    while the sentinel ``channel`` values that otherwise sail through the bare
    prefix+alnum check — ``unknown``, ``webchat``, ``web``, ``discord``, ``cron``
    — are digit-free English words. Without the digit guard those non-Slack
    turns were misclassified as Slack and dumped into the "Slack user · ?"
    By-User bucket (~63% of it was channel=unknown/webchat). The ``:thread:``
    branch above is unaffected, so threaded ids still resolve regardless of case.
    """
    s = (conversation_or_user_id or "").strip()
    if not s:
        return "unknown"
    if _THREAD_MARKER in s:
        return "slack"
    # Slack object id: a C/D/G/U/W prefix (case-insensitive — a rolled-up thread
    # parent can be a lower-cased stem) followed by an alphanumeric body that
    # contains at least one digit. The alnum guard keeps arbitrary words
    # ("weird-id") from matching on the leading letter alone; the digit guard
    # keeps digit-free words ("unknown", "webchat", "discord") from matching
    # on a coincidental C/D/G/U/W initial.
    tail = s[1:]
    if (
        s[0].upper() in ("C", "D", "G", "U", "W")
        and tail.isalnum()
        and any(c.isdigit() for c in tail)
    ):
        return "slack"
    body = s[1:] if s[0] == "-" else s
    if body.isdigit():
        return "telegram"
    return "unknown"


def _split_thread(channel: str) -> "tuple[str, str | None]":
    """Split ``<parent>:thread:<ts>`` into ``(parent, ts)``; a non-threaded
    channel returns ``(channel, None)``. Threads roll up to their parent
    conversation in By Channel."""
    idx = channel.find(_THREAD_MARKER)
    if idx >= 0:
        return channel[:idx], channel[idx + len(_THREAD_MARKER):]
    return channel, None


def _is_system_channel(channel: str) -> bool:
    """True when a turn carries no real conversation id — a sentinel channel
    value (``unknown`` / ``heartbeat`` / empty). Such turns are relabeled into
    named system categories rather than shown as conversation rows."""
    return channel in _SYSTEM_CHANNELS


def _system_category(source: "str | None") -> str:
    """Map a turn's source to a human-legible system category for By Channel.
    heartbeat→Heartbeat, cron→Scheduled, forge→Forge builds, subagent→Subagents,
    evo→Evo, everything else→System."""
    return _SYSTEM_CATEGORY.get((source or "").lower(), "System")


def compute_summary(turns: list[dict]) -> dict:
    """
    Compute the full usage summary from a list of turn records.

    Returns a structured dict (not print output) with:
      total_turns, total_cost
      by_date: [{date, total, by_model: {model_key: count}}]
      by_model: [{model, calls, cost, auth_mode, color, cache_read, cache_write}]
      by_channel: [{channel, calls, cost, system, threads:[{thread_ts,calls,cost}],
                    category?}]  — real conversations (threads rolled up to
                    parent) then named system categories (system=True)
      by_source:  [{source, calls, cost}]
      by_user:    [{platform, user_id, instance, calls}]  (top 20) — REAL PEOPLE
                    only (human-bucket turns); the route adds display_name +
                    name_source + a categorized fallback label
      billing:    {by_provider: {provider: {calls, cost}},
                   unexpected_billing_turns,
                   human_cost, cron_cost, subagent_cost, has_cost_data}
    """
    if not turns:
        return _empty_summary()

    # Aggregation buckets
    by_date_total: dict[str, int] = defaultdict(int)
    by_date_cost:  dict[str, float] = defaultdict(float)
    by_date_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # Per-day three-bucket trigger split (human / scheduled / background).
    # Mirrors tile_metrics._classify_split + the Usage Composition card's
    # bucket map: source ∈ {user, human} → human; {heartbeat, cron} →
    # scheduled; everything else (subagent / unknown / null) → background.
    by_date_trigger: dict[str, dict[str, int]] = defaultdict(
        lambda: {"human": 0, "scheduled": 0, "background": 0}
    )
    # Cost equivalents — same shape, same keys, but tracking dollars
    # per-day per-model and per-day per-bucket. The Usage page's timeline
    # charts use either turns (the count dicts above) or cost (these
    # parallel dicts) depending on the unit toggle.
    by_date_model_cost:   dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_date_trigger_cost: dict[str, dict[str, float]] = defaultdict(
        lambda: {"human": 0.0, "scheduled": 0.0, "background": 0.0}
    )
    # Per-session aggregates for the context-health card and the day-drill
    # top-sessions panel. Populated alongside the other rollups in the
    # main loop below to avoid a second pass.
    session_contexts: dict[str, Any] = {}
    by_model_calls: dict[str, int] = defaultdict(int)
    by_model_cost:  dict[str, float] = defaultdict(float)
    by_model_auth:  dict[str, str] = {}
    by_model_cache_read:  dict[str, int] = defaultdict(int)
    by_model_cache_write: dict[str, int] = defaultdict(int)
    # Per-model billed-token totals — feed the observed $/1k computation
    # in the by_model list below. Input vs output kept separate so the UI
    # can show $/1k input + $/1k output (the two have very different
    # per-token rates on every frontier provider).
    by_model_input:  dict[str, int] = defaultdict(int)
    by_model_output: dict[str, int] = defaultdict(int)
    # Per-model bot-count + recency — the two small NEW aggregations the
    # Model Economics lens needs (spec-model-economics-page-2026-06-13 §Data
    # sources). bot-count = distinct `instance` per model ("how many bots use
    # this model"); last_ts = max `ts` per model ("last used"). Both are a
    # re-aggregation of turns already loaded — no second load. ADDITIVE on the
    # by_model rows, so the Cost page is unaffected.
    by_model_instances: dict[str, set[str]] = defaultdict(set)
    by_model_last_ts:   dict[str, str] = {}
    # Per-model audience split: human (direct operator/user input) vs
    # non_human (everything else — heartbeat, cron, subagent, forge,
    # unknown). Lets the Usage tab show "am I using premium models for
    # autonomous work when cheap models would do?" at a glance.
    # Keyed [model][audience] → {calls, cost}.
    by_model_audience_calls: dict[str, dict[str, int]] = defaultdict(
        lambda: {"human": 0, "non_human": 0}
    )
    by_model_audience_cost: dict[str, dict[str, float]] = defaultdict(
        lambda: {"human": 0.0, "non_human": 0.0}
    )
    # By Channel: roll thread sub-conversations (``<channel>:thread:<ts>``) up
    # to their parent conversation, tracking each thread leg for the expandable
    # child list. Turns with no real conversation (sentinel channels) route to
    # by_system_* under a named category instead, so real conversations render
    # distinct from heartbeat / forge / subagent volume.
    by_channel_calls: dict[str, int] = defaultdict(int)
    by_channel_cost:  dict[str, float] = defaultdict(float)
    by_channel_threads: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"calls": 0, "cost": 0.0})
    )
    by_system_calls: dict[str, int] = defaultdict(int)
    by_system_cost:  dict[str, float] = defaultdict(float)
    by_source_calls: dict[str, int] = defaultdict(int)
    by_source_cost:  dict[str, float] = defaultdict(float)
    # Track distinct session_ids per source so the Usage page can show
    # "240 turns / 58 sessions" alongside cost in the By Source table.
    # Set per source — len() at output time gives the session count.
    by_source_sessions: dict[str, set[str]] = defaultdict(set)
    # By User: REAL PEOPLE only (human-bucket turns), keyed by (platform,
    # user_id) — the same raw user_id recurs across bots and platforms, so the
    # platform disambiguates. Per key we also track the dominant bot (most
    # calls) for display + cache-only name resolution at the route layer. System
    # / scheduled traffic is non-human and never lands here (it's relabeled into
    # By Channel categories above), which is what evicts the old `unknown:?` /
    # `heartbeat:?` rows that dominated the table.
    by_user_calls: dict[tuple[str, str], int] = defaultdict(int)
    by_user_instances: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    total_cost = 0.0
    has_cost = False

    for t in turns:
        d_str   = (t.get("ts") or "")[:10] or "unknown"
        key     = _turn_key(t)
        # Use recorded cost when available; fall back to token-based estimate.
        # Turns written before API-key billing (or by older plugin versions)
        # have cost hardcoded to 0 even when tokens were consumed.
        recorded_cost = float(t.get("cost") or 0)
        cost = recorded_cost if recorded_cost else _estimate_turn_cost(t)
        channel = t.get("channel") or "unknown"
        source  = t.get("source")  or "unknown"
        user_id = t.get("user_id") or "?"
        auth    = t.get("auth_mode") or "unknown"
        cr      = int(t.get("cache_read_tokens")  or 0)
        cw      = int(t.get("cache_write_tokens") or 0)
        inp     = int(t.get("input_tokens")  or 0)
        out     = int(t.get("output_tokens") or 0)

        if cost:
            has_cost = True
        total_cost += cost

        by_date_total[d_str] += 1
        by_date_cost[d_str]  += cost
        by_date_model[d_str][key] += 1
        by_date_model_cost[d_str][key] += cost
        if source in ("user", "human"):
            bucket = "human"
        elif source in ("heartbeat", "cron"):
            bucket = "scheduled"
        else:
            # forge / subagent / unknown all roll into background. Forge is
            # the retag added in _apply_forge_retag for OC `--local --agent
            # main` dispatches from the forge engine — without the retag
            # they'd land in the human bucket above.
            bucket = "background"
        by_date_trigger[d_str][bucket] += 1
        by_date_trigger_cost[d_str][bucket] += cost

        by_model_calls[key] += 1
        by_model_cost[key]  += cost
        by_model_auth[key]   = auth if key not in by_model_auth else by_model_auth[key]
        by_model_cache_read[key]  += cr
        by_model_cache_write[key] += cw
        by_model_input[key]  += inp
        by_model_output[key] += out
        # Bot-count + recency for the Model Economics lens. instance is set on
        # every turn by load_turns (defaults to bot_id); ts is an ISO string so
        # a lexicographic max is also the chronological max.
        inst = t.get("instance")
        if inst:
            by_model_instances[key].add(inst)
        ts = t.get("ts")
        if ts and (key not in by_model_last_ts or ts > by_model_last_ts[key]):
            by_model_last_ts[key] = ts

        # Audience split: same bucket logic as the trigger composition
        # chart, collapsed to two values. The single source of truth for
        # what counts as "human" is the bucket computation above —
        # keeping these aligned matters when the operator cross-checks
        # By Source totals against per-model audience splits.
        audience = "human" if bucket == "human" else "non_human"
        by_model_audience_calls[key][audience] += 1
        by_model_audience_cost[key][audience]  += cost

        if _is_system_channel(channel):
            # No real conversation — relabel into a named system category.
            cat = _system_category(source)
            by_system_calls[cat] += 1
            by_system_cost[cat]  += cost
        else:
            parent, thread_ts = _split_thread(channel)
            by_channel_calls[parent] += 1
            by_channel_cost[parent]  += cost
            if thread_ts:
                leg = by_channel_threads[parent][thread_ts]
                leg["calls"] += 1
                leg["cost"]  += cost

        by_source_calls[source] += 1
        by_source_cost[source]  += cost
        sid = t.get("session_id")
        if sid:
            by_source_sessions[source].add(sid)

        # By User: REAL PEOPLE only. `bucket` (computed above) is "human" exactly
        # for direct operator/user turns; everything else is system/scheduled and
        # must NOT appear here. Infer the platform from the user_id shape, falling
        # back to the conversation id when the user_id is opaque (e.g. "?").
        if bucket == "human":
            plat = _infer_platform(user_id)
            if plat == "unknown":
                plat = _infer_platform(channel)
            pkey = (plat, user_id)
            by_user_calls[pkey] += 1
            inst = t.get("instance")
            if inst:
                by_user_instances[pkey][inst] += 1

        # Per-session aggregates — drives both the context-health card
        # and the day-drill top-sessions panel. session_id may be missing
        # for very early or malformed records; fall back to "?" so they
        # collapse into a single "unknown" bucket rather than crashing.
        ssid = sid or "?"
        if ssid not in session_contexts:
            session_contexts[ssid] = {
                "max_cache_write": 0,
                "total_cache_read": 0,
                "turns": 0,
                "total_cost": 0.0,
                "date": d_str,
                # Source/instance: first non-empty wins.
                "source": source if source != "unknown" else "",
                "instance": t.get("instance") or "",
            }
        sc = session_contexts[ssid]
        sc["max_cache_write"] = max(sc["max_cache_write"], cw)
        sc["total_cache_read"] += cr
        sc["turns"] += 1
        sc["total_cost"] += cost
        if not sc["source"] and source != "unknown":
            sc["source"] = source
        if not sc["instance"] and t.get("instance"):
            sc["instance"] = t["instance"]

    # Billing breakdown — per-provider, provider-neutral
    total_turns = len(turns)
    by_provider_calls: dict[str, int] = defaultdict(int)
    by_provider_cost: dict[str, float] = defaultdict(float)
    unexpected_billing_turns = 0

    def _tcost(t: dict) -> float:
        """Recorded cost when non-zero; otherwise token-based estimate."""
        c = float(t.get("cost") or 0)
        return c if c else _estimate_turn_cost(t)

    for t in turns:
        provider = _turn_provider(t)
        by_provider_calls[provider] += 1
        by_provider_cost[provider] += _tcost(t)
        if t.get("unexpected_billing_mode"):
            unexpected_billing_turns += 1

    by_provider = {
        p: {
            "calls": by_provider_calls[p],
            "cost": round(by_provider_cost[p], 6),
        }
        for p in sorted(by_provider_calls, key=lambda k: -by_provider_calls[k])
    }

    human_cost    = sum(_tcost(t) for t in turns if t.get("source") == "human")
    cron_cost     = sum(_tcost(t) for t in turns if t.get("source") == "cron")
    subagent_cost = sum(_tcost(t) for t in turns if t.get("source") == "subagent")

    # Top sessions per day — drives the Usage page's day-drill panel.
    # Group session_contexts by date, sort by total_cost desc, keep top
    # PER_DAY_TOP_SESSIONS. Same source-bucket mapping used elsewhere
    # so the drill panel can render bucket pills consistent with the
    # composition card and the trigger timeline.
    PER_DAY_TOP_SESSIONS = 5

    def _bucket_of(src: str) -> str:
        s = (src or "").lower()
        if s in ("user", "human"):
            return "human"
        if s in ("heartbeat", "cron"):
            return "scheduled"
        return "background"

    sessions_by_date: dict[str, list[dict]] = defaultdict(list)
    for sid, sc in session_contexts.items():
        sessions_by_date[sc["date"]].append({
            "session_id": sid,
            "cost": round(sc["total_cost"], 6),
            "turns": sc["turns"],
            "source": sc.get("source") or "unknown",
            "bucket": _bucket_of(sc.get("source")),
            "instance": sc.get("instance") or "",
        })
    top_sessions_by_date: dict[str, list[dict]] = {}
    for d, sessions in sessions_by_date.items():
        sessions.sort(key=lambda s: s["cost"], reverse=True)
        top_sessions_by_date[d] = sessions[:PER_DAY_TOP_SESSIONS]

    # Build by_date list
    by_date_list = [
        {
            "date": d,
            "total": by_date_total[d],
            "total_cost": round(by_date_cost[d], 6),
            "by_model": dict(by_date_model[d]),
            "by_trigger": dict(by_date_trigger[d]),
            "by_model_cost":   {k: round(v, 6) for k, v in by_date_model_cost[d].items()},
            "by_trigger_cost": {k: round(v, 6) for k, v in by_date_trigger_cost[d].items()},
            "top_sessions":    top_sessions_by_date.get(d, []),
        }
        for d in sorted(by_date_total)
    ]

    # Build by_model list (sorted by calls desc). Each row carries the
    # observed $/1k figures (input / output / blended) plus a
    # low_confidence flag for models below the min-sample threshold —
    # see _observed_per_1k. input_tokens / output_tokens are surfaced so
    # the UI can show volume alongside the rate.
    by_model_list = [
        {
            "model": key,
            "calls": by_model_calls[key],
            "cost": round(by_model_cost[key], 6),
            "auth_mode": by_model_auth.get(key, "unknown"),
            "color": _model_color(key),
            "cache_read": by_model_cache_read[key],
            "cache_write": by_model_cache_write[key],
            "input_tokens": by_model_input[key],
            "output_tokens": by_model_output[key],
            # Additive fields for the Model Economics lens (Cost page ignores
            # them). bot_count = how many distinct bots ran this model;
            # last_used_ts = most recent turn ts (ISO string) or None.
            "bot_count": len(by_model_instances[key]),
            "last_used_ts": by_model_last_ts.get(key),
            **_observed_per_1k(
                by_model_cost[key],
                by_model_input[key],
                by_model_output[key],
            ),
        }
        for key in sorted(by_model_calls, key=lambda k: -by_model_calls[k])
    ]

    # Build by_model_by_audience list (sorted by total cost desc — the
    # most useful order for spotting "premium models doing autonomous
    # work" outliers, which is the operator question this view answers).
    # Audience values: ``human`` = direct operator/user input; ``non_human``
    # = everything else (heartbeat, cron, subagent, forge, unknown).
    by_model_by_audience_list = [
        {
            "model":         key,
            "color":         _model_color(key),
            "total_calls":   by_model_calls[key],
            "total_cost":    round(by_model_cost[key], 6),
            "human": {
                "calls": by_model_audience_calls[key]["human"],
                "cost":  round(by_model_audience_cost[key]["human"], 6),
            },
            "non_human": {
                "calls": by_model_audience_calls[key]["non_human"],
                "cost":  round(by_model_audience_cost[key]["non_human"], 6),
            },
        }
        for key in sorted(
            by_model_calls, key=lambda k: -by_model_cost[k],
        )
    ]

    # Build by_channel list. Real conversations first (ranked by volume), each
    # carrying its rolled-up `threads` child list; then the named system
    # categories, flagged `system: True` so the UI groups them apart. Thread
    # legs are sorted by volume too. `threads` is [] for un-threaded channels.
    by_channel_list = []
    for ch in sorted(by_channel_calls, key=lambda k: -by_channel_calls[k]):
        threads = [
            {"thread_ts": ts, "calls": leg["calls"], "cost": round(leg["cost"], 6)}
            for ts, leg in sorted(
                by_channel_threads.get(ch, {}).items(),
                key=lambda kv: -kv[1]["calls"],
            )
        ]
        by_channel_list.append({
            "channel": ch,
            "calls": by_channel_calls[ch],
            "cost": round(by_channel_cost[ch], 6),
            "system": False,
            "threads": threads,
        })
    for cat in sorted(by_system_calls, key=lambda k: -by_system_calls[k]):
        by_channel_list.append({
            "channel": cat,
            "calls": by_system_calls[cat],
            "cost": round(by_system_cost[cat], 6),
            "system": True,
            "category": cat,
            "threads": [],
        })

    # Build by_source list
    by_source_list = [
        {
            "source": src,
            "calls": by_source_calls[src],
            "sessions": len(by_source_sessions[src]),
            "cost": round(by_source_cost[src], 6),
        }
        for src in sorted(by_source_calls, key=lambda k: -by_source_calls[k])
    ]

    # Build by_user list (top 20) — structured real-person rows. The raw
    # user_id is kept for debugging; `instance` is the bot this person most
    # interacts with (dominant by call count) and is the bot_id the route uses
    # for cache-only name resolution. The route enriches each row with a
    # display_name + a categorized fallback label (never a bare opaque id).
    by_user_list = []
    for (plat, uid), cnt in sorted(
        by_user_calls.items(), key=lambda x: -x[1],
    )[:20]:
        insts = by_user_instances.get((plat, uid)) or {}
        dominant = max(insts, key=lambda k: insts[k]) if insts else None
        by_user_list.append({
            "platform": plat,
            "user_id": uid,
            "instance": dominant,
            "calls": cnt,
        })

    # ── Context health (per-session cache analysis) ────────────────────────────
    # session_contexts is populated in the main loop above (consolidated
    # to avoid two passes over turns); we just compute aggregates here.
    sizes = sorted(s["max_cache_write"] for s in session_contexts.values() if s["max_cache_write"] > 0)
    total_cache_write = sum(int(t.get("cache_write_tokens") or 0) for t in turns)
    total_cache_read  = sum(int(t.get("cache_read_tokens")  or 0) for t in turns)
    cache_total = total_cache_read + total_cache_write

    def _pct_idx(lst: list, pct: float) -> int:
        return min(int(len(lst) * pct), len(lst) - 1)

    context_health: dict[str, Any] = {
        "session_count": len(session_contexts),
        "median_context": sizes[_pct_idx(sizes, 0.50)] if sizes else 0,
        "p75_context":    sizes[_pct_idx(sizes, 0.75)] if sizes else 0,
        "p95_context":    sizes[_pct_idx(sizes, 0.95)] if sizes else 0,
        "max_context":    sizes[-1] if sizes else 0,
        "over_100k_count": sum(1 for s in sizes if s > 100_000),
        "over_50k_count":  sum(1 for s in sizes if s > 50_000),
        "cache_efficiency_pct": round(100 * total_cache_read / cache_total) if cache_total > 0 else 0,
        "top_sessions": sorted(
            [
                {
                    "session_id": (k[:8] + "...") if k != "?" else "?",
                    "context": v["max_cache_write"],
                    "turns": v["turns"],
                    "cost": round(v["total_cost"], 4),
                    "date": v["date"],
                }
                for k, v in session_contexts.items()
            ],
            key=lambda x: -x["cost"],
        )[:10],
    }

    return {
        "total_turns": total_turns,
        "total_cost": round(total_cost, 6),
        "by_date": by_date_list,
        "by_model": by_model_list,
        "by_model_by_audience": by_model_by_audience_list,
        "by_channel": by_channel_list,
        "by_source": by_source_list,
        "by_user": by_user_list,
        "context_health": context_health,
        "billing": {
            "by_provider": by_provider,
            "unexpected_billing_turns": unexpected_billing_turns,
            "human_cost": round(human_cost, 6),
            "cron_cost": round(cron_cost, 6),
            "subagent_cost": round(subagent_cost, 6),
            "has_cost_data": has_cost,
        },
    }


def _empty_summary() -> dict:
    return {
        "total_turns": 0,
        "total_cost": 0.0,
        "by_date": [],
        "by_model": [],
        "by_model_by_audience": [],
        "by_channel": [],
        "by_source": [],
        "by_user": [],
        "context_health": {
            "session_count": 0,
            "median_context": 0, "p75_context": 0, "p95_context": 0, "max_context": 0,
            "over_100k_count": 0, "over_50k_count": 0,
            "cache_efficiency_pct": 0,
            "top_sessions": [],
        },
        "billing": {
            "by_provider": {},
            "unexpected_billing_turns": 0,
            "human_cost": 0.0, "cron_cost": 0.0, "subagent_cost": 0.0,
            "has_cost_data": False,
        },
    }
