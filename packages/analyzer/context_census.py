"""Context-efficiency census — where a bot's input tokens actually come from.

Brief: internal/dispatch/done/context-efficiency-census.md. Design context:
internal/design-pa-context-economy-2026-08-31.md (the levers whose ORDER this
census grounds) and internal/overview-cost-spikes-2026-08-31.md (the $18
evening). Instruments it RIDES rather than re-measures:

  * spec-context-observability-2026-07-30.md — the prefix-hash ledger
    (``{shared_dir}/{bot}/turns/prefix-hashes-*.jsonl``, one record per
    ``before_prompt_build`` with per-block shas) and its cold-miss definition
    (``context_health``: cache_read == 0 and cache_write above a floor,
    excluding each session's expected-cold first call);
  * spec-evolve-overhead-budget-2026-07-31.md — the boot-time tool footprint
    (``{shared_dir}/{bot}/turns/context-footprint.json``, per registered
    Evolve tool: schema chars);
  * OpenClaw's own per-session record (verified live 2026-09-01 on an OC
    2026.7 gateway, shape only):
      - ``<home>/.openclaw/agents/<agent>/sessions/<uuid>.jsonl`` — the
        transcript: ``message`` records whose ``message.role`` is ``user`` /
        ``assistant`` / ``toolResult``; assistant messages carry
        ``usage: {input, output, cacheRead, cacheWrite, totalTokens, cost}``,
        ``model``, ``provider``, ``stopReason``; content blocks are ``text``,
        ``thinking``, ``toolCall {id, name, arguments}``; tool results are
        their own ``toolResult`` messages (``toolName``, ``content``);
        ``compaction`` records carry ``summary`` + ``firstKeptEntryId``;
      - ``<uuid>.trajectory.jsonl`` — OC's per-run trace: ``context.compiled``
        (the actual system prompt string + the tool definitions sent) and
        ``trace.artifacts`` (``promptCache.retention``);
      - ``sessions.json`` — the session index (channel, chat type, and a
        ``systemPromptReport`` with system-prompt / tool-schema char counts).

What it reports (one bot, trailing N days, READ-ONLY):
  1. History composition per call — input tokens split into fixed prefix /
     tool descriptions / conversation, and conversation into user text /
     assistant text / assistant thinking / tool-call arguments / tool results
     (per tool) / compaction summaries; plus the share of tool-result tokens
     re-carried after the call that consumed them.
  2. Cache — cache-read vs full-price vs cache-write tokens where the provider
     reports them; hit rate; and for each cold miss WHICH prefix block changed
     (the cache-break cause table), or the other causes the spec names
     (model swap, cache window elapsed, compaction), or "unexplained".
  3. Re-emission — assistant outputs of at least K tokens whose content (or a
     near-duplicate) recurs within a session: the document written N times.
  4. Session shape — turns / calls / tool calls per session, the longest
     unrotated session, and the power-rung share of mechanical calls.
  5. The money line — the costliest session replayed arithmetically under
     each lever's assumption, stated as "would have cost about $X instead of
     $Y under stated assumptions", never as a promise.

Token splits are ESTIMATES: the provider reports one total per call; the
split is by character share of what was in the context, anchored so the
pieces sum exactly to the reported total. Anything not measured says so.

Usage (read-only; the only write is an optional ``--json-out`` file)::

    python3 context_census.py --bot personal-bot --days 3
    python3 context_census.py --bot personal-bot --days 3 --json-out /tmp/census.json
    python3 context_census.py --sessions-dir <dir> --shared-dir <dir> --bot <id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platform_profile import get_profile
from session_kinds import ALL_KINDS, KIND_UNINDEXED, classify_index_entry

CENSUS_SCHEMA_VERSION = 1

#: Rough chars-per-token for English/markdown — the same convention as
#: context_health.BYTES_PER_TOKEN (the post-mortem's hand probe).
CHARS_PER_TOKEN = 4

#: The Phase 0 cold-miss floor (context_health.DEFAULT_PREFIX_FLOOR_TOKENS).
DEFAULT_PREFIX_FLOOR_TOKENS = 1000

#: Default "document" threshold for the re-emission detector, in tokens.
DEFAULT_MIN_DOC_TOKENS = 300

#: Shingle size + Jaccard bar for near-duplicate documents.
SHINGLE_WORDS = 6
NEAR_DUP_JACCARD = 0.5

#: Provider prompt-cache windows, minutes. OC's ``promptCache.retention``
#: is ``short`` (5 min) or ``long`` (1 h) on Anthropic.
CACHE_TTL_MINUTES = {"short": 5, "long": 60}
DEFAULT_CACHE_TTL_MINUTES = 5

#: How far back the prefix-hash ledger is read so a whole session can pair
#: even when it started before the window.
LEDGER_MAX_DAYS = 60

BLOCK_NAMES = ("capabilities", "digest", "narrative", "speaker", "cost_downgrade")

_SESSION_FILE_RE = re.compile(r"^[0-9a-f-]{36}\.jsonl$")

#: Sentinel: a file exists but could not be read/parsed. Never collapses
#: to [] (spec §Failure posture — unreadable is not clean).
UNREADABLE = object()


# ── Plain-English sentences (readability-gated; see tests) ────────────────────
#: Every prose sentence the one-pager can say. Slots are filled at render
#: time; ``tests/test_context_census.py`` runs each template through
#: ``dossier.readability.check`` so a sentence that reaches for jargon
#: fails the build, not the reader.
SENTENCES = {
    "intro": "This is a census of {bot} over the last {days} days. It reads {calls} model calls in {sessions} sessions.",
    "no_calls": "No model calls were found for {bot} in the last {days} days. Nothing below is measured.",
    "composition": "Each call carried about {avg_tokens} tokens of input. About {conv_pct} percent of that was conversation history.",
    "composition_fixed": "The fixed prefix was about {fixed_pct} percent. Tool descriptions were about {tools_pct} percent.",
    "composition_unmeasured": "The fixed prefix and tool descriptions were not measured for {n} of {calls} calls. History is an upper bound there.",
    "history_mix": "Inside the history, tool results were {tool_pct} percent. Assistant text was {asst_pct} percent.",
    "tool_idle": "About {idle_pct} percent of the tool description tokens went to calls that used no tool.",
    "recarry": "About {recarry_pct} percent of tool-result tokens were carried again after the call that used them.",
    "cache_reported": "The provider reported cache fields. About {hit_pct} percent of input tokens were read from cache.",
    "cache_none": "No call reported any cache fields, so cache use is not measured here.",
    "cache_zero": "Every call reported cache fields as zero. Either the provider does not cache or does not report it.",
    "misses": "There were {misses} cold misses after a session's first call. The table below says why each one happened.",
    "misses_none": "There were no cold misses after a session's first call.",
    "reemit": "{docs} documents were written more than once, {repeats} repeat copies in all. The repeats cost about {tokens} output tokens.",
    "reemit_none": "No document of {k} tokens or more was written twice in one session.",
    "shape": "The longest session ran {calls} calls and {tools} tool calls without a reset. It spanned {hours} hours.",
    "mechanical": "About {mech_pct} percent of calls were tool steps with no text for the user. The power model handled {power_pct} percent of those.",
    "mechanical_none": "No call was a pure tool step.",
    "money_intro": "The costliest session cost about {actual} dollars. The replay below is arithmetic, not a promise.",
    "money_lever": "If {lever}, it would have cost about {after} dollars instead of {before} dollars.",
    "money_all": "With all three changes together, about {after} dollars instead of {before} dollars.",
    "money_none": "No session in the window could be priced, so there is no money line.",
    "unreadable": "{n} files could not be read. They are listed at the end and are not counted.",
    # Method / definition lines printed in the detail sections.
    "method_split": "Splits are by character share of what was in the context. They are scaled so the parts sum exactly to the reported input total.",
    "method_reemit": "A document is an output of at least {k} tokens, as text or inside a tool-call argument. Two match when they are the same after whitespace folding, or share half their {n}-word phrases.",
    "miss_definition": "A cold miss is a call with zero cache reads and more than {floor} cache-write tokens. The first call of a session never counts.",
    # Money-line assumptions.
    "a_actual": "Actual cost is the provider's recorded cost per call when present, else the pod's list-price estimate.",
    "a_evict": "Dropped tool results are priced at each call's blended input rate.",
    "a_cache": "A cached prefix re-prices each cold miss's cache-write tokens at the cache-read rate. The first call of a session still primes the cache.",
    "a_docs": "Linked documents remove the repeat copies' output tokens. Their carry in later calls is priced at the blended input rate.",
    "a_sum": "The three savings are added without modelling how they interact. The combined line is an upper bound.",
    "a_est": "Token splits inside a call are by character share, so the per-lever token counts are estimates.",
    "a_carry": "The carry estimate counts every block of a repeated message, so it can run high.",
    "a_clamp": "List-price savings exceeded the recorded cost here, so a line was clamped at zero.",
    # Notes.
    "note_straddle": "{n} sessions started before the window. They are counted whole.",
    "note_skipped": "{n} lines could not be parsed and were skipped.",
    "note_ledger_none": "No prefix-hash ledger records were found for these sessions. Cache breaks cannot be tied to a prefix block.",
    "note_ledger_unmatched": "Ledger records exist but none share a session id with a transcript. Cache breaks could not be tied to a block.",
    "note_footprint_none": "No tool footprint file was found for this bot.",
    "note_session_missing": "Session {sid} was not found in the window. The money line uses the costliest session.",
    "note_undated": "{n} sessions carry no timestamps and were left out of the window.",
    "note_compaction": "{n} compactions named a kept entry that could not be found. Their whole history was kept, so those calls read high.",
    "money_clamped": "One or more replay lines fell below zero, so they are not shown. The recorded cost and the list price disagree here.",
}

LEVER_LABELS = {
    "evict_tool_results": "tool results were dropped after use",
    "prefix_cached": "the fixed prefix had stayed in cache",
    "docs_by_reference": "documents were linked, not re-sent",
}


def say(key: str, **slots: object) -> str:
    return SENTENCES[key].format(**slots)


# ── Loading ──────────────────────────────────────────────────────────────────
def _parse_ts(raw: object) -> datetime | None:
    """ISO string or epoch milliseconds → aware UTC datetime."""
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_jsonl(path: Path, skipped: "list[tuple[str, int]] | None" = None) -> "list[dict] | object":
    """All dict records of a JSONL file; ``[]`` when missing; UNREADABLE on
    an OS error (never an empty list). Lines that do not parse are skipped
    (a torn tail line is expected on a live file) and counted into
    ``skipped`` as ``(path, n)`` so the report can say so."""
    if not path.exists():
        return []
    records: list[dict] = []
    bad = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except OSError:
        return UNREADABLE
    if bad and skipped is not None:
        skipped.append((str(path), bad))
    return records


def load_json(path: Path) -> "dict | None | object":
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return UNREADABLE
    return doc if isinstance(doc, dict) else UNREADABLE


def _text_of(content: object) -> str:
    """Flatten a transcript ``content`` (string or block list) to its text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _to_int(value: object) -> int:
    """A token count from whatever the provider wrote: int, float, numeric
    string, None. Anything else — or a negative — reads as 0, so one odd
    field never takes down the census (it is listed, not counted)."""
    if isinstance(value, bool):
        return 0
    try:
        return max(int(float(value)), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def _estimate_tokens(chars: int) -> int:
    return (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class HistoryEntry:
    """One message as it sits in the history a later call re-reads."""

    msg_id: str
    kind: str  # user_text | assistant_text | assistant_thinking | tool_call_args | tool_result | compaction_summary
    chars: int
    tool: str | None = None
    produced_at_call: int = -1  # ordinal of the call that produced it (-1: user/compaction)


@dataclass
class Call:
    """One model call = one assistant message carrying ``usage``."""

    session_id: str
    ordinal: int  # index within the session's calls
    run_index: int  # which turn (user-message-delimited) this call belongs to
    ts: datetime | None
    model: str
    provider: str
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    cache_fields_present: bool
    recorded_cost: float
    stop_reason: str
    history: list[HistoryEntry]
    # This call's own output.
    text_chars: int = 0
    thinking_chars: int = 0
    tool_calls: list[tuple[str, int]] = field(default_factory=list)  # (tool, arg chars)
    mechanical: bool = False
    after_compaction: bool = False
    # Fixed context for this call (chars), and where the number came from.
    fixed_prefix_chars: int | None = None
    tool_schema_chars: int | None = None
    #: Per-tool definition weight (chars), when the source could name the
    #: individual tools. ``None`` = the total is known but its breakdown is
    #: not — reported as ``unattributed``, never silently dropped.
    tool_schema_by_tool: "dict[str, int] | None" = None
    fixed_source: str = "unmeasured"

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_write

    def as_turn(self) -> dict:
        """The ``turn_cost`` record shape, so pricing goes through the pod's
        single per-turn cost rule."""
        return {
            "model": self.model,
            "provider": self.provider,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
            "cost": self.recorded_cost,
        }


@dataclass
class OutputUnit:
    """One assistant output large enough to be a 'document'."""

    session_id: str
    call_ordinal: int
    msg_id: str
    kind: str  # text | tool_call_args
    tool: str | None
    chars: int
    text: str


@dataclass
class Session:
    session_id: str
    calls: list[Call] = field(default_factory=list)
    units: list[OutputUnit] = field(default_factory=list)
    runs: int = 0
    tool_calls: int = 0
    compactions: int = 0
    compactions_unresolved: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    #: One of ``session_kinds.ALL_KINDS``. Defaults to ``unindexed`` — a
    #: session with no ``sessions.json`` row is an ABSENCE of evidence, not
    #: an unclassifiable session (the two used to share one ``unknown``).
    kind: str = KIND_UNINDEXED
    channel: str | None = None
    cache_retention: str | None = None


# ── Transcript parsing ───────────────────────────────────────────────────────
def parse_transcript(session_id: str, records: list[dict], min_doc_chars: int) -> Session:
    """Walk one transcript and build its calls with the history each one
    re-read. Compaction resets the history to the summary plus the kept
    tail, exactly as OC replays it."""
    sess = Session(session_id=session_id)
    history: list[HistoryEntry] = []
    ordinal = 0
    run_index = -1
    pending_compaction = False
    last_model: str | None = None
    for rec in records:
        rtype = rec.get("type")
        if rtype == "compaction":
            sess.compactions += 1
            kept_from = rec.get("firstKeptEntryId")
            # Keep the tail from the first kept entry. An id we cannot find
            # (unknown, or a non-message record) keeps EVERYTHING — the
            # conservative side of the upper-bound posture — and is counted.
            kept: list[HistoryEntry] = list(history)
            if isinstance(kept_from, str):
                idx = next((i for i, e in enumerate(history) if e.msg_id == kept_from), None)
                if idx is not None:
                    kept = history[idx:]
                else:
                    sess.compactions_unresolved += 1
            summary_chars = len(str(rec.get("summary") or ""))
            history = [HistoryEntry(str(rec.get("id") or ""), "compaction_summary", summary_chars)] + kept
            pending_compaction = True
            continue
        if rtype != "message":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        msg_id = str(rec.get("id") or "")
        ts = _parse_ts(rec.get("timestamp")) or _parse_ts(msg.get("timestamp"))
        if ts is not None:
            sess.first_ts = ts if sess.first_ts is None else min(sess.first_ts, ts)
            sess.last_ts = ts if sess.last_ts is None else max(sess.last_ts, ts)
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            run_index += 1
            sess.runs += 1
            history.append(HistoryEntry(msg_id, "user_text", len(_text_of(content))))
            continue
        if role in ("toolResult", "tool"):
            tool = str(msg.get("toolName") or msg.get("name") or "unknown")
            history.append(HistoryEntry(
                msg_id, "tool_result", len(_text_of(content)), tool=tool,
                produced_at_call=ordinal - 1,
            ))
            continue
        if role != "assistant":
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            # An assistant message with no usage is not a priced model call
            # (a replayed/synthetic message); its text still sits in history.
            history.append(HistoryEntry(msg_id, "assistant_text", len(_text_of(content))))
            continue
        cache_present = "cacheRead" in usage or "cacheWrite" in usage
        cost = usage.get("cost")
        recorded = 0.0
        if isinstance(cost, dict):
            try:
                recorded = float(cost.get("total") or 0.0)
            except (TypeError, ValueError):
                recorded = 0.0
        elif isinstance(cost, (int, float)):
            recorded = float(cost)
        model = str(msg.get("model") or "unknown")
        call = Call(
            session_id=session_id,
            ordinal=ordinal,
            run_index=max(run_index, 0),
            ts=ts,
            model=model,
            provider=str(msg.get("provider") or "unknown"),
            input_tokens=_to_int(usage.get("input")),
            cache_read=_to_int(usage.get("cacheRead")),
            cache_write=_to_int(usage.get("cacheWrite")),
            output_tokens=_to_int(usage.get("output")),
            cache_fields_present=cache_present,
            recorded_cost=recorded,
            stop_reason=str(msg.get("stopReason") or ""),
            history=list(history),
            after_compaction=pending_compaction,
        )
        pending_compaction = False
        last_model = model
        text_chars = 0
        thinking_chars = 0
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = str(block.get("text") or "")
                    text_chars += len(text)
                    if len(text) >= min_doc_chars:
                        sess.units.append(OutputUnit(session_id, ordinal, msg_id, "text", None, len(text), text))
                elif btype == "thinking":
                    thinking_chars += len(str(block.get("thinking") or ""))
                elif btype in ("toolCall", "tool_use"):
                    name = str(block.get("name") or "unknown")
                    args = block.get("arguments", block.get("input"))
                    arg_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
                    call.tool_calls.append((name, len(arg_text)))
                    sess.tool_calls += 1
                    doc = _largest_string(args) if not isinstance(args, str) else args
                    if len(doc) >= min_doc_chars:
                        sess.units.append(OutputUnit(session_id, ordinal, msg_id, "tool_call_args", name, len(doc), doc))
        elif isinstance(content, str):
            text_chars = len(content)
            if len(content) >= min_doc_chars:
                sess.units.append(OutputUnit(session_id, ordinal, msg_id, "text", None, len(content), content))
        call.text_chars = text_chars
        call.thinking_chars = thinking_chars
        call.mechanical = bool(call.tool_calls) and not text_chars
        sess.calls.append(call)
        # This call's output joins the history the next call re-reads.
        if thinking_chars:
            history.append(HistoryEntry(msg_id, "assistant_thinking", thinking_chars, produced_at_call=ordinal))
        if text_chars:
            history.append(HistoryEntry(msg_id, "assistant_text", text_chars, produced_at_call=ordinal))
        for name, arg_chars in call.tool_calls:
            history.append(HistoryEntry(msg_id, "tool_call_args", arg_chars, tool=name, produced_at_call=ordinal))
        ordinal += 1
    del last_model
    return sess


def _largest_string(value: object) -> str:
    """The longest string anywhere inside a tool-call argument object — the
    document a send/write tool carries."""
    best = ""
    stack = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if len(cur) > len(best):
                best = cur
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return best


# ── Fixed-context sources (trajectory → session index → footprint) ───────────
def _tool_definition_chars(tools: list, total: int) -> "dict[str, int]":
    """Chars per tool definition, summing EXACTLY to ``total``.

    ``total`` is the serialized length of the whole list; the individual
    definitions are shorter than that by the array punctuation. The difference
    is apportioned by ``split_exact`` rather than discarded, so the per-tool
    table reconciles with the tool-schema total the composition already
    reports. Two definitions sharing a name (an aliased tool) fold together —
    the name is what rides the prompt."""
    weights: Counter[str] = Counter()
    for entry in tools:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "unnamed")
        else:
            name = "unnamed"
        weights[name] += len(json.dumps(entry, ensure_ascii=False, default=str))
    if not weights:
        return {}
    return split_exact(total, dict(weights))


def trajectory_runs(records: list[dict]) -> "tuple[list[dict], str | None]":
    """Per-run ``{system_prompt_chars, tool_schema_chars, tool_schema_by_tool}``
    from OC's trajectory trace, in run order, plus the prompt-cache retention
    seen.

    ``tool_schema_by_tool`` is the CE-2a table: OC's ``context.compiled`` carries
    the actual tool-definition list it sent, so each definition can be weighed
    individually instead of only in aggregate. Chars are apportioned so they sum
    to ``tool_schema_chars`` exactly — the JSON array's own brackets/commas are
    spread over the definitions rather than dropped, so the per-tool table and
    the total the census already reports never disagree."""
    runs: dict[str, dict] = {}
    order: list[str] = []
    retention: str | None = None
    for rec in records:
        run_id = str(rec.get("runId") or rec.get("sessionId") or "")
        rtype = rec.get("type")
        raw_data = rec.get("data")
        data: dict = raw_data if isinstance(raw_data, dict) else {}
        if rtype == "context.compiled":
            if run_id not in runs:
                runs[run_id] = {}
                order.append(run_id)
            sp = data.get("systemPrompt")
            tools = data.get("tools")
            runs[run_id]["system_prompt_chars"] = len(sp) if isinstance(sp, str) else None
            if isinstance(tools, list):
                total = len(json.dumps(tools, ensure_ascii=False, default=str))
                runs[run_id]["tool_schema_chars"] = total
                runs[run_id]["tool_schema_by_tool"] = _tool_definition_chars(tools, total)
            else:
                runs[run_id]["tool_schema_chars"] = None
                runs[run_id]["tool_schema_by_tool"] = None
        elif rtype == "trace.artifacts":
            pc = data.get("promptCache")
            if isinstance(pc, dict) and isinstance(pc.get("retention"), str):
                retention = pc["retention"]
    return [runs[r] for r in order], retention


def _index_report_chars(entry: dict) -> "tuple[int | None, int | None]":
    """(system_prompt_chars, tool_schema_chars) from a sessions.json entry's
    ``systemPromptReport``, or (None, None)."""
    spr = entry.get("systemPromptReport")
    if not isinstance(spr, dict):
        return None, None
    raw_sp = spr.get("systemPrompt")
    sp: dict = raw_sp if isinstance(raw_sp, dict) else {}
    raw_tools = spr.get("tools")
    tools: dict = raw_tools if isinstance(raw_tools, dict) else {}
    sp_chars = sp.get("chars") if isinstance(sp.get("chars"), int) else None
    t_chars = None
    if isinstance(tools.get("schemaChars"), int):
        t_chars = tools["schemaChars"] + (tools.get("listChars") if isinstance(tools.get("listChars"), int) else 0)
    return sp_chars, t_chars


def _session_kind(key: str, entry: dict) -> "tuple[str, str | None]":
    """``(kind, channel)`` for one indexed session — see :mod:`session_kinds`,
    which owns the rule (and mirrors it into the plugin's tool profiles)."""
    return classify_index_entry(key, entry)


def apply_fixed_context(
    sess: Session,
    traj_runs: list[dict],
    index_entry: dict | None,
    footprint: dict | None,
) -> None:
    """Fill each call's fixed-prefix / tool-schema chars from the best source
    available: the run's own ``context.compiled`` → the last compiled context
    seen in the session → the session index's report → the Evolve tool
    footprint (tools only, Evolve's share) → unmeasured."""
    idx_sp, idx_tools = _index_report_chars(index_entry) if index_entry else (None, None)
    fp_tools = footprint.get("total_chars") if footprint and isinstance(footprint.get("total_chars"), int) else None
    last_compiled: dict | None = None
    for call in sess.calls:
        run = traj_runs[call.run_index] if call.run_index < len(traj_runs) else None
        if run and run.get("system_prompt_chars") is not None:
            last_compiled = run
        src = last_compiled
        if src is not None:
            call.fixed_prefix_chars = src.get("system_prompt_chars")
            call.tool_schema_chars = src.get("tool_schema_chars")
            call.tool_schema_by_tool = src.get("tool_schema_by_tool")
            call.fixed_source = "trajectory" if src is run else "trajectory (earlier run)"
        elif idx_sp is not None:
            call.fixed_prefix_chars = idx_sp
            call.tool_schema_chars = idx_tools
            call.fixed_source = "session index report"
        elif fp_tools is not None:
            call.tool_schema_chars = fp_tools
            call.fixed_source = "tool footprint (Evolve tools only; system prompt unmeasured)"
        else:
            call.fixed_source = "unmeasured"


# ── Composition ──────────────────────────────────────────────────────────────
def split_exact(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Split ``total`` across ``weights`` by proportion so the parts sum to
    ``total`` exactly (largest-remainder rounding). Zero weights get 0; an
    all-zero weight set puts everything in ``conversation``-less bucket
    ``unattributed``."""
    wsum = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        return {k: 0 for k in weights}
    if wsum <= 0:
        out = {k: 0 for k in weights}
        out["unattributed"] = total
        return out
    raw = {k: (total * max(v, 0)) / wsum for k, v in weights.items()}
    floors = {k: int(v) for k, v in raw.items()}
    remainder = total - sum(floors.values())
    for k in sorted(raw, key=lambda key: raw[key] - floors[key], reverse=True)[:remainder]:
        floors[k] += 1
    return floors


def call_composition(call: Call) -> dict:
    """Token split for one call, anchored to the provider's reported total."""
    buckets: Counter[str] = Counter()
    per_tool: Counter[str] = Counter()
    recarried_tool_chars = 0
    fresh_tool_chars = 0
    for entry in call.history:
        buckets[entry.kind] += entry.chars
        if entry.kind == "tool_result":
            per_tool[entry.tool or "unknown"] += entry.chars
            if entry.produced_at_call == call.ordinal - 1:
                fresh_tool_chars += entry.chars
            else:
                recarried_tool_chars += entry.chars
    conv_chars = sum(buckets.values())
    weights = {
        "fixed_prefix": call.fixed_prefix_chars or 0,
        "tool_schemas": call.tool_schema_chars or 0,
        "conversation": conv_chars,
    }
    top = split_exact(call.prompt_tokens, weights)
    conv_tokens = top.get("conversation", 0)
    conv = split_exact(conv_tokens, dict(buckets)) if buckets else {}
    tool_tokens = conv.get("tool_result", 0)
    tools_split = split_exact(tool_tokens, dict(per_tool)) if per_tool else {}
    recarry_split = split_exact(tool_tokens, {"fresh": fresh_tool_chars, "recarried": recarried_tool_chars})
    # CE-2a: the tool-DESCRIPTION bucket, per tool. Anchored to the same
    # ``top["tool_schemas"]`` figure the one-pager quotes, so the table can
    # never total something else. A call whose source knew the aggregate but
    # not the breakdown puts the whole bucket in ``unattributed`` rather than
    # reporting a table that silently omits it.
    schema_tokens = top.get("tool_schemas", 0)
    if call.tool_schema_by_tool:
        schemas_split = split_exact(schema_tokens, dict(call.tool_schema_by_tool))
    elif schema_tokens:
        schemas_split = {"unattributed": schema_tokens}
    else:
        schemas_split = {}
    return {
        "prompt_tokens": call.prompt_tokens,
        "top": top,
        "conversation": conv,
        "tool_schemas_by_tool": schemas_split,
        "tool_schemas_measured": bool(call.tool_schema_by_tool),
        "tool_results_by_tool": tools_split,
        "tool_results_fresh": recarry_split.get("fresh", 0),
        "tool_results_recarried": recarry_split.get("recarried", 0),
        "fixed_measured": call.fixed_prefix_chars is not None,
        "fixed_source": call.fixed_source,
    }


def composition_report(sessions: list[Session]) -> dict:
    calls = [c for s in sessions for c in s.calls]
    top: Counter[str] = Counter()
    conv: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    fresh = recarried = 0
    unmeasured_calls = 0
    sources: Counter[str] = Counter()
    # CE-2a tables. ``schemas_by_tool`` is the pod-wide per-tool weight;
    # ``schemas_by_kind`` is what each SESSION KIND paid for tool descriptions;
    # ``schema_kinds_by_tool`` names which kinds registered each tool — the
    # three questions "which tool is heavy", "who pays", "who could stop
    # paying" that CE-2b then acts on.
    schemas_by_tool: Counter[str] = Counter()
    schemas_by_kind: Counter[str] = Counter()
    schema_kinds_by_tool: dict[str, set[str]] = defaultdict(set)
    kind_calls: Counter[str] = Counter()
    kind_tokens: Counter[str] = Counter()
    schema_unmeasured_calls = 0
    # Tool-description tokens carried by calls that made NO tool call — the
    # share CE-2b's profiles are aimed at.
    schema_tokens_idle = 0
    for sess in sessions:
        for call in sess.calls:
            comp = call_composition(call)
            top.update(comp["top"])
            conv.update(comp["conversation"])
            by_tool.update(comp["tool_results_by_tool"])
            fresh += comp["tool_results_fresh"]
            recarried += comp["tool_results_recarried"]
            if not comp["fixed_measured"]:
                unmeasured_calls += 1
            sources[comp["fixed_source"]] += 1
            schemas_by_tool.update(comp["tool_schemas_by_tool"])
            if not call.tool_calls:
                schema_tokens_idle += comp["top"].get("tool_schemas", 0)
            schemas_by_kind[sess.kind] += comp["top"].get("tool_schemas", 0)
            kind_calls[sess.kind] += 1
            kind_tokens[sess.kind] += call.prompt_tokens
            if comp["tool_schemas_measured"]:
                for name in comp["tool_schemas_by_tool"]:
                    schema_kinds_by_tool[name].add(sess.kind)
            elif comp["top"].get("tool_schemas", 0):
                schema_unmeasured_calls += 1
    total = sum(c.prompt_tokens for c in calls)
    return {
        "calls": len(calls),
        "prompt_tokens_total": total,
        "avg_prompt_tokens": (total // len(calls)) if calls else 0,
        "top": dict(top),
        "conversation": dict(conv),
        "tool_schemas_by_tool": dict(schemas_by_tool.most_common()),
        "tool_schemas_by_kind": {k: schemas_by_kind[k] for k in ALL_KINDS if schemas_by_kind.get(k)},
        "tool_schema_kinds_by_tool": {k: sorted(v) for k, v in sorted(schema_kinds_by_tool.items())},
        "tool_schemas_unmeasured_calls": schema_unmeasured_calls,
        "tool_schemas_on_toolless_calls": schema_tokens_idle,
        "calls_by_kind": {k: kind_calls[k] for k in ALL_KINDS if kind_calls.get(k)},
        "prompt_tokens_by_kind": {k: kind_tokens[k] for k in ALL_KINDS if kind_calls.get(k)},
        "tool_results_by_tool": dict(by_tool.most_common()),
        "tool_results_fresh": fresh,
        "tool_results_recarried": recarried,
        "recarried_share": (recarried / (fresh + recarried)) if (fresh + recarried) else None,
        "fixed_unmeasured_calls": unmeasured_calls,
        "fixed_sources": dict(sources),
        "method": say("method_split"),
    }


# ── Cache ────────────────────────────────────────────────────────────────────
def _pair_hashes(hashes: list[dict]) -> dict[str, list[dict]]:
    by_session: dict[str, list[dict]] = defaultdict(list)
    for rec in hashes:
        sid = rec.get("session_id")
        if isinstance(sid, str) and sid:
            by_session[sid].append(rec)
    for recs in by_session.values():
        recs.sort(key=lambda r: _parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return by_session


def _ledger_pair(
    sess_hashes: list[dict], prev_ts: datetime | None, call_ts: datetime | None,
) -> "tuple[dict | None, dict | None]":
    """(this turn's ledger record, the record before it) by TIME, not ordinal.

    The ledger writes one record per ``before_prompt_build``; the record for
    a turn is the latest one stamped at or before the turn's first model call
    and after the previous call. Ordinal pairing breaks the moment the ledger
    does not start at the session's first turn — the live case (armed
    mid-session, or a session older than the loaded day-files) — so pair by
    the timestamps both streams carry. Returns (None, None) when no record
    falls in the turn's window; (cur, None) when the record is the session's
    first, so there is nothing to compare against.
    """
    if call_ts is None:
        return None, None
    cur_idx: int | None = None
    for i, rec in enumerate(sess_hashes):
        ts = _parse_ts(rec.get("ts"))
        if ts is None or ts > call_ts:
            continue
        if prev_ts is not None and ts <= prev_ts:
            continue
        cur_idx = i
    if cur_idx is None:
        return None, None
    before = sess_hashes[cur_idx - 1] if cur_idx > 0 else None
    return sess_hashes[cur_idx], before


def _changed_blocks(prev: dict, cur: dict) -> list[str]:
    prev_shas = prev.get("appended_block_shas")
    cur_shas = cur.get("appended_block_shas")
    if not isinstance(prev_shas, dict):
        prev_shas = {}
    if not isinstance(cur_shas, dict):
        cur_shas = {}
    return [b for b in BLOCK_NAMES if prev_shas.get(b) != cur_shas.get(b)]


def cache_report(
    sessions: list[Session],
    hashes: list[dict],
    *,
    prefix_floor: int = DEFAULT_PREFIX_FLOOR_TOKENS,
    ttl_minutes: float | None = None,
) -> dict:
    calls = [c for s in sessions for c in s.calls]
    present = [c for c in calls if c.cache_fields_present]
    if not calls:
        return {"status": "no_calls"}
    if not present:
        return {"status": "not_reported", "calls": len(calls)}
    read = sum(c.cache_read for c in present)
    write = sum(c.cache_write for c in present)
    full = sum(c.input_tokens for c in present)
    total = read + write + full
    status = "reported" if (read or write) else "reported_zero"
    ledger = _pair_hashes(hashes)
    ledger_matches = sum(1 for s in sessions if s.session_id in ledger)
    causes: Counter[str] = Counter()
    cause_write_tokens: Counter[str] = Counter()
    misses: list[dict] = []
    for sess in sessions:
        sess_hashes = ledger.get(sess.session_id, [])
        ttl = ttl_minutes
        if ttl is None:
            ttl = CACHE_TTL_MINUTES.get(sess.cache_retention or "", DEFAULT_CACHE_TTL_MINUTES)
        for idx, call in enumerate(sess.calls):
            if idx == 0 or not call.cache_fields_present:
                continue
            cold = call.cache_read == 0 and call.cache_write > prefix_floor
            if not cold:
                continue
            prev = sess.calls[idx - 1]
            why: list[str] = []
            first_of_run = prev.run_index != call.run_index
            if first_of_run:
                cur, before = _ledger_pair(sess_hashes, prev.ts, call.ts)
                if cur is None:
                    why.append("no ledger record for this turn")
                elif before is None:
                    why.append("first ledger record for this session (nothing to compare)")
                elif not (isinstance(before.get("appended_block_shas"), dict)
                          and isinstance(cur.get("appended_block_shas"), dict)):
                    why.append("ledger record unreadable (block map malformed)")
                else:
                    changed = _changed_blocks(before, cur)
                    if changed:
                        why.append("prefix block changed: " + ", ".join(changed))
                    elif before.get("prefix_sha256") != cur.get("prefix_sha256"):
                        why.append("prefix changed (block unknown)")
                    if (before.get("prefix_sha256") is None) != (cur.get("prefix_sha256") is None):
                        why.append("prefix presence flapped")
            if call.model != prev.model:
                why.append(f"model changed: {prev.model} -> {call.model}")
            if call.ts and prev.ts and (call.ts - prev.ts) > timedelta(minutes=ttl):
                why.append(f"gap over the cache window ({ttl:g} min)")
            if call.after_compaction:
                why.append("history compacted before this call")
            if not why:
                why.append("unexplained: prefix stable, same model, inside the window")
            key = " + ".join(why)
            causes[key] += 1
            cause_write_tokens[key] += call.cache_write
            misses.append({
                "session_id": sess.session_id,
                "call": call.ordinal,
                "ts": call.ts.isoformat() if call.ts else None,
                "cache_write": call.cache_write,
                "causes": why,
            })
    return {
        "status": status,
        "calls": len(calls),
        "calls_with_cache_fields": len(present),
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "full_price_input_tokens": full,
        "hit_rate_tokens": (read / total) if total else None,
        "calls_after_first": sum(max(len(s.calls) - 1, 0) for s in sessions),
        "cold_misses": len(misses),
        "cause_table": [
            {"cause": k, "misses": v, "cache_write_tokens": cause_write_tokens[k]}
            for k, v in causes.most_common()
        ],
        "misses": misses,
        "ledger_records": len(hashes),
        "ledger_sessions_matched": ledger_matches,
        "miss_definition": say("miss_definition", floor=prefix_floor),
    }


# ── Re-emission ──────────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _shingles(text: str) -> set[str]:
    words = _norm(text).split(" ")
    if len(words) < SHINGLE_WORDS:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def find_reemissions(units: list[OutputUnit]) -> list[dict]:
    """Groups of ≥2 outputs in one session that are the same document
    (exact after whitespace folding) or near-duplicates (shingle Jaccard ≥
    NEAR_DUP_JACCARD). Union-find over pairs; O(n²) per session, fine for
    the tens of documents a session holds."""
    if len(units) < 2:
        return []
    parent = list(range(len(units)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    hashes = [hashlib.sha256(_norm(u.text).encode("utf-8")).hexdigest() for u in units]
    shingles = [_shingles(u.text) for u in units]
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            same = hashes[i] == hashes[j]
            if not same and shingles[i] and shingles[j]:
                inter = len(shingles[i] & shingles[j])
                union = len(shingles[i]) + len(shingles[j]) - inter
                same = union > 0 and (inter / union) >= NEAR_DUP_JACCARD
            if same:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(units)):
        groups[find(i)].append(i)
    out: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda k: (units[k].call_ordinal, k))
        mean_chars = sum(units[k].chars for k in members) // len(members)
        unit_tokens = _estimate_tokens(mean_chars)
        out.append({
            "session_id": units[members[0]].session_id,
            "copies": len(members),
            "kinds": sorted({units[k].kind for k in members}),
            "tools": sorted({str(units[k].tool) for k in members if units[k].tool}),
            "mean_chars": mean_chars,
            "unit_tokens_est": unit_tokens,
            "repeat_output_tokens_est": unit_tokens * (len(members) - 1),
            "first_call": units[members[0]].call_ordinal,
            "calls": [units[k].call_ordinal for k in members],
            "msg_ids": [units[k].msg_id for k in members],
            "exact": len({hashes[k] for k in members}) == 1,
        })
    out.sort(key=lambda g: g["repeat_output_tokens_est"], reverse=True)
    return out


def reemission_report(sessions: list[Session], min_doc_tokens: int) -> dict:
    groups: list[dict] = []
    for sess in sessions:
        groups.extend(find_reemissions(sess.units))
    return {
        "min_doc_tokens": min_doc_tokens,
        "documents_seen": sum(len(s.units) for s in sessions),
        "groups": groups,
        "repeat_output_tokens_est": sum(g["repeat_output_tokens_est"] for g in groups),
        "method": say("method_reemit", k=min_doc_tokens, n=SHINGLE_WORDS),
    }


# ── Session shape ────────────────────────────────────────────────────────────
def _input_rate_per_mtok(model: str, provider: str) -> float | None:
    """List input price per million tokens, via the pod's pricing rule."""
    from turn_cost import estimate_turn_cost  # analyzer top-level module

    return estimate_turn_cost({
        "model": model, "provider": provider, "input_tokens": 1_000_000,
        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
    })


def shape_report(sessions: list[Session], power_model: str | None = None) -> dict:
    calls = [c for s in sessions for c in s.calls]
    per_session = []
    for s in sessions:
        span_h = ((s.last_ts - s.first_ts).total_seconds() / 3600.0) if (s.first_ts and s.last_ts) else 0.0
        per_session.append({
            "session_id": s.session_id,
            "kind": s.kind,
            "channel": s.channel,
            "turns": s.runs,
            "calls": len(s.calls),
            "tool_calls": s.tool_calls,
            "compactions": s.compactions,
            "span_hours": round(span_h, 2),
            "prompt_tokens": sum(c.prompt_tokens for c in s.calls),
            "output_tokens": sum(c.output_tokens for c in s.calls),
        })
    longest = max(per_session, key=lambda r: r["calls"], default=None)
    models = sorted({(c.model, c.provider) for c in calls})
    rates = {m: _input_rate_per_mtok(m, p) for m, p in models}
    if power_model is None and rates:
        priced = [(m, r) for m, r in rates.items() if r is not None]
        power_model = max(priced, key=lambda mr: mr[1])[0] if priced else None
    mech = [c for c in calls if c.mechanical]
    mech_power = [c for c in mech if c.model == power_model]
    by_model: Counter[str] = Counter(c.model for c in mech)
    return {
        "sessions": len(sessions),
        "calls": len(calls),
        "turns": sum(s.runs for s in sessions),
        "tool_calls": sum(s.tool_calls for s in sessions),
        "per_session": sorted(per_session, key=lambda r: r["prompt_tokens"], reverse=True),
        "longest_unrotated": longest,
        "mechanical_calls": len(mech),
        "mechanical_share": (len(mech) / len(calls)) if calls else None,
        "mechanical_by_model": dict(by_model.most_common()),
        "power_model": power_model,
        "power_model_basis": "highest list input price among models seen" if power_model else "no priced model seen",
        "mechanical_on_power_model": len(mech_power),
        "mechanical_power_share": (len(mech_power) / len(mech)) if mech else None,
        "mechanical_power_prompt_tokens": sum(c.prompt_tokens for c in mech_power),
        "model_input_rates_per_mtok": rates,
    }


# ── Money line ───────────────────────────────────────────────────────────────
def _rates(call: Call) -> "dict[str, float] | None":
    """Per-token list rates (full, cache_read, cache_write, output) for a
    call's model, through ``turn_cost.estimate_turn_cost`` so the table of
    record is the pod's own."""
    from turn_cost import estimate_turn_cost

    base = {"model": call.model, "provider": call.provider, "input_tokens": 0,
            "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    out: dict[str, float] = {}
    for key, fld in (("full", "input_tokens"), ("cache_read", "cache_read_tokens"),
                     ("cache_write", "cache_write_tokens"), ("output", "output_tokens")):
        probe = dict(base)
        probe[fld] = 1_000_000
        price = estimate_turn_cost(probe)
        if price is None:
            return None
        out[key] = price / 1_000_000
    return out


def _call_cost(call: Call) -> float | None:
    from turn_cost import turn_cost

    return turn_cost(call.as_turn())


def money_line(sess: Session, reemit_groups: list[dict], *, prefix_floor: int = DEFAULT_PREFIX_FLOOR_TOKENS) -> dict:
    """Replay one session under each lever's assumption. Pure arithmetic on
    the recorded token counts and list prices; every assumption is stated
    in the returned ``assumptions``."""
    dup_msg_ids: set[str] = set()
    for group in reemit_groups:
        if group["session_id"] == sess.session_id:
            dup_msg_ids.update(group["msg_ids"][1:])
    actual = 0.0
    priced_calls = 0
    save_evict = save_cache = save_docs_output = save_docs_carry = 0.0
    tokens_evict = tokens_cache = tokens_docs_out = tokens_docs_carry = 0
    cache_reported = any(c.cache_fields_present for c in sess.calls)
    by_ordinal = {c.ordinal: c for c in sess.calls}
    for idx, call in enumerate(sess.calls):
        cost = _call_cost(call)
        rates = _rates(call)
        if cost is None or rates is None:
            continue
        priced_calls += 1
        actual += cost
        total = call.prompt_tokens
        if total <= 0:
            continue
        input_cost = (call.input_tokens * rates["full"] + call.cache_read * rates["cache_read"]
                      + call.cache_write * rates["cache_write"])
        blended = input_cost / total
        comp = call_composition(call)
        # Lever 1: tool results evicted after the call that consumed them.
        recarried = comp["tool_results_recarried"]
        tokens_evict += recarried
        save_evict += recarried * blended
        # Lever 2: the prefix had cached — every cold miss after the first
        # call re-priced from cache-write to cache-read.
        if cache_reported and idx > 0 and call.cache_read == 0 and call.cache_write > prefix_floor:
            tokens_cache += call.cache_write
            save_cache += call.cache_write * (rates["cache_write"] - rates["cache_read"])
        # Lever 3 (carry half): duplicate documents sitting in history.
        conv_tokens = comp["top"].get("conversation", 0)
        conv_chars = sum(e.chars for e in call.history) or 1
        dup_chars = sum(e.chars for e in call.history if e.msg_id in dup_msg_ids)
        dup_tokens = int(conv_tokens * dup_chars / conv_chars)
        tokens_docs_carry += dup_tokens
        save_docs_carry += dup_tokens * blended
    # Lever 3 (output half): the repeats were never generated.
    for group in reemit_groups:
        if group["session_id"] != sess.session_id:
            continue
        for ordinal in group["calls"][1:]:
            call = by_ordinal.get(ordinal)
            rates = _rates(call) if call else None
            if rates is None:
                continue
            # Anchored to what the call actually emitted: a chars/4 estimate
            # can exceed the provider's reported output for that call.
            unit = min(group["unit_tokens_est"], call.output_tokens) if call else 0
            tokens_docs_out += unit
            save_docs_output += unit * rates["output"]
    levers = {
        "evict_tool_results": {"tokens": tokens_evict, "saved": save_evict},
        "prefix_cached": {"tokens": tokens_cache, "saved": save_cache} if cache_reported else None,
        "docs_by_reference": {
            "tokens": tokens_docs_out + tokens_docs_carry,
            "saved": save_docs_output + save_docs_carry,
        },
    }
    all_saved = sum(v["saved"] for v in levers.values() if v)
    clamped = any(v is not None and v["saved"] > actual for v in levers.values()) or all_saved > actual
    assumptions = [say(k) for k in ("a_actual", "a_evict", "a_cache", "a_docs", "a_sum", "a_est", "a_carry")]
    if clamped:
        assumptions.append(say("a_clamp"))
    return {
        "session_id": sess.session_id,
        "priced_calls": priced_calls,
        "calls": len(sess.calls),
        "actual_cost": actual,
        "levers": {
            k: (None if v is None else {**v, "after": max(actual - v["saved"], 0.0)})
            for k, v in levers.items()
        },
        "all_levers": {"saved": all_saved, "after": max(actual - all_saved, 0.0)},
        "clamped": clamped,
        "assumptions": assumptions,
    }


# ── Collection ───────────────────────────────────────────────────────────────
@dataclass
class Census:
    bot_id: str
    days: int
    generated_at: str
    sessions: list[Session]
    composition: dict
    cache: dict
    reemission: dict
    shape: dict
    money: dict | None
    sources: dict
    unreadable: list[str]
    notes: list[str]


def _in_window(sess: Session, since: datetime) -> bool:
    return sess.last_ts is not None and sess.last_ts >= since


def discover_session_dirs(sessions_dir: Path | None, bot_home: Path | None) -> "tuple[list[Path], str | None]":
    """(session dirs, error). An unreadable agents dir is reported as an
    error string, never as "no sessions" — running as the wrong user is
    the usual cause, and absence must not masquerade as emptiness."""
    if sessions_dir is not None:
        return [sessions_dir], None
    if bot_home is None:
        return [], "bot home unknown"
    agents = bot_home / ".openclaw" / "agents"
    out: list[Path] = []
    try:
        for agent_dir in sorted(agents.iterdir()):
            sess = agent_dir / "sessions"
            if sess.is_dir():
                out.append(sess)
    except FileNotFoundError:
        return [], f"{agents} does not exist"
    except OSError as exc:
        return [], f"cannot read {agents}: {type(exc).__name__}"
    return out, None


def collect(
    bot_id: str,
    days: int,
    *,
    session_dirs: list[Path],
    shared_dir: Path,
    now: datetime | None = None,
    min_doc_tokens: int = DEFAULT_MIN_DOC_TOKENS,
    prefix_floor: int = DEFAULT_PREFIX_FLOOR_TOKENS,
    ttl_minutes: float | None = None,
    power_model: str | None = None,
    money_session: str | None = None,
) -> Census:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    unreadable: list[str] = []
    notes: list[str] = []
    min_doc_chars = min_doc_tokens * CHARS_PER_TOKEN

    skipped: list[tuple[str, int]] = []
    turns_dir = shared_dir / bot_id / "turns"
    footprint = load_json(turns_dir / "context-footprint.json")
    if footprint is UNREADABLE:
        unreadable.append(str(turns_dir / "context-footprint.json"))
        footprint = None

    sessions: list[Session] = []
    undated = 0
    files_seen = 0
    traj_seen = 0
    index_seen = 0
    for sdir in session_dirs:
        index: dict = {}
        idx_doc = load_json(sdir / "sessions.json")
        if idx_doc is UNREADABLE:
            unreadable.append(str(sdir / "sessions.json"))
        elif isinstance(idx_doc, dict):
            index_seen += 1
            for key, entry in idx_doc.items():
                if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
                    index[entry["sessionId"]] = (key, entry)
        try:
            names = sorted(p.name for p in sdir.iterdir())
        except OSError:
            unreadable.append(str(sdir))
            continue
        for name in names:
            if not _SESSION_FILE_RE.match(name):
                continue
            path = sdir / name
            # Cheap window pre-filter on mtime; the message timestamps decide.
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                unreadable.append(str(path))
                continue
            if mtime < since:
                continue
            records = load_jsonl(path, skipped)
            if records is UNREADABLE:
                unreadable.append(str(path))
                continue
            files_seen += 1
            session_id = name[:-len(".jsonl")]
            sess = parse_transcript(session_id, records, min_doc_chars)  # type: ignore[arg-type]
            if not sess.calls:
                continue
            if sess.last_ts is None:
                undated += 1
                continue
            if not _in_window(sess, since):
                continue
            traj = load_jsonl(sdir / f"{session_id}.trajectory.jsonl", skipped)
            traj_runs: list[dict] = []
            if traj is UNREADABLE:
                unreadable.append(str(sdir / f"{session_id}.trajectory.jsonl"))
            elif traj:
                traj_seen += 1
                traj_runs, sess.cache_retention = trajectory_runs(traj)  # type: ignore[arg-type]
            entry = index.get(session_id)
            if entry:
                sess.kind, sess.channel = _session_kind(entry[0], entry[1])
            apply_fixed_context(
                sess, traj_runs, entry[1] if entry else None,
                footprint if isinstance(footprint, dict) else None,
            )
            sessions.append(sess)
    # Sessions are the unit and are counted WHOLE: trimming calls would leave
    # ordinals, turn counts, spans and the first-call cache rule inconsistent.
    # A session that started before the window is reported, and said so.
    sessions.sort(key=lambda s: s.first_ts or datetime.min.replace(tzinfo=timezone.utc))
    straddling = sum(1 for s in sessions if s.first_ts is not None and s.first_ts < since)
    if straddling:
        notes.append(say("note_straddle", n=straddling))

    # Prefix-hash ledger: every day-file from the earliest session start to
    # today, so a whole session's turns can pair even when it began before
    # the window (capped at LEDGER_MAX_DAYS).
    hashes: list[dict] = []
    earliest = min((s.first_ts for s in sessions if s.first_ts), default=since)
    span_days = min(max((now - min(earliest, since)).days + 1, days + 1), LEDGER_MAX_DAYS)
    for offset in range(span_days):
        day = (now - timedelta(days=offset)).date().isoformat()
        ledger_path = turns_dir / f"prefix-hashes-{day}.jsonl"
        loaded = load_jsonl(ledger_path, skipped)
        if loaded is UNREADABLE:
            unreadable.append(str(ledger_path))
        else:
            hashes.extend(loaded)  # type: ignore[arg-type]

    composition = composition_report(sessions)
    cache = cache_report(sessions, hashes, prefix_floor=prefix_floor, ttl_minutes=ttl_minutes)
    reemit = reemission_report(sessions, min_doc_tokens)
    shape = shape_report(sessions, power_model=power_model)

    money: dict | None = None
    target: Session | None = None
    if money_session:
        target = next((s for s in sessions if s.session_id == money_session), None)
        if target is None:
            notes.append(say("note_session_missing", sid=money_session))
    if target is None and sessions:
        costed = []
        for s in sessions:
            total = 0.0
            for c in s.calls:
                cost = _call_cost(c)
                if cost:
                    total += cost
            costed.append((total, s))
        costed.sort(key=lambda t: t[0], reverse=True)
        if costed and costed[0][0] > 0:
            target = costed[0][1]
    if target is not None:
        money = money_line(target, reemit["groups"], prefix_floor=prefix_floor)

    if hashes and cache.get("ledger_sessions_matched") == 0:
        notes.append(say("note_ledger_unmatched"))
    if not hashes:
        notes.append(say("note_ledger_none"))
    if footprint is None:
        notes.append(say("note_footprint_none"))
    if skipped:
        notes.append(say("note_skipped", n=sum(n for _, n in skipped)))
    if undated:
        notes.append(say("note_undated", n=undated))
    unresolved = sum(s.compactions_unresolved for s in sessions)
    if unresolved:
        notes.append(say("note_compaction", n=unresolved))

    sources = {
        "session_dirs": [str(p) for p in session_dirs],
        "transcripts_read": files_seen,
        "transcripts_in_window": len(sessions),
        "trajectories_read": traj_seen,
        "session_index_files": index_seen,
        "ledger_records": len(hashes),
        "ledger_days_read": span_days,
        "skipped_lines": skipped,
        "tool_footprint": bool(footprint),
        "shared_dir": str(shared_dir),
    }
    return Census(
        bot_id=bot_id, days=days, generated_at=now.isoformat(),
        sessions=sessions, composition=composition, cache=cache,
        reemission=reemit, shape=shape, money=money, sources=sources,
        unreadable=unreadable, notes=notes,
    )


# ── Rendering ────────────────────────────────────────────────────────────────
def _pct(part: float | int, whole: float | int) -> int:
    return int(round(100.0 * part / whole)) if whole else 0


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def one_pager(census: Census) -> list[str]:
    """The plain-English sentences, in order. Every line is a filled
    SENTENCES template (readability-gated)."""
    lines: list[str] = []
    comp = census.composition
    calls = comp["calls"]
    lines.append(say("intro", bot=census.bot_id, days=census.days, calls=calls, sessions=len(census.sessions)))
    if not calls:
        lines.append(say("no_calls", bot=census.bot_id, days=census.days))
        if census.unreadable:
            lines.append(say("unreadable", n=len(census.unreadable)))
        return lines
    top = comp["top"]
    total = comp["prompt_tokens_total"] or 1
    lines.append(say("composition", avg_tokens=_fmt_k(comp["avg_prompt_tokens"]),
                     conv_pct=_pct(top.get("conversation", 0) + top.get("unattributed", 0), total)))
    if comp["fixed_unmeasured_calls"] < calls:
        lines.append(say("composition_fixed", fixed_pct=_pct(top.get("fixed_prefix", 0), total),
                         tools_pct=_pct(top.get("tool_schemas", 0), total)))
    if comp["fixed_unmeasured_calls"]:
        lines.append(say("composition_unmeasured", n=comp["fixed_unmeasured_calls"], calls=calls))
    conv = comp["conversation"]
    conv_total = sum(conv.values())
    if conv_total:
        lines.append(say("history_mix", tool_pct=_pct(conv.get("tool_result", 0), conv_total),
                         asst_pct=_pct(conv.get("assistant_text", 0), conv_total)))
    schema_total = top.get("tool_schemas", 0)
    if schema_total:
        lines.append(say("tool_idle", idle_pct=_pct(comp["tool_schemas_on_toolless_calls"], schema_total)))
    if comp["recarried_share"] is not None:
        lines.append(say("recarry", recarry_pct=_pct(comp["recarried_share"], 1)))
    cache = census.cache
    status = cache.get("status")
    if status == "reported":
        lines.append(say("cache_reported", hit_pct=_pct(cache.get("hit_rate_tokens") or 0, 1)))
        if cache.get("cold_misses"):
            lines.append(say("misses", misses=cache["cold_misses"]))
        else:
            lines.append(say("misses_none"))
    elif status == "reported_zero":
        lines.append(say("cache_zero"))
    elif status == "not_reported":
        lines.append(say("cache_none"))
    reemit = census.reemission
    if reemit["groups"]:
        lines.append(say("reemit", docs=len(reemit["groups"]),
                         repeats=sum(g["copies"] - 1 for g in reemit["groups"]),
                         tokens=_fmt_k(reemit["repeat_output_tokens_est"])))
    else:
        lines.append(say("reemit_none", k=reemit["min_doc_tokens"]))
    shape = census.shape
    longest = shape.get("longest_unrotated")
    if longest:
        lines.append(say("shape", calls=longest["calls"], tools=longest["tool_calls"], hours=f"{longest['span_hours']:.1f}"))
    if shape["mechanical_calls"]:
        lines.append(say("mechanical", mech_pct=_pct(shape["mechanical_share"] or 0, 1),
                         power_pct=_pct(shape["mechanical_power_share"] or 0, 1)))
    else:
        lines.append(say("mechanical_none"))
    money = census.money
    if money and money["priced_calls"]:
        before = f"{money['actual_cost']:.2f}"
        lines.append(say("money_intro", actual=before))
        if money.get("clamped"):
            lines.append(say("money_clamped"))
        else:
            for key, label in LEVER_LABELS.items():
                lever = money["levers"].get(key)
                if lever is None:
                    continue
                lines.append(say("money_lever", lever=label, after=f"{lever['after']:.2f}", before=before))
            lines.append(say("money_all", after=f"{money['all_levers']['after']:.2f}", before=before))
    else:
        lines.append(say("money_none"))
    if census.unreadable:
        lines.append(say("unreadable", n=len(census.unreadable)))
    return lines


def render(census: Census) -> str:
    out: list[str] = ["Context-efficiency census", "=" * 25, ""]
    out.extend(one_pager(census))
    out.append("")
    comp = census.composition
    if comp["calls"]:
        total = comp["prompt_tokens_total"] or 1
        out += ["History composition (estimated tokens, all calls)", "-" * 48]
        for key in ("fixed_prefix", "tool_schemas", "conversation", "unattributed"):
            val = comp["top"].get(key, 0)
            if val or key != "unattributed":
                out.append(f"  {key:<22} {val:>12,}  {_pct(val, total):>3}%")
        out.append("  fixed context source: " + ", ".join(f"{k} x{v}" for k, v in comp["fixed_sources"].items()))
        conv_total = sum(comp["conversation"].values()) or 1
        out += ["", "  conversation by kind:"]
        for key, val in sorted(comp["conversation"].items(), key=lambda kv: kv[1], reverse=True):
            out.append(f"    {key:<22} {val:>12,}  {_pct(val, conv_total):>3}%")
        if comp["tool_results_by_tool"]:
            out += ["", "  tool results by tool:"]
            for key, val in comp["tool_results_by_tool"].items():
                out.append(f"    {key:<22} {val:>12,}")
            out.append(f"    re-carried after use: {comp['tool_results_recarried']:,} of "
                       f"{comp['tool_results_fresh'] + comp['tool_results_recarried']:,}")
        schemas = comp["tool_schemas_by_tool"]
        if schemas:
            schema_total = sum(schemas.values()) or 1
            kinds_by_tool = comp["tool_schema_kinds_by_tool"]
            out += ["", "  tool descriptions by tool (what every call carries whether or not it calls it):"]
            for key, val in schemas.items():
                who = ",".join(kinds_by_tool.get(key, [])) or "-"
                out.append(f"    {key:<28} {val:>12,}  {_pct(val, schema_total):>3}%  {who}")
            if comp["tool_schemas_unmeasured_calls"]:
                out.append(f"    ({comp['tool_schemas_unmeasured_calls']} call(s) knew the total but not the "
                           "breakdown — counted as unattributed above)")
        by_kind = comp["tool_schemas_by_kind"]
        if by_kind:
            out += ["", "  tool descriptions by session kind:"]
            calls_by_kind = comp["calls_by_kind"]
            for key, val in sorted(by_kind.items(), key=lambda kv: kv[1], reverse=True):
                n = calls_by_kind.get(key, 0)
                per_call = (val // n) if n else 0
                out.append(f"    {key:<18} {val:>12,}  over {n:>3} call(s)  ~{_fmt_k(per_call)}/call")
        out += ["", f"  method: {comp['method']}", ""]
        cache = census.cache
        out += ["Cache", "-" * 5]
        if cache.get("status") == "reported":
            out.append(f"  cache read {cache['cache_read_tokens']:,}   cache write {cache['cache_write_tokens']:,}   "
                       f"full price {cache['full_price_input_tokens']:,}   hit rate {_pct(cache['hit_rate_tokens'] or 0, 1)}%")
            out.append(f"  cold misses after first call: {cache['cold_misses']} of {cache['calls_after_first']} "
                       f"({cache['miss_definition']})")
            out.append(f"  ledger records {cache['ledger_records']}, sessions matched {cache['ledger_sessions_matched']}")
            if cache["cause_table"]:
                out.append("  cache-break causes:")
                for row in cache["cause_table"]:
                    out.append(f"    {row['misses']:>3}  {row['cache_write_tokens']:>10,} write tok  {row['cause']}")
        else:
            out.append(f"  {cache.get('status')}")
        out.append("")
        reemit = census.reemission
        out += ["Re-emission", "-" * 11, f"  documents seen: {reemit['documents_seen']} (>= {reemit['min_doc_tokens']} tokens)"]
        for g in reemit["groups"][:10]:
            out.append(f"  x{g['copies']}  ~{g['unit_tokens_est']:,} tok each  {'/'.join(g['kinds'])}"
                       f"{(' via ' + ','.join(g['tools'])) if g['tools'] else ''}  session {g['session_id'][:8]}  "
                       f"calls {g['calls']}  {'exact' if g['exact'] else 'near-duplicate'}")
        out.append(f"  method: {reemit['method']}")
        out.append("")
        shape = census.shape
        out += ["Session shape", "-" * 13,
                f"  sessions {shape['sessions']}  turns {shape['turns']}  calls {shape['calls']}  tool calls {shape['tool_calls']}",
                f"  mechanical calls {shape['mechanical_calls']} ({_pct(shape['mechanical_share'] or 0, 1)}%); "
                f"power model {shape['power_model'] or 'none'} ({shape['power_model_basis']}) handled "
                f"{shape['mechanical_on_power_model']} of them"]
        for row in shape["per_session"][:8]:
            out.append(f"    {row['session_id'][:8]}  {row['kind']:<15} turns {row['turns']:>3}  calls {row['calls']:>3}  "
                       f"tools {row['tool_calls']:>3}  span {row['span_hours']:>6.1f}h  input {row['prompt_tokens']:>10,}")
        out.append("")
        money = census.money
        if money:
            out += ["Money line", "-" * 10, f"  session {money['session_id'][:8]}  actual ${money['actual_cost']:.2f} "
                    f"({money['priced_calls']} of {money['calls']} calls priced)"]
            for key, label in LEVER_LABELS.items():
                lever = money["levers"].get(key)
                if lever is None:
                    out.append(f"  {key:<20} not measurable (cache not reported)")
                else:
                    out.append(f"  {key:<20} -{lever['tokens']:>10,} tok  saves ${lever['saved']:.2f}  -> ${lever['after']:.2f}")
            out.append(f"  {'all three':<20} saves ${money['all_levers']['saved']:.2f}  -> ${money['all_levers']['after']:.2f}")
            out.append("  assumptions:")
            for line in money["assumptions"]:
                out.append(f"    - {line}")
            out.append("")
    if census.notes:
        out += ["Notes", "-" * 5] + [f"  - {n}" for n in census.notes] + [""]
    if census.unreadable:
        out += ["Unreadable (not counted)", "-" * 24] + [f"  {p}" for p in census.unreadable] + [""]
    src = census.sources
    out.append(f"sources: {src['transcripts_in_window']} transcripts in window of {src['transcripts_read']} read, "
               f"{src['trajectories_read']} trajectories, {src['ledger_records']} ledger records, "
               f"tool footprint {'yes' if src['tool_footprint'] else 'no'}")
    return "\n".join(out)


def to_json(census: Census) -> dict:
    return {
        "schema_version": CENSUS_SCHEMA_VERSION,
        "bot_id": census.bot_id,
        "days": census.days,
        "generated_at": census.generated_at,
        "one_pager": one_pager(census),
        "composition": census.composition,
        "cache": census.cache,
        "reemission": census.reemission,
        "session_shape": census.shape,
        "money_line": census.money,
        "sources": census.sources,
        "notes": census.notes,
        "unreadable": census.unreadable,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of days")
    return value


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--bot", required=True, help="bot id")
    parser.add_argument("--days", type=_positive_int, default=3, help="trailing window in days (default 3)")
    parser.add_argument("--shared-dir", default=get_profile().shared_dir_default,
                        help="pod shared dir for the ledger + tool footprint (default: platform profile)")
    parser.add_argument("--sessions-dir", default=None,
                        help="OC sessions dir (default: every <bot home>/.openclaw/agents/*/sessions)")
    parser.add_argument("--min-doc-tokens", type=int, default=DEFAULT_MIN_DOC_TOKENS,
                        help=f"re-emission document threshold (default {DEFAULT_MIN_DOC_TOKENS})")
    parser.add_argument("--prefix-floor", type=int, default=DEFAULT_PREFIX_FLOOR_TOKENS,
                        help="cold-miss cache-write floor (default 1000)")
    parser.add_argument("--cache-ttl-minutes", type=float, default=None,
                        help="override the cache window used for gap attribution (default: from the trajectory, else 5)")
    parser.add_argument("--power-model", default=None, help="model id to treat as the power rung (default: priciest seen)")
    parser.add_argument("--session", default=None, help="session id for the money line (default: costliest)")
    parser.add_argument("--json-out", default=None, help="also write the JSON report to this path")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the one-pager")
    args = parser.parse_args(argv)

    session_dirs: list[Path]
    if args.sessions_dir:
        session_dirs = [Path(args.sessions_dir)]
    else:
        home: Path | None = None
        try:
            from evolve_config import bot_home  # analyzer top-level module
            home = Path(bot_home(args.bot))
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            print(f"cannot resolve bot home for {args.bot}: {exc}; pass --sessions-dir", file=sys.stderr)
            return 2
        session_dirs, err = discover_session_dirs(None, home)
        if err or not session_dirs:
            print(f"no readable sessions dirs for {args.bot}: {err or 'none found'}; pass --sessions-dir", file=sys.stderr)
            return 2
    census = collect(
        args.bot, args.days, session_dirs=session_dirs, shared_dir=Path(args.shared_dir),
        min_doc_tokens=args.min_doc_tokens, prefix_floor=args.prefix_floor,
        ttl_minutes=args.cache_ttl_minutes, power_model=args.power_model,
        money_session=args.session,
    )
    doc = to_json(census)
    if args.json_out:
        try:
            Path(args.json_out).write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"cannot write {args.json_out}: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print(render(census))
    return 0


if __name__ == "__main__":
    sys.exit(main())
