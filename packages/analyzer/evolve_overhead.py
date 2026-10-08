"""evolve_overhead — what Evolve costs a bot, the target, and the breaker.

Brief ``evolve-overhead-ledger-and-budget``; decision D-OH5
(``internal/decision-evolve-overhead-2026-09-07.md``): *the overhead ledger is
the scoreboard, and Evolve's own breaker trips Evolve.*

Three things live here, one reader of the turn files behind all three:

1. **The number** (:func:`compute_bot`, :func:`compute_pod`). Per bot, per
   local day and over a rolling 7 days: Evolve's own model calls (the
   ``summarizer`` / ``classifier`` / ``task_extractor`` turns, by kind and by
   call site), the injected-context cost (footprint tokens x answering turns x
   the answering model's cached-input price — an ESTIMATE, labelled), and the
   sum as a share of the bot's total spend. Written to
   ``{shared}/evolve-overhead/ledger.json`` for the Cost page and the receipt.
2. **The breaker** (:func:`evaluate_hour`, :func:`run_cycle`). ``network.json``
   ``evolve.overhead``: ``share_max`` (0.05) and ``calls_per_user_turn_max``
   (1). Over either, for a bot, across a rolling hour, the **Evolve overhead
   breaker** trips — ``{shared}/breakers/<bot>/evolve_overhead.json``. The
   plugin reads that file in ``runPinnedSubagent`` and stops *Evolve's*
   machinery (the routing, judge, classifier and summariser calls). The bot
   keeps answering: this file is deliberately NOT a ``breakers.store`` type, so
   nothing that enforces a cost trip (heartbeat stash, exec passthrough, the
   conversation checkpoint) can ever see it and pause a conversation. The trip
   record names the caller in words — the top session key, the top prompt
   prefix ("Evolve's own routing calls"), the count — because on 2026-09-07 the
   breaker named the bot and the operator reactivated it twice.
   Reactivation is Evolve's: :func:`resume` clears the trip and records the
   moment, and the next evaluation counts from there.
3. **The hook-fire-rate signal** (:func:`hook_rate_check`).
   ``before_model_resolve`` fires per bot per hour against the bot's own 7-day
   median for that hour; over 10x is a "Needs you" Signal naming the top key
   and prefix — the early warning for any recursion, not only the known one.

What the breaker measures, and why it is not the whole number: the breaker
trips on Evolve's **model calls** (what tripping can remove). The injected
context is in the ledger's share but not the breaker's — a tripped breaker
cannot take tool schemas off a prompt, so counting them would trip a cheap bot
permanently and fix nothing.

Dollars come from the same resolver the cap uses (:mod:`turn_cost`), so OC's
own per-call ``usage.cost`` is the truth where it exists and an Evolve call OC
did not price is labelled ``estimate`` (``cost_source`` on every row).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PRODUCER = "evolve_overhead"
BREAKER_TYPE = "evolve_overhead"

#: Turn ``source`` / ``trigger_kind`` values that are Evolve's own model calls.
#: Pinned equal to ``context_health.EVOLVE_TRIGGER_KINDS`` by test.
EVOLVE_KINDS: frozenset[str] = frozenset({"summarizer", "classifier", "task_extractor"})

#: Plain words for each call site (``evolve_tag`` on the turn row), so a trip
#: record reads "Evolve's own routing calls", never "preflight".
TAG_WORDS: dict[str, str] = {
    "preflight": "Evolve's own routing calls",
    "tier-classifier": "Evolve's tier-classification calls",
    "session-judge": "Evolve's session-judging calls",
    "session-summary": "Evolve's session-summary calls",
}
KIND_WORDS: dict[str, str] = {
    "classifier": "Evolve's classifier calls",
    "summarizer": "Evolve's summarizer calls",
    "task_extractor": "Evolve's task-extraction calls",
}

#: What the breaker pauses, in the words the card uses. Pinned to the plugin's
#: gate (every ``runPinnedSubagent`` call site) — see subagentRun.ts.
PAUSED_WORDS: tuple[str, ...] = (
    "model routing (falls back to the rule and the bot's primary)",
    "session judges",
    "classifiers",
    "session summaries",
)

_ROUTER_PROMPT_PREFIX = "you are routing an ai request to the right model tier"
_INTERNAL_SENTINEL = "[evolve:internal-model-call]"


# ── Config ───────────────────────────────────────────────────────────────────


class OverheadConfig:
    """``network.json`` ``evolve.overhead`` with product defaults in code."""

    def __init__(self, raw: dict | None = None) -> None:
        raw = raw if isinstance(raw, dict) else {}

        def num(key: str, default: float, *, lo: float = 0.0) -> float:
            v = raw.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
                return default
            return float(v)

        #: Evolve's share of a bot's spend. ``0`` / absent -> the default.
        self.share_max = num("share_max", 0.05, lo=0.0) or 0.05
        self.calls_per_user_turn_max = num("calls_per_user_turn_max", 1.0, lo=0.0) or 1.0
        #: The rolling window the breaker judges, in minutes.
        self.window_minutes = int(num("window_minutes", 60.0, lo=5.0) or 60)
        #: Noise floors. A bot that made two classifier calls in an hour is not
        #: a loop however the ratio reads; these stop a quiet bot's tiny
        #: denominator from tripping a breaker nothing could fix.
        self.min_calls = int(num("min_calls", 5.0, lo=1.0) or 5)
        self.min_spend_usd = num("min_spend_usd", 0.10)
        #: How long a trip stands before the next evaluation may retry.
        self.breaker_ttl_hours = num("breaker_ttl_hours", 4.0, lo=0.25) or 4.0
        self.enabled = raw.get("enabled", True) is not False
        #: Hook-fire-rate signal: multiple of the 7-day same-hour median, and
        #: an absolute floor under it.
        self.hook_rate_multiple = num("hook_rate_multiple", 10.0, lo=1.0) or 10.0
        self.hook_rate_min_fires = int(num("hook_rate_min_fires", 30.0, lo=1.0) or 30)

    @classmethod
    def from_network(cls, network: dict | None) -> "OverheadConfig":
        evolve = (network or {}).get("evolve") if isinstance(network, dict) else None
        raw = evolve.get("overhead") if isinstance(evolve, dict) else None
        return cls(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "share_max": self.share_max,
            "calls_per_user_turn_max": self.calls_per_user_turn_max,
            "window_minutes": self.window_minutes,
            "min_calls": self.min_calls,
            "min_spend_usd": self.min_spend_usd,
            "breaker_ttl_hours": self.breaker_ttl_hours,
            "enabled": self.enabled,
        }


def bot_ids_of(network: dict | None) -> list[str]:
    """The pod's bots: ``bots`` keys (what the breaker runner iterates), else
    the legacy ``members`` list."""
    if not isinstance(network, dict):
        return []
    bots = network.get("bots")
    if isinstance(bots, dict) and bots:
        return sorted(bots)
    members = network.get("members")
    return sorted(m for m in members if isinstance(m, str)) if isinstance(members, list) else []


# ── Paths ────────────────────────────────────────────────────────────────────


def ledger_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / "evolve-overhead" / "ledger.json"


def breaker_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "breakers" / bot_id / f"{BREAKER_TYPE}.json"


def _accepted_path(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / "evolve-overhead" / "resumed" / f"{bot_id}.json"


# ── Classifying a turn ───────────────────────────────────────────────────────


def evolve_kind(turn: dict) -> str | None:
    """The Evolve call kind of a turn row, or ``None`` for a bot's own turn."""
    for key in ("source", "trigger_kind"):
        val = turn.get(key)
        if isinstance(val, str) and val.strip().lower() in EVOLVE_KINDS:
            return val.strip().lower()
    return None


