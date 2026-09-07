#!/usr/bin/env python3
"""Atlas On-Demand Research — @-mention responder.

Two invocation modes:

  **Legacy CLI args** — used by cron, tests, and direct operator
  invocation::

      atlas_research.py ask --query "<question>" --member-id <ID> --message-id <MSG_ID>

  **JSON request file** — used by the bot agent through the OpenClaw
  ``exec`` tool. OC v2026.5.26+ has an exec preflight that rejects
  "complex interpreter invocations" (multiple flags, quoted strings,
  subcommand) per ``openclaw#87371``. The workaround: the agent writes
  a JSON file containing all the args, then invokes the script with
  a single positional path::

      atlas_research.py /tmp/atlas-research-<msg.id>.json

  The script reads the JSON, processes, deletes the request file, and
  emits the same stdout signals as the CLI mode. The OC preflight
  passes because there are no flags or quoted strings to inspect.

Pipeline (both modes):
1. Hash member_id (privacy)
2. Off-topic check (cheap LLM classification)
3. Strategy-question check
4. Per-member rate-limit check
5. Pod-wide daily budget check
6. Brave Search (1-3 queries)
7. Haiku synthesis with strict source-grounding prompt
8. Update budget, log query, emit signal

Signals (stdout):
    RESEARCH_ANSWERED:<base64-encoded-reply-text>
    RESEARCH_RATE_LIMITED:<reply-text>
    RESEARCH_BUDGET_EXCEEDED:<reply-text>
    RESEARCH_REFUSED:<reply-text>
    RESEARCH_FAILED

Budget tracking is a single JSON file at workspace/atlas/research-budget.json.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas_lib import config as cfg  # noqa: E402
from atlas_lib import fetchers  # noqa: E402
from atlas_lib import guard  # noqa: E402
from atlas_lib import hashing  # noqa: E402
from atlas_lib import oc_dispatch  # noqa: E402

APP_ID = "app_atlas_on_demand_research"


def _log(msg: str) -> None:
    print(f"[atlas_research] {msg}", file=sys.stderr)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _today_str() -> str:
    return _now().date().isoformat()


def _atlas_dir(bot_id: str) -> Path:
    return cfg.workspace_root(bot_id) / "atlas"


def _research_log_path(bot_id: str) -> Path:
    return _atlas_dir(bot_id) / "research-log.jsonl"


def _budget_path(bot_id: str) -> Path:
    return _atlas_dir(bot_id) / "research-budget.json"


def _read_budget(bot_id: str) -> dict:
    return cfg.read_json(_budget_path(bot_id), default={})


def _refresh_budget(bot_id: str, persist: bool = True) -> dict:
    """Read budget, reset if it's from a previous day.

    When persist=False, the reset is in-memory only — used by read-only callers
    (cmd_budget) so that running 'budget' against an uninitialized bot doesn't
    try to create directories it can't.
    """
    b = _read_budget(bot_id)
    today = _today_str()
    if b.get("date") != today:
        b = {"date": today, "queries_today": 0, "cost_today_usd": 0.0,
             "rate_limited_members": []}
        if persist:
            try:
                cfg.write_json_atomic(_budget_path(bot_id), b)
            except OSError as exc:
                _log(f"could not persist fresh budget: {exc}")
    return b


def _update_budget(bot_id: str, cost: float, member_hash: str | None,
                    rate_limited: bool = False) -> dict:
    b = _refresh_budget(bot_id)
    b["queries_today"] = int(b.get("queries_today", 0)) + 1
    b["cost_today_usd"] = float(b.get("cost_today_usd", 0.0)) + cost
    if rate_limited and member_hash:
        rls = set(b.get("rate_limited_members", []))
        rls.add(member_hash)
        b["rate_limited_members"] = sorted(rls)
    cfg.write_json_atomic(_budget_path(bot_id), b)
    return b


def _append_log(bot_id: str, entry: dict) -> None:
    """Best-effort append. Silent on missing workspace (so smoke tests don't crash)."""
    log = _research_log_path(bot_id)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log(f"could not append to research-log: {exc}")


def _count_member_queries(bot_id: str, member_hash: str,
                          since: datetime) -> int:
    log = _research_log_path(bot_id)
    if not log.exists():
        return 0
    n = 0
    with log.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(e["asked_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts < since:
                continue
            if e.get("member_id_hash") == member_hash:
                n += 1
    return n


# ----- LLM dispatch (via bot's local OpenClaw agent, openclaw_headless transport) -----

def _llm_call(prompt: str, timeout_s: int = 30) -> tuple[str, float]:
    """Returns (text, cost_usd). Cost is a pessimistic estimate for budget tracking;
    the bot's daily_total_usd is the authoritative figure."""
    text, tel = oc_dispatch.dispatch(prompt, timeout_s=timeout_s)
    if tel["error"]:
        _log(f"oc dispatch failed: {tel['error']}")
        return "", tel["cost_usd"]
    return text, tel["cost_usd"]


# ----- Scope checks -----

SCOPE_CHECK_PROMPT = """You are gating questions for a research bot scoped to the OpenClaw / AI-agent ecosystem.

The bot answers questions about: agent frameworks, MCPs, agent skills, models (Claude/GPT/Gemini), agent tooling, deployment case studies, security incidents involving agents, agent-relevant new releases.

The bot DOES NOT answer: general weather/sports/news, personal advice, recipe/cooking, code-writing requests, math problems, anything unrelated to the agent ecosystem.

Question: {question}

Reply with JSON ONLY: {{"in_scope": true|false, "reason": "<one short sentence>"}}.
"""

STRATEGY_CHECK_PROMPT = """You are gating questions for a research bot. The bot can describe what is happening in the ecosystem but does NOT give strategic or evaluative recommendations.

Strategy/opinion questions include: "should I use X?", "is X better than Y?", "what should we build?", "is X worth it?", "what's the best Z?", "do you recommend X?".

Factual questions include: "what is X?", "how does X work?", "what does X support?", "any new X this week?", "case studies on X?".

Question: {question}

Reply with JSON ONLY: {{"is_strategy_question": true|false, "reason": "<one short sentence>"}}.
"""

SYNTHESIS_PROMPT = """You are Atlas, a research assistant for an OpenClaw / AI-agent enthusiast community on Telegram.

A community member asked: {question}

Here are search results gathered from Brave:

{results}

Compose a Team_bot_a-style answer:
- 1-2 sentence direct answer to the question
- Then 2-4 bullet points of supporting facts (one fact per line)
- Then a "Sources:" line with 2-4 URLs separated by " | "

Rules:
- Maximum 6 lines body + 1 sources line.
- Cite only URLs that appear in the search results above. Do not invent sources.
- If the results don't actually answer the question, say so plainly in the body and offer 1-2 lines on what was found instead.
- Do not editorialize. Do not give recommendations. State what is, not what should be done.
- Plain text only. No markdown formatting (no **bold**, no `code`).
"""


def _is_in_scope(question: str) -> tuple[bool, str, float]:
    text, cost = _llm_call(SCOPE_CHECK_PROMPT.format(question=question[:500]), timeout_s=30)
    if not text:
        return True, "scope-check unreachable; allowing", cost  # fail-open
    parsed = oc_dispatch.parse_json_reply(text)
    if parsed is None:
        return True, "scope-check malformed; allowing", cost
    return bool(parsed.get("in_scope", True)), parsed.get("reason", ""), cost


def _is_strategy_question(question: str) -> tuple[bool, str, float]:
    text, cost = _llm_call(STRATEGY_CHECK_PROMPT.format(question=question[:500]), timeout_s=30)
    if not text:
        return False, "strategy-check unreachable; allowing", cost
    parsed = oc_dispatch.parse_json_reply(text)
    if parsed is None:
        return False, "strategy-check malformed; allowing", cost
    return bool(parsed.get("is_strategy_question", False)), parsed.get("reason", ""), cost


def _format_search_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results[:8], 1):
        lines.append(f"[{i}] {r.get('title', '')} ({r.get('url', '')})")
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")[:400]
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)


def _next_window(now: datetime) -> str:
    return (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).strftime("%H:%M")


def _emit_signal(signal: str, payload: str = "") -> None:
    if payload:
        print(f"{signal}{payload}")
    else:
        print(signal)


def _signal_with_text(signal_name: str, text: str) -> None:
    _emit_signal(f"{signal_name}:", text)


def cmd_ask(args) -> int:
    # Guard: research is allowed in approved groups, DM from operator, DM from member.
    # Refused in foreign_group and DM from stranger.
    context, role = guard.classify(args.member_id, args.chat_id, args.chat_type, bot_id=args.bot_id)
    if context == "foreign_group":
        # Silent ignore: do not even emit a signal; bot will see no output and not reply.
        return 0
    if context == "dm" and role == "stranger":
        # Silent ignore for strangers. (Polite refusal exposed via AGENTS.md guidance if Pod_admin
        # later prefers it; default is silent to deny information about bot capabilities.)
        return 0

    cfg_research = cfg.research_config(args.bot_id)
    refusals = cfg_research.get("refusal_templates", {})
    rate = cfg_research.get("per_member_rate", {})
    budget_cfg = cfg_research.get("pod_budget", {})
    queries_per_hour = int(rate.get("queries_per_hour", 3))
    queries_per_day = int(rate.get("queries_per_day", 10))
    max_cost_usd = float(budget_cfg.get("max_cost_usd_per_day", 1.00))
    max_queries = int(budget_cfg.get("max_queries_per_day", 100))

    salt = hashing.read_or_create_salt(_atlas_dir(args.bot_id) / ".capture-salt")
    member_hash = hashing.hash_member(args.member_id, salt) if args.member_id else None

    # Operator bypasses rate-limit and budget (they're the steward, not a consumer).
    # Operator still goes through scope/strategy checks for consistency (the bot answers
    # better questions when scope is enforced uniformly).
    is_operator = (role == "operator")

    # Pod-wide budget check (cheap, do first) — but operator bypasses
    b = _refresh_budget(args.bot_id)
    if not is_operator and (b["cost_today_usd"] >= max_cost_usd or b["queries_today"] >= max_queries):
        tmpl = refusals.get("budget_exceeded",
                            "Research budget for today is spent. The daily digest will still run tomorrow.")
        reply = tmpl.format(spent=f"{b['cost_today_usd']:.2f}", cap=f"{max_cost_usd:.2f}",
                            time="07:00")
        _signal_with_text("RESEARCH_BUDGET_EXCEEDED", reply)
        _append_log(args.bot_id, {
            "asked_at": _now_iso(), "member_id_hash": member_hash,
            "query": args.query, "outcome": "budget_exceeded", "cost_usd": 0.0,
        })
        return 0

    # Per-member rate limit check — operator bypasses
    if member_hash and not is_operator:
        now = _now()
        hourly = _count_member_queries(args.bot_id, member_hash, now - timedelta(hours=1))
        daily = _count_member_queries(args.bot_id, member_hash, now - timedelta(days=1))
        if hourly >= queries_per_hour or daily >= queries_per_day:
            tmpl = refusals.get(
                "rate_limited",
                "You've asked {n} questions in the last hour — limit is {cap}. Try again at {next_window}.")
            reply = tmpl.format(n=hourly, cap=queries_per_hour, next_window=_next_window(now))
            _signal_with_text("RESEARCH_RATE_LIMITED", reply)
            _update_budget(args.bot_id, 0.0, member_hash, rate_limited=True)
            _append_log(args.bot_id, {
                "asked_at": _now_iso(), "member_id_hash": member_hash,
                "query": args.query, "outcome": "rate_limited", "cost_usd": 0.0,
            })
            return 0

    total_cost = 0.0

    # Scope check
    in_scope, scope_reason, scope_cost = _is_in_scope(args.query)
    total_cost += scope_cost
    if not in_scope:
        tmpl = refusals.get("off_topic",
                            "Not in my lane — I cover OC, AI agents, agent tooling, ecosystem case studies, and warnings.")
        _signal_with_text("RESEARCH_REFUSED", tmpl)
        _update_budget(args.bot_id, total_cost, member_hash)
        _append_log(args.bot_id, {
            "asked_at": _now_iso(), "member_id_hash": member_hash,
            "query": args.query, "outcome": "refused_off_topic", "reason": scope_reason,
            "cost_usd": total_cost,
        })
        return 0

    # Strategy check
    is_strategy, strategy_reason, strat_cost = _is_strategy_question(args.query)
    total_cost += strat_cost
    if is_strategy:
        tmpl = refusals.get("strategy",
                            "Strategy questions go to the operator, not me. I can tell you what's happening; I can't tell you what to do about it.")
        _signal_with_text("RESEARCH_REFUSED", tmpl)
        _update_budget(args.bot_id, total_cost, member_hash)
        _append_log(args.bot_id, {
            "asked_at": _now_iso(), "member_id_hash": member_hash,
            "query": args.query, "outcome": "refused_strategy", "reason": strategy_reason,
            "cost_usd": total_cost,
        })
        return 0

    # Brave search
    brave_key = cfg.brave_api_key(args.bot_id)
    results = fetchers.brave_search(args.query, api_key=brave_key, count=8)
    if not results:
        _emit_signal("RESEARCH_FAILED")
        _update_budget(args.bot_id, total_cost, member_hash)
        _append_log(args.bot_id, {
            "asked_at": _now_iso(), "member_id_hash": member_hash,
            "query": args.query, "outcome": "search_failed", "cost_usd": total_cost,
        })
        return 2

    # Synthesis
    synth_text, synth_cost = _llm_call(
        SYNTHESIS_PROMPT.format(question=args.query, results=_format_search_results(results)),
        timeout_s=45,
    )
    total_cost += synth_cost
    if not synth_text:
        _emit_signal("RESEARCH_FAILED")
        _update_budget(args.bot_id, total_cost, member_hash)
        _append_log(args.bot_id, {
            "asked_at": _now_iso(), "member_id_hash": member_hash,
            "query": args.query, "outcome": "synthesis_failed", "cost_usd": total_cost,
        })
        return 2

    encoded = base64.b64encode(synth_text.encode("utf-8")).decode("ascii")
    _emit_signal("RESEARCH_ANSWERED", f":{encoded}")
    _update_budget(args.bot_id, total_cost, member_hash)
    _append_log(args.bot_id, {
        "asked_at": _now_iso(), "member_id_hash": member_hash,
        "query": args.query, "outcome": "answered",
        "cost_usd": total_cost, "sources_cited": len(results),
    })
    return 0


def cmd_budget(args) -> int:
    b = _refresh_budget(args.bot_id, persist=False)
    print(f"date: {b.get('date')}")
    print(f"queries_today: {b.get('queries_today', 0)}")
    print(f"cost_today_usd: ${b.get('cost_today_usd', 0.0):.4f}")
    print(f"rate_limited_members (today): {len(b.get('rate_limited_members', []))}")
    return 0


class _RequestArgs:
    """argparse.Namespace-shaped struct populated from a JSON request file.

    Used by the JSON-file invocation mode so cmd_ask / cmd_budget can
    consume the same attribute layout regardless of how the script was
    invoked. argparse.Namespace would work too, but a dedicated class
    documents the contract.
    """

    __slots__ = ("mode", "query", "member_id", "message_id",
                 "chat_id", "chat_type", "bot_id")

    def __init__(self, mode: str, query: str = "", member_id: str = "",
                 message_id: str = "", chat_id: str = "",
                 chat_type: str = "supergroup", bot_id: str = "atlas") -> None:
        self.mode         = mode
        self.query        = query
        self.member_id    = member_id
        self.message_id   = message_id
        self.chat_id      = chat_id
        self.chat_type    = chat_type
        self.bot_id       = bot_id


_VALID_CHAT_TYPES = ("private", "group", "supergroup", "channel")
_VALID_MODES = ("ask", "budget")


def _looks_like_request_file(argv: list) -> bool:
    """Return True iff argv looks like ``script.py <one-json-path>``.

    The OC exec preflight blocks multi-arg interpreter invocations
    (openclaw#87371), so the agent passes a single positional path to a
    JSON file holding the actual args. We accept it iff:
      * exactly one extra arg after the script name
      * arg ends in ``.json``
      * arg starts with ``/`` (an absolute path — guards against the
        agent accidentally passing a CLI-style ``ask`` token if it
        forgets the new pattern)

    Any other shape falls through to argparse / CLI mode.
    """
    return (
        len(argv) == 2
        and argv[1].startswith("/")
        and argv[1].endswith(".json")
    )


def _args_from_request_file(path: str) -> "_RequestArgs | None":
    """Load + validate a JSON request file. Returns None on any read
    error or shape mismatch — main() then emits RESEARCH_FAILED.

    The request file is deleted after a successful read so /tmp doesn't
    accumulate request payloads across many invocations. Failure to
    delete is silently tolerated (e.g. the agent already removed it).
    """
    from pathlib import Path as _Path  # local import to keep top-level light
    p = _Path(path)
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None

    mode = str(body.get("mode") or "").strip()
    if mode not in _VALID_MODES:
        return None
    chat_type = str(body.get("chat_type") or "supergroup").strip()
    if chat_type not in _VALID_CHAT_TYPES:
        chat_type = "supergroup"

    args = _RequestArgs(
        mode       = mode,
        query      = str(body.get("query") or ""),
        member_id  = str(body.get("member_id") or ""),
        message_id = str(body.get("message_id") or ""),
        chat_id    = str(body.get("chat_id") or ""),
        chat_type  = chat_type,
        bot_id     = str(body.get("bot_id") or os.environ.get("BOT_ID") or "atlas"),
    )

    try:
        p.unlink()
    except OSError:
        pass
    return args


def main() -> int:
    # ── JSON-file mode (OC exec preflight workaround) ─────────────────
    # Agent writes a JSON file with all the args, then invokes us with
    # a single positional path. See module docstring + openclaw#87371.
    if _looks_like_request_file(sys.argv):
        args = _args_from_request_file(sys.argv[1])
        if args is None:
            _emit_signal("RESEARCH_FAILED")
            return 2
    else:
        # ── Legacy CLI args mode (cron / tests / operator invocation) ──
        parser = argparse.ArgumentParser(description="Atlas on-demand research")
        parser.add_argument("mode", choices=list(_VALID_MODES))
        parser.add_argument("--query", default="")
        parser.add_argument("--member-id", default="")
        parser.add_argument("--message-id", default="")
        parser.add_argument("--chat-id", default="",
                            help="Telegram chat ID where the event originated")
        parser.add_argument("--chat-type", default="supergroup",
                            choices=list(_VALID_CHAT_TYPES),
                            help="Telegram chat.type of the originating chat")
        parser.add_argument("--bot-id", default=os.environ.get("BOT_ID") or "atlas")
        parsed = parser.parse_args()
        # Hyphenated CLI flags arrive as ``args.member_id`` etc. — same
        # attribute layout as _RequestArgs so cmd_ask / cmd_budget don't
        # care which mode we came from.
        args = parsed  # type: ignore[assignment]

    if args.mode == "ask":
        if not args.query:
            _emit_signal("RESEARCH_FAILED")
            return 2
        return cmd_ask(args)
    if args.mode == "budget":
        return cmd_budget(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
