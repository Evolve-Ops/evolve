"""Tests for context_census — the context-efficiency census.

Pins (brief: internal/dispatch/done/context-efficiency-census.md):
  1. Splits sum EXACTLY to the provider's reported input total, at every
     level (top / conversation / per tool / fresh-vs-recarried).
  2. Fixed context comes from OC's own compiled context when present.
  3. Cache-break attribution names the planted block change, the model
     swap and the cache-window gap — and never says "unexplained" for them.
  4. No cache fields ⇒ honest "not reported", and the cache lever is None.
  5. The re-emission detector finds the planted document written three
     times (one exact, one near-duplicate inside a tool-call argument) and
     ignores a document emitted once.
  6. Session shape: mechanical calls and the power-model share.
  7. The money line never claims a negative cost and prices the two misses.
  8. Every one-pager sentence passes the readability gate.
  9. Unreadable files are listed, never counted; checkpoint files are ignored.
 10. The CLI wrapper loads and ``--json-out`` writes the JSON report.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import context_census as cc
from dossier import readability

SESSION_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
BASE = datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)

DOC = ("Day 1: fly to Lisbon, check in, walk Alfama. " * 12).strip()  # ~540 chars → ≥ 100 tokens
DOC_NEAR = DOC.replace("Alfama", "Belem", 1)  # one word changed
OTHER_DOC = ("Packing list: charger, passport, adapters, shoes, hat. " * 10).strip()

SYSTEM_PROMPT = "S" * 8000
TOOLS = [
    {"name": "web_search", "description": "d" * 200, "parameters": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "e" * 300, "parameters": {"type": "object", "properties": {}}},
]
TOOLS_CHARS = len(json.dumps(TOOLS, ensure_ascii=False, default=str))


def _ts(minutes: float) -> str:
    return (BASE + __import__("datetime").timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _msg(mid: str, minutes: float, message: dict) -> dict:
    return {"type": "message", "id": mid, "parentId": None, "timestamp": _ts(minutes), "message": message}


def _assistant(mid: str, minutes: float, content: list, *, model: str, usage: dict) -> dict:
    return _msg(mid, minutes, {
        "role": "assistant", "provider": "anthropic" if model.startswith("claude") else "openai",
        "model": model, "api": "anthropic-messages", "stopReason": "stop", "usage": usage, "content": content,
    })


def _usage(inp: int, read: int, write: int, out: int, *, cache: bool = True) -> dict:
    usage = {"input": inp, "output": out, "totalTokens": inp + read + write + out, "cost": {"total": 0}}
    if cache:
        usage["cacheRead"] = read
        usage["cacheWrite"] = write
    return usage


def session_a_records() -> list[dict]:
    """The 'Sunday' session: 3 turns, 5 calls, 2 tool calls, the document
    written three times, two cold misses with distinct planted causes."""
    return [
        {"type": "session", "version": 3, "id": SESSION_A, "timestamp": _ts(0), "cwd": "/x"},
        # turn 0
        _msg("u0", 0, {"role": "user", "content": [{"type": "text", "text": "P" * 200}]}),
        _assistant("a0", 0.1, [{"type": "toolCall", "id": "tc0", "name": "web_search", "arguments": {"q": "x" * 100}}],
                   model="claude-opus-4-5", usage=_usage(100, 0, 5000, 50)),
        _msg("r0", 0.2, {"role": "toolResult", "toolCallId": "tc0", "toolName": "web_search",
                         "content": [{"type": "text", "text": "R" * 4000}], "isError": False}),
        _assistant("a1", 0.3, [{"type": "text", "text": DOC}], model="claude-opus-4-5",
                   usage=_usage(200, 5000, 1100, 600)),
        # turn 1 — cold miss; the ledger says the digest block changed
        _msg("u1", 1.0, {"role": "user", "content": "revise please"}),
        _assistant("a2", 1.1, [{"type": "toolCall", "id": "tc1", "name": "send_message",
                                "arguments": {"to": "me", "body": DOC_NEAR}}],
                   model="claude-opus-4-5", usage=_usage(150, 0, 7000, 550)),
        _msg("r1", 1.2, {"role": "toolResult", "toolCallId": "tc1", "toolName": "send_message",
                         "content": [{"type": "text", "text": "sent ok"}], "isError": False}),
        _assistant("a3", 1.3, [{"type": "text", "text": "Sent."}], model="claude-opus-4-5",
                   usage=_usage(50, 7000, 200, 10)),
        # turn 2 — cold miss after a 10-minute gap on a different model; prefix stable
        _msg("u2", 12.0, {"role": "user", "content": "again"}),
        _assistant("a4", 12.1, [{"type": "text", "text": DOC}, {"type": "text", "text": OTHER_DOC}],
                   model="claude-sonnet-4-5", usage=_usage(300, 0, 8000, 700)),
    ]


def session_b_records(*, cache: bool) -> list[dict]:
    return [
        {"type": "session", "version": 3, "id": SESSION_B, "timestamp": _ts(30), "cwd": "/x"},
        _msg("u0", 30, {"role": "user", "content": "hi"}),
        _assistant("a0", 30.1, [{"type": "text", "text": OTHER_DOC}], model="gpt-4o",
                   usage=_usage(900, 0, 0, 120, cache=cache)),
    ]


def trajectory_a() -> list[dict]:
    recs = []
    for run in ("r0", "r1", "r2"):
        recs.append({"type": "context.compiled", "runId": run, "sessionId": SESSION_A,
                     "data": {"systemPrompt": SYSTEM_PROMPT, "tools": TOOLS, "messages": []}})
        recs.append({"type": "trace.artifacts", "runId": run, "sessionId": SESSION_A,
                     "data": {"promptCache": {"retention": "short"}}})
    return recs


def ledger_a() -> list[dict]:
    def rec(minutes: float, digest: str) -> dict:
        return {"schema_version": 1, "type": "prefix_hash", "ts": _ts(minutes), "bot_id": "personal-bot",
                "session_id": SESSION_A, "path": "blocks", "prefix_sha256": "p" + digest,
                "appended_block_shas": {"capabilities": "cap", "digest": digest, "narrative": None,
                                        "speaker": None, "cost_downgrade": None}, "combined_chars": 3000}
    return [rec(0, "d1"), rec(1.0, "d2"), rec(12.0, "d2")]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def pod(tmp_path: Path) -> dict:
    sessions = tmp_path / "home" / ".openclaw" / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    _write_jsonl(sessions / f"{SESSION_A}.jsonl", session_a_records())
    _write_jsonl(sessions / f"{SESSION_A}.trajectory.jsonl", trajectory_a())
    _write_jsonl(sessions / f"{SESSION_A}.checkpoint.cccccccc-cccc-4ccc-8ccc-cccccccccccc.jsonl", session_a_records())
    _write_jsonl(sessions / f"{SESSION_B}.jsonl", session_b_records(cache=True))
    (sessions / "sessions.json").write_text(json.dumps({
        "agent:main:telegram:12345": {"sessionId": SESSION_A, "route": {"channel": "telegram"}, "chatType": "direct"},
        "agent:main:explicit:evolve:tier-classifier:1": {
            "sessionId": SESSION_B,
            "systemPromptReport": {"systemPrompt": {"chars": 5000}, "tools": {"schemaChars": 1500, "listChars": 500}},
        },
    }), encoding="utf-8")
    shared = tmp_path / "shared"
    turns = shared / "personal-bot" / "turns"
    turns.mkdir(parents=True)
    _write_jsonl(turns / "prefix-hashes-2026-08-30.jsonl", ledger_a())
    (turns / "context-footprint.json").write_text(json.dumps(
        {"schema_version": 1, "bot_id": "personal-bot", "tier": "full", "tools": [{"name": "defer", "chars": 1454}],
         "total_chars": 22071, "tool_count": 1}), encoding="utf-8")
    return {"sessions": sessions, "shared": shared, "root": tmp_path}


def _census(pod: dict, **kw) -> cc.Census:
    return cc.collect("personal-bot", 3, session_dirs=[pod["sessions"]], shared_dir=pod["shared"], now=NOW,
                      min_doc_tokens=100, **kw)


def _session(census: cc.Census, sid: str) -> cc.Session:
    return next(s for s in census.sessions if s.session_id == sid)


# ── 1. exact sums ─────────────────────────────────────────────────────────────
def test_splits_sum_exactly_to_reported_input(pod: dict) -> None:
    census = _census(pod)
    for sess in census.sessions:
        for call in sess.calls:
            comp = cc.call_composition(call)
            assert sum(comp["top"].values()) == call.prompt_tokens
            assert sum(comp["conversation"].values()) == comp["top"]["conversation"]
            tool_tokens = comp["conversation"].get("tool_result", 0)
            assert sum(comp["tool_results_by_tool"].values()) == tool_tokens
            assert comp["tool_results_fresh"] + comp["tool_results_recarried"] == tool_tokens
            # CE-2a: the tool-DESCRIPTION table totals the tool_schemas bucket
            # the one-pager quotes — the table can never say something else.
            assert sum(comp["tool_schemas_by_tool"].values()) == comp["top"]["tool_schemas"]
    total = census.composition["prompt_tokens_total"]
    assert sum(census.composition["top"].values()) == total
    assert total == sum(c.prompt_tokens for s in census.sessions for c in s.calls)
    comp = census.composition
    assert sum(comp["tool_schemas_by_tool"].values()) == comp["top"]["tool_schemas"]
    assert sum(comp["tool_schemas_by_kind"].values()) == comp["top"]["tool_schemas"]
    assert sum(comp["prompt_tokens_by_kind"].values()) == total
    assert sum(comp["calls_by_kind"].values()) == comp["calls"]


def test_split_exact_largest_remainder() -> None:
    out = cc.split_exact(10, {"a": 1, "b": 1, "c": 1})
    assert sum(out.values()) == 10 and sorted(out.values()) == [3, 3, 4]
    assert cc.split_exact(7, {"a": 0, "b": 0}) == {"a": 0, "b": 0, "unattributed": 7}
    assert cc.split_exact(0, {"a": 5}) == {"a": 0}


# ── 2. fixed context sources ─────────────────────────────────────────────────
def test_fixed_context_from_trajectory_then_index_then_footprint(pod: dict) -> None:
    census = _census(pod)
    a = _session(census, SESSION_A)
    assert all(c.fixed_source == "trajectory" for c in a.calls)
    assert a.calls[0].fixed_prefix_chars == len(SYSTEM_PROMPT)
    assert a.calls[0].tool_schema_chars == TOOLS_CHARS
    assert a.kind == "user" and a.channel == "telegram"
    assert a.cache_retention == "short"
    b = _session(census, SESSION_B)
    assert b.calls[0].fixed_source == "session index report"
    assert b.calls[0].fixed_prefix_chars == 5000 and b.calls[0].tool_schema_chars == 2000
    assert b.kind == "evolve_internal"
    assert census.composition["fixed_unmeasured_calls"] == 0


def test_fixed_context_falls_back_to_tool_footprint_then_unmeasured() -> None:
    sess = cc.parse_transcript(SESSION_A, session_a_records(), 400)
    cc.apply_fixed_context(sess, [], None, {"total_chars": 22071})
    assert sess.calls[0].tool_schema_chars == 22071
    assert sess.calls[0].fixed_prefix_chars is None
    assert "Evolve tools only" in sess.calls[0].fixed_source
    cc.apply_fixed_context(sess, [], None, None)
    assert sess.calls[0].fixed_source == "unmeasured"
    comp = cc.call_composition(sess.calls[0])
    assert comp["top"]["fixed_prefix"] == 0 and not comp["fixed_measured"]


# ── re-carry ─────────────────────────────────────────────────────────────────
def test_tool_results_recarried_after_the_consuming_call(pod: dict) -> None:
    a = _session(_census(pod), SESSION_A)
    c1 = cc.call_composition(a.calls[1])  # consumed the web_search result
    assert c1["tool_results_fresh"] > 0 and c1["tool_results_recarried"] == 0
    c2 = cc.call_composition(a.calls[2])  # re-carries it
    assert c2["tool_results_fresh"] == 0 and c2["tool_results_recarried"] > 0
    assert set(c2["tool_results_by_tool"]) == {"web_search"}
    c4 = cc.call_composition(a.calls[4])
    assert set(c4["tool_results_by_tool"]) == {"web_search", "send_message"}
    share = _census(pod).composition["recarried_share"]
    assert share is not None and 0.5 < share < 1.0


# ── 3. cache-break attribution ───────────────────────────────────────────────
def test_cache_break_attribution_matches_planted_causes(pod: dict) -> None:
    cache = _census(pod).cache
    assert cache["status"] == "reported"
    assert cache["cold_misses"] == 2
    assert cache["ledger_sessions_matched"] == 1
    by_call = {m["call"]: m["causes"] for m in cache["misses"]}
    assert by_call[2] == ["prefix block changed: digest"]
    assert any(c.startswith("model changed: claude-opus-4-5 -> claude-sonnet-4-5") for c in by_call[4])
    assert any(c.startswith("gap over the cache window (5 min)") for c in by_call[4])
    assert not any("unexplained" in c or "no ledger" in c for causes in by_call.values() for c in causes)
    assert sum(r["misses"] for r in cache["cause_table"]) == 2
    assert 0 < (cache["hit_rate_tokens"] or 0) < 1


def test_cache_miss_without_ledger_is_named_not_guessed(pod: dict) -> None:
    for p in (pod["shared"] / "personal-bot" / "turns").glob("prefix-hashes-*.jsonl"):
        p.unlink()
    census = _census(pod)
    by_call = {m["call"]: m["causes"] for m in census.cache["misses"]}
    assert by_call[2] == ["no ledger record for this turn"]
    assert cc.say("note_ledger_none") in census.notes


def test_ledger_pairs_by_time_when_armed_mid_session(pod: dict) -> None:
    """The live case: the ledger starts partway through a session. Ordinal
    pairing would mis-attribute both misses; time pairing must not."""
    turns = pod["shared"] / "personal-bot" / "turns"
    _write_jsonl(turns / "prefix-hashes-2026-08-30.jsonl", ledger_a()[1:])  # drop the turn-0 record
    by_call = {m["call"]: m["causes"] for m in _census(pod).cache["misses"]}
    assert by_call[2] == ["first ledger record for this session (nothing to compare)"]
    assert not any("no ledger" in c or "unexplained" in c for c in by_call[4])
    assert any(c.startswith("model changed") for c in by_call[4])
    # A record stamped AFTER the call belongs to a later turn, never to this one.
    late = [dict(r, ts=_ts(50.0)) for r in ledger_a()]
    _write_jsonl(turns / "prefix-hashes-2026-08-30.jsonl", late)
    by_call = {m["call"]: m["causes"] for m in _census(pod).cache["misses"]}
    assert by_call[2] == ["no ledger record for this turn"]


def test_sessions_are_counted_whole_and_straddling_is_noted(pod: dict) -> None:
    census = cc.collect("personal-bot", 3, session_dirs=[pod["sessions"]], shared_dir=pod["shared"],
                        now=BASE + __import__("datetime").timedelta(days=3, minutes=5))
    a = _session(census, SESSION_A)
    assert len(a.calls) == 5 and [c.ordinal for c in a.calls] == [0, 1, 2, 3, 4]
    assert cc.say("note_straddle", n=1) in census.notes
    assert census.cache["cold_misses"] == 2


def test_cache_ttl_override_and_compaction_cause() -> None:
    records = session_a_records()
    # Compact right before the last call: the cause list must say so.
    records.insert(-1, {"type": "compaction", "id": "cmp", "timestamp": _ts(11.9), "summary": "s" * 400,
                        "firstKeptEntryId": "u2", "tokensBefore": 1})
    sess = cc.parse_transcript(SESSION_A, records, 400)
    assert sess.compactions == 1 and sess.calls[4].after_compaction
    kinds = [e.kind for e in sess.calls[4].history]
    assert kinds[0] == "compaction_summary" and kinds[1:] == ["user_text"]
    cache = cc.cache_report([sess], ledger_a(), ttl_minutes=60)
    by_call = {m["call"]: m["causes"] for m in cache["misses"]}
    assert "history compacted before this call" in by_call[4]
    assert not any("gap over" in c for c in by_call[4])


# ── 4. no cache fields ───────────────────────────────────────────────────────
def test_no_cache_fields_is_reported_honestly(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_jsonl(sessions / f"{SESSION_B}.jsonl", session_b_records(cache=False))
    census = cc.collect("personal-bot", 3, session_dirs=[sessions], shared_dir=tmp_path / "shared", now=NOW)
    assert census.cache["status"] == "not_reported"
    assert cc.say("cache_none") in cc.one_pager(census)
    assert census.money is not None and census.money["levers"]["prefix_cached"] is None
    assert "not measurable (cache not reported)" in cc.render(census)


def test_cache_fields_all_zero_is_distinct_from_absent(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_jsonl(sessions / f"{SESSION_B}.jsonl", session_b_records(cache=True))
    census = cc.collect("personal-bot", 3, session_dirs=[sessions], shared_dir=tmp_path / "shared", now=NOW)
    assert census.cache["status"] == "reported_zero"
    assert cc.say("cache_zero") in cc.one_pager(census)


# ── 5. re-emission ───────────────────────────────────────────────────────────
def test_reemission_finds_planted_document_and_ignores_single(pod: dict) -> None:
    reemit = _census(pod).reemission
    assert len(reemit["groups"]) == 1
    group = reemit["groups"][0]
    assert group["session_id"] == SESSION_A
    assert group["copies"] == 3 and group["calls"] == [1, 2, 4]
    assert group["kinds"] == ["text", "tool_call_args"] and group["tools"] == ["send_message"]
    assert group["exact"] is False  # one copy is the near-duplicate
    assert group["repeat_output_tokens_est"] == 2 * group["unit_tokens_est"]
    # OTHER_DOC appears once in A and once in B — never in the same session.
    assert reemit["documents_seen"] == 5


def test_reemission_exact_only_and_threshold() -> None:
    units = [cc.OutputUnit("s", i, f"m{i}", "text", None, len(DOC), DOC) for i in range(2)]
    (group,) = cc.find_reemissions(units)
    assert group["exact"] is True and group["copies"] == 2
    assert cc.find_reemissions(units[:1]) == []
    small = cc.parse_transcript(SESSION_A, session_a_records(), min_doc_chars=10_000)
    assert small.units == []


# ── 6. session shape ─────────────────────────────────────────────────────────
def test_session_shape_and_power_rung(pod: dict) -> None:
    shape = _census(pod).shape
    assert shape["sessions"] == 2 and shape["calls"] == 6 and shape["tool_calls"] == 2
    assert shape["longest_unrotated"]["session_id"] == SESSION_A
    assert shape["longest_unrotated"]["calls"] == 5 and shape["longest_unrotated"]["turns"] == 3
    assert shape["mechanical_calls"] == 2
    assert shape["power_model"] == "claude-opus-4-5"
    assert shape["mechanical_on_power_model"] == 2 and shape["mechanical_power_share"] == 1.0
    assert _census(pod, power_model="gpt-4o").shape["mechanical_on_power_model"] == 0


# ── 7. money line ────────────────────────────────────────────────────────────
def test_money_line_replays_the_costliest_session(pod: dict) -> None:
    money = _census(pod).money
    assert money is not None and money["session_id"] == SESSION_A
    assert money["priced_calls"] == 5 and money["actual_cost"] > 0
    levers = money["levers"]
    assert levers["prefix_cached"]["tokens"] == 7000 + 8000
    assert levers["evict_tool_results"]["tokens"] > 0
    assert levers["docs_by_reference"]["tokens"] > 0
    for lever in levers.values():
        assert lever is not None and 0 <= lever["after"] <= money["actual_cost"]
        assert lever["saved"] >= 0
    assert money["all_levers"]["after"] >= 0
    assert money["all_levers"]["saved"] == pytest.approx(sum(v["saved"] for v in levers.values()))
    assert len(money["assumptions"]) >= 5


def test_money_line_session_override_and_missing(pod: dict) -> None:
    assert _census(pod, money_session=SESSION_B).money["session_id"] == SESSION_B
    census = _census(pod, money_session="nope")
    assert census.money["session_id"] == SESSION_A
    assert any("not found" in n for n in census.notes)


def test_money_line_values_are_pinned(pod: dict) -> None:
    money = _census(pod).money
    # Two misses: 7,000 opus write tokens at (18.75 - 1.50)/MTok + 8,000 sonnet at (3.75 - 0.30)/MTok.
    assert money["levers"]["prefix_cached"]["saved"] == pytest.approx(7000 * 17.25e-6 + 8000 * 3.45e-6)
    assert _census(pod).cache["hit_rate_tokens"] == pytest.approx(12000 / (12000 + 21300 + 1700))
    # The output half of the docs lever never exceeds what a call actually emitted.
    group = _census(pod).reemission["groups"][0]
    a = _session(_census(pod), SESSION_A)
    cap = sum(min(group["unit_tokens_est"], a.calls[o].output_tokens) for o in group["calls"][1:])
    assert money["levers"]["docs_by_reference"]["tokens"] >= cap
    assert money["clamped"] is False


def test_money_line_clamp_is_said_not_hidden(pod: dict) -> None:
    records = session_a_records()
    for rec in records:
        msg = rec.get("message") or {}
        if msg.get("role") == "assistant":
            msg["usage"]["cost"] = {"total": 0.0001}  # a recorded cost far below list price
    _write_jsonl(pod["sessions"] / f"{SESSION_A}.jsonl", records)
    census = _census(pod, money_session=SESSION_A)  # B now out-prices A on list price
    assert census.money["clamped"] is True
    lines = cc.one_pager(census)
    assert cc.say("money_clamped") in lines
    assert not any(line.startswith("If ") for line in lines)


def test_odd_usage_values_and_bad_ledger_shapes_do_not_crash(pod: dict) -> None:
    records = session_a_records()
    records[2]["message"]["usage"]["cacheRead"] = "5000.0"
    records[2]["message"]["usage"]["input"] = "n/a"
    records[2]["message"]["usage"]["output"] = -7
    _write_jsonl(pod["sessions"] / f"{SESSION_A}.jsonl", records)
    bad_ledger = ledger_a()
    bad_ledger[1]["appended_block_shas"] = ["digest"]
    _write_jsonl(pod["shared"] / "personal-bot" / "turns" / "prefix-hashes-2026-08-30.jsonl", bad_ledger)
    census = _census(pod)
    call0 = _session(census, SESSION_A).calls[0]
    assert (call0.cache_read, call0.input_tokens, call0.output_tokens) == (5000, 0, 0)
    assert census.cache["cold_misses"] == 2
    by_call = {m["call"]: m["causes"] for m in census.cache["misses"]}
    assert by_call[2] == ["ledger record unreadable (block map malformed)"]


def test_money_lever_rates_are_blended_and_output_anchored(pod: dict) -> None:
    census = _census(pod)
    a = _session(census, SESSION_A)
    money = census.money
    # Evict lever: re-carried tool-result tokens at each call's BLENDED input rate
    # (opus list: full 15, cache read 1.50, cache write 18.75 per MTok).
    rates = {"claude-opus-4-5": (15.0, 1.5, 18.75), "claude-sonnet-4-5": (3.0, 0.3, 3.75)}
    expected = 0.0
    for call in a.calls:
        comp = cc.call_composition(call)
        full, cr, cw = rates[call.model]
        blended = (call.input_tokens * full + call.cache_read * cr + call.cache_write * cw) / call.prompt_tokens
        expected += comp["tool_results_recarried"] * blended / 1e6
    assert money["levers"]["evict_tool_results"]["saved"] == pytest.approx(expected)
    # Docs lever output half: capped at the call's reported output tokens.
    group = census.reemission["groups"][0]
    records = session_a_records()
    for rec in records:
        msg = rec.get("message") or {}
        if msg.get("role") == "assistant":
            msg["usage"]["output"] = 5  # every call emitted only 5 tokens
    _write_jsonl(pod["sessions"] / f"{SESSION_A}.jsonl", records)
    capped = _census(pod).money["levers"]["docs_by_reference"]
    # The carry half is unchanged (same history, same prompt totals); only the
    # output half shrinks, from the estimate to 5 tokens per repeat copy.
    uncapped = money["levers"]["docs_by_reference"]
    assert uncapped["tokens"] - capped["tokens"] == (group["copies"] - 1) * (group["unit_tokens_est"] - 5)
    assert 0 < capped["saved"] < uncapped["saved"]


def test_unresolvable_compaction_keeps_everything_and_is_noted() -> None:
    records = session_a_records()
    records.insert(-1, {"type": "compaction", "id": "cmp", "timestamp": _ts(11.9), "summary": "s" * 400,
                        "firstKeptEntryId": "model_change_id", "tokensBefore": 1})
    sess = cc.parse_transcript(SESSION_A, records, 400)
    assert sess.compactions_unresolved == 1
    kinds = [e.kind for e in sess.calls[4].history]
    assert kinds[0] == "compaction_summary" and "tool_result" in kinds and kinds[-1] == "user_text"


def test_mechanical_requires_no_text_and_near_dup_bar_holds() -> None:
    records = session_a_records()
    records[2]["message"]["content"].append({"type": "text", "text": "Searching now."})
    sess = cc.parse_transcript(SESSION_A, records, 400)
    assert sess.calls[0].mechanical is False and sess.calls[2].mechanical is True
    words = [f"w{i}" for i in range(200)]
    doc_a = " ".join(words)
    doc_b = " ".join(words[:80] + [f"z{i}" for i in range(120)])  # ~40% shingle overlap
    units = [cc.OutputUnit("s", 0, "m0", "text", None, len(doc_a), doc_a),
             cc.OutputUnit("s", 1, "m1", "text", None, len(doc_b), doc_b)]
    assert cc.find_reemissions(units) == []
    doc_c = " ".join(words[:170] + [f"z{i}" for i in range(30)])  # well over half
    units[1] = cc.OutputUnit("s", 1, "m1", "text", None, len(doc_c), doc_c)
    assert len(cc.find_reemissions(units)) == 1


# ── 7b. CE-2a: tool descriptions per tool, per session kind ──────────────────
def test_tool_descriptions_are_reported_per_tool_and_per_session_kind(pod: dict) -> None:
    """The census can now say WHICH tool descriptions every call carried and
    WHICH session kinds paid for them — the measurement gap CE-2b acts on."""
    census = _census(pod)
    comp = census.composition
    by_tool = comp["tool_schemas_by_tool"]
    # Session A's trajectory names its two tools; session B knows only a total.
    assert set(by_tool) == {"web_search", "send_message", "unattributed"}
    assert by_tool["send_message"] > by_tool["web_search"] > 0  # the heavier description
    assert comp["tool_schemas_unmeasured_calls"] == 1  # session B: aggregate only
    # Which kinds registered each named tool.
    assert comp["tool_schema_kinds_by_tool"]["web_search"] == ["user"]
    assert "evolve_internal" not in comp["tool_schema_kinds_by_tool"]["send_message"]
    # Per-kind attribution covers both sessions.
    assert set(comp["tool_schemas_by_kind"]) == {"user", "evolve_internal"}
    assert set(comp["calls_by_kind"]) == {"user", "evolve_internal"}
    assert comp["calls_by_kind"]["evolve_internal"] == 1
    text = cc.render(census)
    assert "tool descriptions by tool" in text
    assert "tool descriptions by session kind" in text


def test_toolless_calls_share_of_tool_descriptions_is_reported(pod: dict) -> None:
    """The chip's money question: how much of the tool-description spend went
    to calls that never called a tool."""
    census = _census(pod)
    comp = census.composition
    idle = comp["tool_schemas_on_toolless_calls"]
    schemas = comp["top"]["tool_schemas"]
    expected = sum(
        cc.call_composition(c)["top"]["tool_schemas"]
        for s in census.sessions for c in s.calls if not c.tool_calls
    )
    assert idle == expected and 0 < idle < schemas
    assert cc.say("tool_idle", idle_pct=cc._pct(idle, schemas)) in cc.one_pager(census)


def test_per_tool_weights_apportion_the_array_punctuation(pod: dict) -> None:
    """``_tool_definition_chars`` spreads the JSON array's own brackets and
    commas over the definitions instead of dropping them, so the per-tool table
    reconciles with the total the census already reported."""
    chars = cc._tool_definition_chars(TOOLS, TOOLS_CHARS)
    assert sum(chars.values()) == TOOLS_CHARS
    assert set(chars) == {"web_search", "send_message"}
    assert cc._tool_definition_chars([], 0) == {}
    # Two definitions sharing a name fold together — the name is what rides.
    folded = cc._tool_definition_chars([{"name": "a"}, {"name": "a"}], 40)
    assert folded == {"a": 40}


# ── 7c. session kinds: the "unknown" the 2026-09-02 census could not name ────
def test_session_kinds_name_what_unknown_used_to_hide(pod: dict) -> None:
    """A bare ``explicit`` key is a one-shot dispatch; a session with no index
    row is unindexed. Both used to report as ``unknown``."""
    index_path = pod["sessions"] / "sessions.json"
    index = json.loads(index_path.read_text())
    # Re-key session B as a BARE one-shot (no evolve tag) and drop A's row.
    index["agent:main:explicit:00000000-0000-4000-8000-000000000000"] = index.pop(
        "agent:main:explicit:evolve:tier-classifier:1")
    del index["agent:main:telegram:12345"]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    census = _census(pod)
    assert _session(census, SESSION_B).kind == "oneshot"
    assert _session(census, SESSION_A).kind == "unindexed"
    assert "unknown" not in census.composition["calls_by_kind"]
    for sess in census.sessions:
        assert sess.kind in cc.ALL_KINDS


# ── 8. readability ───────────────────────────────────────────────────────────
def test_every_sentence_template_passes_the_readability_gate() -> None:
    for key, template in cc.SENTENCES.items():
        assert readability.check(template) == [], (key, template)
    for key, label in cc.LEVER_LABELS.items():
        text = cc.SENTENCES["money_lever"].format(lever=label, after="1.00", before="2.00")
        assert readability.check(text) == [], (key, text)


def test_rendered_one_pager_passes_the_readability_gate(pod: dict) -> None:
    census = _census(pod)
    lines = cc.one_pager(census)
    assert len(lines) >= 10
    for line in lines:
        assert readability.check(line) == [], line
    assert cc.say("misses", misses=2) in lines
    text = cc.render(census)
    assert "cache-break causes:" in text and "prefix block changed: digest" in text
    assert "Money line" in text and "all three" in text


# ── 9. failure posture ───────────────────────────────────────────────────────
def test_unreadable_files_listed_not_counted_and_checkpoints_ignored(pod: dict) -> None:
    bad = pod["sessions"] / "dddddddd-dddd-4ddd-8ddd-dddddddddddd.jsonl"
    bad.mkdir()  # a directory wearing a session file's name: open() fails
    census = _census(pod)
    assert str(bad) in census.unreadable
    assert {s.session_id for s in census.sessions} == {SESSION_A, SESSION_B}  # checkpoint not a session
    assert cc.say("unreadable", n=1) in cc.one_pager(census)
    assert census.sources["transcripts_read"] == 2


def test_window_excludes_old_sessions(pod: dict) -> None:
    census = cc.collect("personal-bot", 1, session_dirs=[pod["sessions"]], shared_dir=pod["shared"],
                        now=NOW + __import__("datetime").timedelta(days=5))
    assert census.sessions == [] and census.composition["calls"] == 0
    assert cc.say("no_calls", bot="personal-bot", days=1) in cc.one_pager(census)
    assert census.cache["status"] == "no_calls" and census.money is None


def test_torn_tail_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text(json.dumps({"a": 1}) + "\n{\"torn\": ", encoding="utf-8")
    assert cc.load_jsonl(path) == [{"a": 1}]
    assert cc.load_jsonl(tmp_path / "missing.jsonl") == []
    assert cc.load_json(tmp_path / "missing.json") is None


# ── 10. CLI + wrapper ────────────────────────────────────────────────────────
def test_cli_json_out_and_read_only(pod: dict, capsys: pytest.CaptureFixture[str]) -> None:
    out = pod["root"] / "census.json"
    before = sorted(p.name for p in pod["sessions"].iterdir())
    rc = cc.main(["--bot", "personal-bot", "--days", "3000", "--sessions-dir", str(pod["sessions"]),
                  "--shared-dir", str(pod["shared"]), "--json-out", str(out), "--min-doc-tokens", "100"])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == cc.CENSUS_SCHEMA_VERSION and doc["bot_id"] == "personal-bot"
    assert doc["cache"]["cold_misses"] == 2 and doc["reemission"]["groups"][0]["copies"] == 3
    assert sorted(p.name for p in pod["sessions"].iterdir()) == before  # nothing written beside the transcripts
    assert "Context-efficiency census" in capsys.readouterr().out
    rc = cc.main(["--bot", "personal-bot", "--days", "3000", "--sessions-dir", str(pod["sessions"]),
                  "--shared-dir", str(pod["shared"]), "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["bot_id"] == "personal-bot"


def test_cli_rejects_bad_days_and_reports_unwritable_json_out(pod: dict, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cc.main(["--bot", "personal-bot", "--days", "0", "--sessions-dir", str(pod["sessions"])])
    rc = cc.main(["--bot", "personal-bot", "--days", "3000", "--sessions-dir", str(pod["sessions"]),
                  "--shared-dir", str(pod["shared"]), "--json-out", str(pod["root"] / "missing" / "x.json")])
    assert rc == 2 and "cannot write" in capsys.readouterr().err


def test_tool_wrapper_loads_the_analyzer_main() -> None:
    tool = Path(__file__).resolve().parents[3] / "tools" / "context-efficiency-census"
    loader = importlib.machinery.SourceFileLoader("context_efficiency_census_tool", str(tool))
    spec = importlib.util.spec_from_loader("context_efficiency_census_tool", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["context_efficiency_census_tool"] = mod
    loader.exec_module(mod)
    assert mod.main is cc.main