def describe_tag(tag: str | None, kind: str | None = None) -> str:
    """Plain words for a call site (``evolve_tag``), falling back to its kind."""
    if tag and tag in TAG_WORDS:
        return TAG_WORDS[tag]
    if kind and kind in KIND_WORDS:
        return KIND_WORDS[kind]
    return "Evolve's own model calls"


def describe_prompt(prefix: str | None, key: str | None = None) -> str:
    """Words for the top ``before_model_resolve`` prompt prefix.

    Recognises Evolve's own prompts (the router's, anything carrying the
    internal-call sentinel, an ``evolve:<tag>`` key); a person's or a cron's
    prompt is quoted, never summarised — the card must not pretend to know.
    """
    p = (prefix or "").strip()
    low = p.lower()
    if low.startswith(_ROUTER_PROMPT_PREFIX):
        return TAG_WORDS["preflight"]
    if _INTERNAL_SENTINEL in p:
        return "an Evolve-internal prompt"
    if key:
        import re

        m = re.search(r"(?:^|:)evolve:([a-z0-9-]+)(?::|$)", key)
        if m and m.group(1) in TAG_WORDS:
            return TAG_WORDS[m.group(1)]
    if not p:
        return "an empty prompt"
    return f'a prompt starting "{p[:60]}"'


# ── Reading the inputs ───────────────────────────────────────────────────────


def _parse_ts(raw: Any) -> datetime | None:
    from spend_attribution import parse_ts  # type: ignore[import]

    return parse_ts(raw)


def load_turns(
    bot_id: str, *, days: int, now: datetime,
    log: Callable[[str], None] | None = None,
) -> list[dict] | None:
    """Live turn rows for ``days`` pod-local days ending ``now``, or ``None``.

    ``None`` is "I could not look" — never an empty bot. Loads ``days + 1``
    UTC files (a pod-local window spills into one extra file).
    """
    import live_spend  # type: ignore[import]

    turns = live_spend.load_live_turns(bot_id, days=days + 1, end=now, log=log)
    if turns is live_spend.LIVE_LOAD_FAILED or not isinstance(turns, list):
        return None
    return turns


def footprint_tokens(shared_dir: Path, bot_id: str) -> dict[str, int] | None:
    """Injected tool-schema tokens per answering turn, from the plugin's
    boot-time footprint (``turns/context-footprint.json``), or ``None``.

    ``chars / 4`` — the same conversion the plugin's own
    ``context footprint — N tools, … chars (~tok)`` log line prints, so the
    number on the Cost page is the number in the log. ``full`` is a user
    session; ``background`` is a heartbeat/cron session (the trimmed
    ``no_live_speaker`` profile when the footprint carries one).
    """
    for fp in (
        Path(shared_dir) / bot_id / "turns" / "context-footprint.json",
        Path(shared_dir) / bot_id / "context-footprint.json",
    ):
        try:
            data = json.loads(fp.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        total = data.get("total_chars")
        if not isinstance(total, (int, float)) or total < 0:
            continue
        profiles = data.get("profiles")
        trimmed = profiles.get("no_live_speaker") if isinstance(profiles, dict) else None
        bg = trimmed.get("total_chars") if isinstance(trimmed, dict) else None
        bg_chars = bg if isinstance(bg, (int, float)) and bg >= 0 else total
        return {"full": int(total // 4), "background": int(bg_chars // 4)}
    return None


_BACKGROUND_SOURCES = frozenset({"heartbeat", "cron", "cron_app", "scheduled", "subagent"})


def _is_background(turn: dict) -> bool:
    for key in ("source", "trigger_kind", "channel"):
        val = turn.get(key)
        if isinstance(val, str) and val.strip().lower() in _BACKGROUND_SOURCES | {"cron-event"}:
            return True
    return False


def _is_user_turn(turn: dict) -> bool:
    from spend_attribution import is_human_turn  # type: ignore[import]

    return is_human_turn(turn)


# ── The ledger ───────────────────────────────────────────────────────────────


def _blank_day(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "turns": 0,
        "user_turns": 0,
        "total_usd": 0.0,
        "unpriced_turns": 0,
        "evolve_calls": 0,
        "evolve_usd": 0.0,
        "evolve_priced": 0,
        "evolve_oc": 0,
        "by_kind": {},
        "context_turns": 0,
        "context_tokens": 0,
        "context_usd": 0.0,
        "context_unpriced_turns": 0,
        "attributed_usd": 0.0,
        "unattributed_usd": 0.0,
    }


def _context_rate_per_mtok(turn: dict, cache: dict, catalog: dict | None) -> float | None:
    """The answering model's cached-input price ($/MTok), memoised by model."""
    from turn_cost import estimate_dimensions  # type: ignore[import]

    key = (turn.get("provider"), turn.get("model"))
    if key not in cache:
        probe = {
            "model": turn.get("model"), "provider": turn.get("provider"),
            "cache_read_tokens": 1_000_000,
        }
        try:
            dims = estimate_dimensions(probe, catalog=catalog)
        except Exception:  # noqa: BLE001 — an unpriceable model is "unknown"
            dims = None
        cache[key] = dims["cache_read"] if dims else None
    return cache[key]


def build_days(
    turns: list[dict],
    *,
    days: int,
    now: datetime,
    fp_tokens: dict[str, int] | None,
    shared_dir: Path | None = None,
    role_of: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """One row per pod-local day (oldest first), ``days`` of them ending at
    the local day containing ``now``. Days with no turns are still rows — a
    zero row is a real answer ("the bot was idle"), distinct from a failed
    read, which never reaches this function."""
    import live_spend  # type: ignore[import]
    from spend_attribution import is_attributed  # type: ignore[import]
    from turn_cost import TRUTH_RESOLUTIONS, load_pricing_catalog, turn_cost_detail  # type: ignore[import]

    tz = live_spend.pod_tz_or_local()
    last = now.astimezone(tz).date()
    rows: dict[str, dict[str, Any]] = {
        (last - timedelta(days=i)).isoformat(): _blank_day((last - timedelta(days=i)).isoformat())
        for i in range(days - 1, -1, -1)
    }
    catalog = load_pricing_catalog(shared_dir)
    rate_cache: dict = {}
    for t in turns:
        d = live_spend.local_day_iso(t.get("ts"), tz)
        row = rows.get(d) if d else None
        if row is None:
            continue
        cost, resolution = turn_cost_detail(t, catalog=catalog)
        kind = evolve_kind(t)
        row["turns"] += 1
        if cost is None:
            row["unpriced_turns"] += 1
        else:
            row["total_usd"] += cost
        if kind is not None:
            row["evolve_calls"] += 1
            bucket = row["by_kind"].setdefault(kind, {"calls": 0, "usd": 0.0, "tags": {}})
            bucket["calls"] += 1
            tag = t.get("evolve_tag") if isinstance(t.get("evolve_tag"), str) else None
            tag_b = bucket["tags"].setdefault(tag or "unknown", {"calls": 0, "usd": 0.0})
            tag_b["calls"] += 1
            if cost is not None:
                row["evolve_usd"] += cost
                row["evolve_priced"] += 1
                bucket["usd"] += cost
                tag_b["usd"] += cost
                if resolution in TRUTH_RESOLUTIONS:
                    row["evolve_oc"] += 1
            continue
        if cost is not None:
            if is_attributed(t, role_of):
                row["attributed_usd"] += cost
            else:
                row["unattributed_usd"] += cost
        if _is_user_turn(t):
            row["user_turns"] += 1
        # Injected context rides every ANSWERING turn (Evolve's own calls are
        # already counted in full above).
        if fp_tokens:
            tokens = fp_tokens["background" if _is_background(t) else "full"]
            rate = _context_rate_per_mtok(t, rate_cache, catalog)
            row["context_turns"] += 1
            row["context_tokens"] += tokens
            if rate is None:
                row["context_unpriced_turns"] += 1
            else:
                row["context_usd"] += tokens * rate / 1_000_000
    return [rows[k] for k in sorted(rows)]


def _source_label(priced: int, oc: int) -> str | None:
    if priced == 0:
        return None
    if oc == priced:
        return "oc"
    return "estimate" if oc == 0 else "mixed"


def summarize(day_rows: Iterable[dict[str, Any]], *, share_max: float) -> dict[str, Any]:
    """Fold day rows into one window: the numbers the card and receipt show."""
    out = _blank_day("window")
    out.pop("day")
    priced = oc = 0
    for r in day_rows:
        for k in ("turns", "user_turns", "total_usd", "unpriced_turns", "evolve_calls",
                  "evolve_usd", "context_turns", "context_tokens", "context_usd",
                  "context_unpriced_turns", "attributed_usd", "unattributed_usd"):
            out[k] += r[k]
        priced += r["evolve_priced"]
        oc += r["evolve_oc"]
        for kind, b in r["by_kind"].items():
            ob = out["by_kind"].setdefault(kind, {"calls": 0, "usd": 0.0, "tags": {}})
            ob["calls"] += b["calls"]
            ob["usd"] += b["usd"]
            for tag, tb in b["tags"].items():
                ot = ob["tags"].setdefault(tag, {"calls": 0, "usd": 0.0})
                ot["calls"] += tb["calls"]
                ot["usd"] += tb["usd"]
    out["evolve_priced"], out["evolve_oc"] = priced, oc
    overhead = out["evolve_usd"] + out["context_usd"]
    total = out["total_usd"]
    out["overhead_usd"] = overhead
    out["share"] = (overhead / total) if total > 0 else None
    out["model_share"] = (out["evolve_usd"] / total) if total > 0 else None
    out["calls_per_user_turn"] = (
        out["evolve_calls"] / out["user_turns"] if out["user_turns"] else None
    )
    out["over_target"] = bool(out["share"] is not None and out["share"] > share_max)
    #: ``oc`` / ``estimate`` / ``mixed`` for the model-call dollars. The
    #: injected-context dollars are ALWAYS an estimate and say so separately.
    out["evolve_cost_source"] = _source_label(priced, oc)
    out["context_cost_source"] = "estimate"
    out["measurable"] = out["unpriced_turns"] == 0
    for k in ("total_usd", "evolve_usd", "context_usd", "overhead_usd",
              "attributed_usd", "unattributed_usd"):
        out[k] = round(out[k], 6)
    for b in out["by_kind"].values():
        b["usd"] = round(b["usd"], 6)
        for tb in b["tags"].values():
            tb["usd"] = round(tb["usd"], 6)
    if total > 0:
        out["share"] = round(overhead / total, 6)
        out["model_share"] = round(out["evolve_usd"] / total, 6)
    return out


def compute_bot(
    shared_dir: Path,
    bot_id: str,
    *,
    now: datetime | None = None,
    cfg: OverheadConfig | None = None,
    turns: list[dict] | None = None,
    role_of: Callable[[str], str | None] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """One bot's ledger: seven day rows, today (``d1``) and the rolling 7 days
    (``d7``). ``None`` when the turn files could not be read — the ledger says
    "could not look", never a confident zero."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = cfg or OverheadConfig()
    if turns is None:
        turns = load_turns(bot_id, days=7, now=now, log=log)
        if turns is None:
            return None
    fp = footprint_tokens(shared_dir, bot_id)
    day_rows = build_days(
        turns, days=7, now=now, fp_tokens=fp, shared_dir=shared_dir, role_of=role_of,
    )
    d7 = summarize(day_rows, share_max=cfg.share_max)
    d1 = summarize(day_rows[-1:], share_max=cfg.share_max)
    for r in day_rows:
        for k in ("total_usd", "evolve_usd", "context_usd", "attributed_usd", "unattributed_usd"):
            r[k] = round(r[k], 6)
        for b in r["by_kind"].values():
            b["usd"] = round(b["usd"], 6)
            for tb in b["tags"].values():
                tb["usd"] = round(tb["usd"], 6)
    return {
        "bot_id": bot_id,
        "days": day_rows,
        "d1": d1,
        "d7": d7,
        "footprint_tokens": fp,
    }


def compute_pod(bots: dict[str, dict[str, Any]], *, share_max: float) -> dict[str, Any]:
    """The same window over every bot that could be read, summed."""
    day_rows: list[dict[str, Any]] = []
    for b in bots.values():
        day_rows.extend(b["days"])
    d7 = summarize(day_rows, share_max=share_max)
    today = {}
    if bots:
        last_day = max(r["day"] for b in bots.values() for r in b["days"])
        d1 = summarize((r for r in day_rows if r["day"] == last_day), share_max=share_max)
        today = d1
    return {"d7": d7, "d1": today}


def build_ledger(
    shared_dir: Path,
    bot_ids: Iterable[str],
    *,
    now: datetime | None = None,
    cfg: OverheadConfig | None = None,
    turns_by_bot: dict[str, list[dict]] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = cfg or OverheadConfig()
    bots: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    for bot_id in bot_ids:
        try:
            entry = compute_bot(
                shared_dir, bot_id, now=now, cfg=cfg, log=log,
                turns=(turns_by_bot or {}).get(bot_id),
            )
        except Exception as exc:  # noqa: BLE001 — one bot never sinks the ledger
            if log:
                log(f"[evolve_overhead] {bot_id}: ledger raised {exc}")
            entry = None
        if entry is None:
            unreadable.append(bot_id)
        else:
            bots[bot_id] = entry
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "config": cfg.as_dict(),
        "bots": bots,
        "unreadable_bots": unreadable,
        "pod": compute_pod(bots, share_max=cfg.share_max),
    }


def write_ledger(shared_dir: Path, ledger: dict[str, Any]) -> None:
    from evolve_util import atomic_write_json  # type: ignore[import]

    p = ledger_path(shared_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, ledger, indent=None, mode=0o644)


def load_ledger(shared_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(ledger_path(shared_dir).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("bots"), dict) else None


# ── The breaker ──────────────────────────────────────────────────────────────


def read_breaker(shared_dir: Path, bot_id: str, *, now: datetime | None = None) -> dict | None:
    """The bot's overhead trip record, or ``None`` when absent / expired.

    An unparseable file reads as tripped with ``unreadable: true`` — the
    plugin's gate does the same (a breaker that can only remove Evolve's own
    machinery fails toward removing it).
    """
    p = breaker_path(shared_dir, bot_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return {"bot_id": bot_id, "type": BREAKER_TYPE, "unreadable": True,
                "reason": "trip record unreadable", "detail": {}}
    exp = _parse_ts(data.get("expires_at"))
    if exp is not None and (now or datetime.now(timezone.utc)) >= exp:
        return None
    return data


def read_resumed_at(shared_dir: Path, bot_id: str) -> datetime | None:
    try:
        data = json.loads(_accepted_path(shared_dir, bot_id).read_text())
    except (OSError, ValueError):
        return None
    return _parse_ts(data.get("accepted_at")) if isinstance(data, dict) else None


def evaluate_hour(
    turns: list[dict],
    *,
    now: datetime,
    cfg: OverheadConfig,
    since: datetime | None = None,
) -> dict[str, Any]:
    """The breaker's verdict for one bot over the rolling window. Pure.

    ``since`` is the operator's "count from now" instant: the window never
    reaches back before it, so a resumed bot is not re-judged on the calls
    that tripped it.
    """
    from spend_attribution import window_start  # type: ignore[import]
    from turn_cost import load_pricing_catalog, turn_cost_detail  # type: ignore[import]

    start = window_start(now, 1, since)
    start = max(start, now - timedelta(minutes=cfg.window_minutes))
    catalog = load_pricing_catalog(None)
    total = evolve_usd = 0.0
    calls = users = 0
    by_key: dict[str, dict[str, Any]] = {}
    for t in turns:
        ts = _parse_ts(t.get("ts"))
        if ts is None or ts < start or ts > now:
            continue
        cost, _res = turn_cost_detail(t, catalog=catalog)
        if cost is not None:
            total += cost
        kind = evolve_kind(t)
        if kind is None:
            if _is_user_turn(t):
                users += 1
            continue
        calls += 1
        if cost is not None:
            evolve_usd += cost
        tag = t.get("evolve_tag") if isinstance(t.get("evolve_tag"), str) else None
        raw_key = t.get("evolve_key")
        key: str = (raw_key if isinstance(raw_key, str) and raw_key
                    else f"evolve:{tag}" if tag else str(t.get("session_id") or "unknown"))
        b = by_key.setdefault(key, {"n": 0, "tag": tag, "kind": kind})
        b["n"] += 1
    share = (evolve_usd / total) if total > 0 else None
    ratio = calls / max(users, 1)
    reasons: list[str] = []
    if calls >= cfg.min_calls:
        if share is not None and total >= cfg.min_spend_usd and share > cfg.share_max:
            reasons.append("share")
        if ratio > cfg.calls_per_user_turn_max:
            reasons.append("calls_per_user_turn")
    top_key = max(by_key, key=lambda k: by_key[k]["n"]) if by_key else None
    top = by_key.get(top_key) if top_key else None
    return {
        "trip": bool(reasons) and cfg.enabled,
        "reasons": reasons,
        "window_start": start.isoformat(),
        "window_minutes": cfg.window_minutes,
        "total_usd": round(total, 6),
        "evolve_usd": round(evolve_usd, 6),
        "share": None if share is None else round(share, 6),
        "evolve_calls": calls,
        "user_turns": users,
        "calls_per_user_turn": round(ratio, 3),
        "top_session_key": top_key,
        "top_count": top["n"] if top else 0,
        "top_prefix_words": describe_tag(top["tag"], top["kind"]) if top else None,
        "top_tag": top["tag"] if top else None,
    }


def _reason_sentence(v: dict[str, Any], cfg: OverheadConfig) -> str:
    words = v.get("top_prefix_words") or "Evolve's own model calls"
    mins = v["window_minutes"]
    span = "the last hour" if mins == 60 else f"the last {mins} minutes"
    users = v["user_turns"]
    msgs = (f"for {users} message{'s' if users != 1 else ''} to the bot"
            if users else "with no message to the bot")
    parts = [f"{words}: {v['evolve_calls']} calls in {span} {msgs}"]
    if "calls_per_user_turn" in v["reasons"]:
        parts.append(f"{v['calls_per_user_turn']:g} per message against a target of "
                     f"{cfg.calls_per_user_turn_max:g}")
    if "share" in v["reasons"] and v["share"] is not None:
        parts.append(f"{v['share'] * 100:.0f}% of the bot's spend against a target of "
                     f"{cfg.share_max * 100:.0f}%")
    return "; ".join(parts)


def trip(
    shared_dir: Path, bot_id: str, verdict: dict[str, Any], cfg: OverheadConfig,
    *, now: datetime,
) -> dict[str, Any]:
    """Write the trip record. Atomic; ``breakers/<bot>/`` is evolve-owned."""
    from evolve_util import atomic_write_json  # type: ignore[import]

    record = {
        "bot_id": bot_id,
        "type": BREAKER_TYPE,
        "state": "tripped",
        "tripped_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=cfg.breaker_ttl_hours)).isoformat(),
        "initiated_by": "auto",
        "trip_id": str(uuid.uuid4()),
        "reason": _reason_sentence(verdict, cfg),
        "detail": {
            **{k: verdict[k] for k in (
                "reasons", "window_minutes", "evolve_calls", "user_turns",
                "calls_per_user_turn", "share", "evolve_usd", "total_usd",
                "top_session_key", "top_count", "top_prefix_words", "top_tag")},
            "share_max": cfg.share_max,
            "calls_per_user_turn_max": cfg.calls_per_user_turn_max,
            "paused": list(PAUSED_WORDS),
            "bot_keeps_answering": True,
        },
    }
    p = breaker_path(shared_dir, bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, record, sort_keys=True, mode=0o644)
    return record


def resume(
    shared_dir: Path, bot_id: str, *, by: str, reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resume Evolve's machinery for ``bot_id`` and count from now.

    Clears the trip and stamps the instant; :func:`evaluate_hour` never looks
    before it, so the calls that tripped the breaker are not judged again.
    Returns what was accepted, for the card ("accepted N calls at HH:MM").
    """
    from evolve_util import atomic_write_json  # type: ignore[import]

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    prior = read_breaker(shared_dir, bot_id, now=now)
    accepted = {
        "bot_id": bot_id,
        "accepted_at": now.isoformat(),
        "by": by,
        "reason": reason,
        "was_tripped": prior is not None,
        "accepted_calls": ((prior or {}).get("detail") or {}).get("evolve_calls"),
        "accepted_reason": (prior or {}).get("reason"),
    }
    p = _accepted_path(shared_dir, bot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, accepted, mode=0o644)
    breaker_path(shared_dir, bot_id).unlink(missing_ok=True)
    return accepted


def card_for(shared_dir: Path, bot_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The dashboard card for one bot: tripped state in words, or the last
    resume. Plain data — the page renders it, nothing re-derives it."""
    now = now or datetime.now(timezone.utc)
    rec = read_breaker(shared_dir, bot_id, now=now)
    resumed = read_resumed_at(shared_dir, bot_id)
    card: dict[str, Any] = {"tripped": rec is not None, "resumed_at": None}
    if resumed is not None:
        card["resumed_at"] = resumed.isoformat()
    if rec is None:
        return card
    detail = rec.get("detail") or {}
    card.update({
        "tripped_at": rec.get("tripped_at"),
        "expires_at": rec.get("expires_at"),
        "unreadable": bool(rec.get("unreadable")),
        "headline": "Evolve's own machinery is paused for this bot — the bot is still answering.",
        "reason": rec.get("reason"),
        "paused": detail.get("paused") or list(PAUSED_WORDS),
        "top_session_key": detail.get("top_session_key"),
        "top_prefix_words": detail.get("top_prefix_words"),
        "top_count": detail.get("top_count"),
        "evolve_calls": detail.get("evolve_calls"),
        "resume_hint": ("Resuming turns Evolve's routing, judges and summaries back on "
                        "and counts from that moment — it does not re-judge the calls "
                        "that tripped this."),
    })
    return card


# ── The hook-fire-rate signal ────────────────────────────────────────────────


def _read_fire_day(shared_dir: Path, bot_id: str, day: date) -> dict | None:
    p = Path(shared_dir) / bot_id / "turns" / f"hook-fires-{day.isoformat()}.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("hours"), dict) else None


def hook_rate_check(
    shared_dir: Path, bot_id: str, *, now: datetime, cfg: OverheadConfig,
) -> dict[str, Any] | None:
    """Is this hour's ``before_model_resolve`` rate past ``multiple`` x the
    bot's 7-day median for the same UTC hour? ``None`` when it is not, or
    when there is no baseline to compare against (fewer than 3 prior days of
    ledger — a new bot has no "normal" yet; the breaker covers it)."""
    now = now.astimezone(timezone.utc)
    hh = f"{now.hour:02d}"
    today = _read_fire_day(shared_dir, bot_id, now.date())
    cur = ((today or {}).get("hours") or {}).get(hh)
    if not isinstance(cur, dict):
        return None
    count = int(cur.get("count") or 0)
    base: list[int] = []
    for i in range(1, 8):
        day = _read_fire_day(shared_dir, bot_id, now.date() - timedelta(days=i))
        if day is None:
            continue
        h = (day.get("hours") or {}).get(hh)
        base.append(int(h.get("count") or 0) if isinstance(h, dict) else 0)
    if len(base) < 3:
        return None
    median = statistics.median(base)
    threshold = max(cfg.hook_rate_multiple * max(median, 1.0), float(cfg.hook_rate_min_fires))
    if count <= threshold:
        return None
    raw_keys = cur.get("keys")
    keys: dict[str, Any] = raw_keys if isinstance(raw_keys, dict) else {}
    top_key = max(keys, key=lambda k: int((keys[k] or {}).get("n") or 0)) if keys else None
    top = keys[top_key] if top_key is not None else None
    prefix = top.get("prefix") if isinstance(top, dict) else None
    return {
        "bot_id": bot_id,
        "hour_utc": f"{now.date().isoformat()}T{hh}",
        "count": count,
        "median": median,
        "multiple": round(count / max(median, 1.0), 1),
        "top_session_key": top_key,
        "top_count": int((top or {}).get("n") or 0) if isinstance(top, dict) else 0,
        "top_prefix": prefix,
        "top_prefix_words": describe_prompt(prefix, top_key),
    }


def emit_hook_rate_signal(shared_dir: Path, finding: dict[str, Any]) -> str | None:
    """Raise the "Needs you" line for a hook-fire-rate finding. Returns the
    signature kept (for the sweep), or ``None`` if the store was unreachable."""
    bot = finding["bot_id"]
    sig = f"{PRODUCER}:hook_fire_rate:{bot}/{finding['hour_utc']}"
    try:
        from signals import store as signals_store  # type: ignore[import]

        signals_store.observe(
            shared_dir,
            signature=sig, producer=PRODUCER, type="evolve_hook_fire_rate",
            flavor="maintenance", severity="alert", scope="bot", bot_id=bot,
            title=f"{bot}: model-routing hook firing {finding['multiple']:g}x its usual rate",
            body=(
                f"{bot}'s model-routing hook fired {finding['count']} times this hour; "
                f"its usual for this hour is {finding['median']:g}. Most of it came from "
                f"{finding['top_prefix_words']} ({finding['top_count']} fires, session "
                f"{finding['top_session_key']}). A rate like this is what a recursion "
                f"looks like before the spend does."
            ),
            details={
                **finding, "vector": "cost", "magnitude": 3,
                "what_it_means": (
                    "Something is making the bot's model-routing hook fire far more "
                    "often than it normally does. The session key and prompt above "
                    "name the caller."
                ),
            },
        )
        return sig
    except Exception:  # noqa: BLE001 — a signal write never sinks the cycle
        return None


# ── The cycle ────────────────────────────────────────────────────────────────

#: The ledger is recomputed at most this often (the breaker check runs every
#: tick; reading eight days of turns for every bot does not need to).
LEDGER_REFRESH_SECONDS = 30 * 60


def _ledger_is_fresh(shared_dir: Path, now: datetime) -> bool:
    try:
        age = now.timestamp() - ledger_path(shared_dir).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < LEDGER_REFRESH_SECONDS


def run_cycle(
    shared_dir: Path,
    network: dict,
    *,
    now: datetime | None = None,
    force_ledger: bool = False,
    turns_by_bot: dict[str, list[dict]] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One tick: judge every bot's last hour, trip/expire breakers, raise the
    hook-rate signal, refresh the ledger. Never raises into the caller — the
    breaker runner's own cycle must not depend on this one."""
    emit = log or (lambda _m: None)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = OverheadConfig.from_network(network)
    result: dict[str, Any] = {"tripped": [], "expired": [], "hook_rate": [], "ledger_written": False}
    if not cfg.enabled:
        return result
    bots = bot_ids_of(network)
    kept: set[str] = set()
    for bot_id in bots:
        try:
            # Expire a stale trip so the next evaluation may retry.
            p = breaker_path(shared_dir, bot_id)
            if p.is_file() and read_breaker(shared_dir, bot_id, now=now) is None:
                p.unlink()
                result["expired"].append(bot_id)
            elif p.is_file():
                continue  # tripped and live — don't churn it, don't re-judge it
            turns = (turns_by_bot or {}).get(bot_id)
            if turns is None:
                turns = load_turns(bot_id, days=1, now=now, log=emit)
            if turns is not None:
                v = evaluate_hour(
                    turns, now=now, cfg=cfg,
                    since=read_resumed_at(shared_dir, bot_id),
                )
                if v["trip"]:
                    rec = trip(shared_dir, bot_id, v, cfg, now=now)
                    result["tripped"].append(bot_id)
                    emit(f"[evolve_overhead] TRIPPED {bot_id}: {rec['reason']}")
                    sig = _emit_trip_signal(shared_dir, rec)
                    if sig:
                        kept.add(sig)
            finding = hook_rate_check(shared_dir, bot_id, now=now, cfg=cfg)
            if finding is not None:
                result["hook_rate"].append(finding)
                sig = emit_hook_rate_signal(shared_dir, finding)
                if sig:
                    kept.add(sig)
        except Exception as exc:  # noqa: BLE001 — one bot never sinks the tick
            emit(f"[evolve_overhead] {bot_id}: cycle raised {exc}")
    # Standing trips keep their Signal; everything else this producer raised
    # that is no longer true resolves.
    for bot_id in bots:
        rec = read_breaker(shared_dir, bot_id, now=now)
        if rec is not None and not rec.get("unreadable"):
            kept.add(f"{PRODUCER}:breaker:{bot_id}/{rec.get('trip_id')}")
    try:
        from signals import store as signals_store  # type: ignore[import]

        signals_store.sweep_resolve(shared_dir, producer=PRODUCER, kept_signatures=kept)
    except Exception as exc:  # noqa: BLE001
        emit(f"[evolve_overhead] signal sweep failed: {exc}")
    try:
        if force_ledger or not _ledger_is_fresh(shared_dir, now):
            write_ledger(shared_dir, build_ledger(
                shared_dir, bots, now=now, cfg=cfg, turns_by_bot=None, log=emit,
            ))
            result["ledger_written"] = True
    except Exception as exc:  # noqa: BLE001
        emit(f"[evolve_overhead] ledger write failed: {exc}")
    return result


def _emit_trip_signal(shared_dir: Path, rec: dict[str, Any]) -> str | None:
    bot = rec["bot_id"]
    sig = f"{PRODUCER}:breaker:{bot}/{rec['trip_id']}"
    d = rec.get("detail") or {}
    try:
        from signals import store as signals_store  # type: ignore[import]

        signals_store.observe(
            shared_dir,
            signature=sig, producer=PRODUCER, type="evolve_overhead_breaker",
            flavor="maintenance", severity="alert", scope="bot", bot_id=bot,
            title=f"{bot}: Evolve's own machinery paused — the bot is still answering",
            body=(f"{rec['reason']}. Evolve paused its routing, judges, classifiers and "
                  f"summaries for {bot}; the bot's conversations are untouched. Top caller: "
                  f"{d.get('top_prefix_words')} (session {d.get('top_session_key')}). "
                  f"Resume from the Cost page when the cause is understood."),
            details={**d, "trip_id": rec["trip_id"], "vector": "cost", "magnitude": 3,
                     "what_it_means": "Evolve's own model calls ran past their target for "
                                      "this bot; Evolve stopped them. Nothing here paused the bot."},
        )
        return sig
    except Exception:  # noqa: BLE001
        return None


# ── The weekly receipt line ──────────────────────────────────────────────────


def receipt_lines(
    shared_dir: Path, members: Iterable[str], *, now: datetime | None = None,
    network: dict | None = None, log: Callable[[str], None] | None = None,
) -> list[str]:
    """One line for the weekly receipt: the pod's Evolve overhead, rolling 7
    days, as a share of spend, against the target. Computed fresh (the receipt
    is weekly); falls back to the last ledger; says "not measured" otherwise —
    never a zero."""
    now = now or datetime.now(timezone.utc)
    cfg = OverheadConfig.from_network(network)
    ledger: dict | None = None
    try:
        ledger = build_ledger(shared_dir, list(members), now=now, cfg=cfg, log=log)
        if not ledger["bots"]:
            ledger = None
    except Exception:  # noqa: BLE001
        ledger = None
    if ledger is None:
        ledger = load_ledger(shared_dir)
    if ledger is None:
        return ["  Evolve's own overhead: not measured this week (turn files unreadable)"]
    w = (ledger.get("pod") or {}).get("d7") or {}
    if w.get("share") is None:
        return ["  Evolve's own overhead: no spend this week"]
    flag = " — over target" if w.get("over_target") else ""
    worst = max(
        ((b["d7"].get("share") or 0.0, bot) for bot, b in ledger["bots"].items()),
        default=(0.0, ""),
    )
    worst_txt = (f"; highest: {worst[1]} at {worst[0] * 100:.1f}%"
                 if worst[1] and worst[0] > cfg.share_max else "")
    return [
        f"  Evolve's own overhead: ${w['overhead_usd']:.2f} = {w['share'] * 100:.1f}% of "
        f"${w['total_usd']:.2f} (target ≤{cfg.share_max * 100:.0f}%){flag}"
        f"{worst_txt} — its own model calls ${w['evolve_usd']:.2f} + injected context "
        f"~${w['context_usd']:.2f} (estimate)"
    ]


# ── Rendering / CLI ──────────────────────────────────────────────────────────


def render_table(ledger: dict[str, Any]) -> str:
    """The overhead table, one row per bot then the pod — the PR body's table
    and what ``--table`` prints."""
    cols = ("bot", "total", "evolve calls", "calls $", "context ~$", "overhead", "share",
            "calls/msg")
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]

    def row(name: str, w: dict[str, Any]) -> str:
        share = "n/a" if w.get("share") is None else f"{w['share'] * 100:.1f}%"
        cpu = w.get("calls_per_user_turn")
        return ("| " + " | ".join([
            name, f"${w['total_usd']:.2f}", str(w["evolve_calls"]),
            f"${w['evolve_usd']:.2f}", f"${w['context_usd']:.2f}",
            f"${w['overhead_usd']:.2f}", share + (" ⚠" if w.get("over_target") else ""),
            "n/a" if cpu is None else f"{cpu:.2f}",
        ]) + " |")

    for bot, b in sorted(ledger["bots"].items()):
        lines.append(row(bot, b["d7"]))
    lines.append(row("**pod**", ledger["pod"]["d7"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evolve overhead ledger")
    ap.add_argument("--shared-dir", required=True)
    ap.add_argument("--network", default=None)
    ap.add_argument("--table", action="store_true", help="print the 7-day table")
    ap.add_argument("--write", action="store_true", help="write ledger.json")
    args = ap.parse_args(argv)
    shared = Path(args.shared_dir)
    net_path = Path(args.network) if args.network else shared / "network.json"
    network = json.loads(net_path.read_text())
    cfg = OverheadConfig.from_network(network)
    ledger = build_ledger(shared, bot_ids_of(network), cfg=cfg,
                          log=lambda m: print(m, file=sys.stderr))
    if args.write:
        write_ledger(shared, ledger)
    print(render_table(ledger) if args.table else json.dumps(ledger, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
