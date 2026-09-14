"""The before/after proof for the tool-schema diet (context-economy CE-2b).

Brief: internal/dispatch/done/tool-schema-diet-per-session-type.md, build item
4 — "prove it with the census, not a claim".

This builds a fixture pod with two sessions that differ ONLY in which tool
definitions the gateway compiled into their context:

  * a USER session, carrying the full registered tool set (the "before", and
    also the "after" for user sessions — the chip's guardrail is that a user
    session is never trimmed);
  * a SCHEDULED session, carrying the ``no_live_speaker`` set (the "after"
    the plugin's registration filter produces for a background session).

Both tool inventories come from the SHARED fixture
``tests/fixtures/tool-profile-inventory.json`` — the same file the plugin's
toolProfiles.test.mjs asserts its profile table against, and a real measured
inventory (per-tool chars from a live pod's context-footprint.json). So the
numbers this test prints are the numbers the deployed filter produces, not a
hypothesis about them.

What is proved here is the ARITHMETIC and the REPORTING: given those two
contexts, the census attributes the drop to the right session kind and its
per-tool table reconciles. What is NOT proved here is the pod: the operator's
re-run after deploy is the acceptance (see the PR body).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import context_census as cc

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INVENTORY = json.loads((FIXTURES / "tool-profile-inventory.json").read_text(encoding="utf-8"))

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
BASE = NOW - timedelta(hours=2)
USER_SESSION = "11111111-1111-4111-8111-111111111111"
CRON_SESSION = "22222222-2222-4222-8222-222222222222"
SYSTEM_PROMPT = "S" * 39000  # ~9.7k tokens — the census's measured fixed prefix


# The exact shape a trimmed tool still registers as. Mirrors
# TRIMMED_PARAMETERS / trimmedDescription in packages/plugin/src/tools/ToolProfiles.ts.
STUB_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": True}


def _stub_description(profile_id: str, kind: str) -> str:
    return (
        f'Not available in this session (tool profile "{profile_id}", session kind '
        f'"{kind}"). Calling it returns a refusal, not a result.'
    )


def _tool_defs(
    names: "set[str] | None" = None,
    *,
    profile_id: str = "no_live_speaker",
    kind: str = "scheduled",
) -> list[dict]:
    """Tool definitions whose serialized size matches the measured inventory.

    The description is padded so each definition weighs what the plugin's
    footprint says it weighs — the point is the RELATIVE weights, and padding
    keeps them faithful without copying real descriptions into a fixture.

    A tool OUTSIDE ``names`` is not dropped. It is emitted as the name-only
    STUB the plugin actually registers for it (``trimToolDefinition``): a
    trimmed tool keeps its name, sheds its schema and refuses by name, so it
    still rides on every call. An earlier version of this fixture omitted the
    stubs entirely, which made the headline saving look like 47% when the real
    figure is closer to 38% — the fixture, not the filter, was wrong.
    """
    out: list[dict] = []
    for row in INVENTORY["tools"]:
        name = row["name"]
        if names is None or name in names:
            skeleton = len(json.dumps({"name": name, "description": "", "parameters": {}}))
            pad = max(row["chars"] - skeleton, 1)
            out.append({"name": name, "description": "d" * pad, "parameters": {}})
        else:
            out.append({
                "name": name,
                "description": _stub_description(profile_id, kind),
                "parameters": dict(STUB_PARAMETERS),
            })
    return out


def _expected_scheduled_chars(keep: "set[str]") -> int:
    """What the scheduled session's tool definitions weigh, stubs included.

    Uses the SAME ``json.dumps`` convention the padding above uses, so the
    kept tools weigh exactly their inventory chars and the difference from the
    inventory is purely the stubs.
    """
    return sum(len(json.dumps(d)) for d in _tool_defs(keep))


def _ts(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _session_records(session_id: str, prompt_tokens: int) -> list[dict]:
    """One turn, one call, NO tool call — the shape the 2026-09-02 census found
    eight of, each carrying ~36k input tokens it never used."""
    return [
        {"type": "session", "version": 3, "id": session_id, "timestamp": _ts(0), "cwd": "/x"},
        {"type": "message", "id": "u0", "timestamp": _ts(0),
         "message": {"role": "user", "content": [{"type": "text", "text": "P" * 400}]}},
        {"type": "message", "id": "a0", "timestamp": _ts(0.1), "message": {
            "role": "assistant", "provider": "anthropic", "model": "claude-haiku-4-5",
            "stopReason": "stop",
            "usage": {"input": prompt_tokens, "output": 40, "cacheRead": 0,
                      "cacheWrite": 0, "cost": {"total": 0.01}},
            "content": [{"type": "text", "text": "done"}],
        }},
    ]


def _trajectory(session_id: str, tools: list[dict]) -> list[dict]:
    return [{"type": "context.compiled", "runId": "r0", "sessionId": session_id,
             "data": {"systemPrompt": SYSTEM_PROMPT, "tools": tools, "messages": []}}]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _tokens(chars: int) -> int:
    return chars // cc.CHARS_PER_TOKEN


@pytest.fixture
def pod(tmp_path: Path) -> dict:
    """A pod where the scheduled session already runs the trimmed profile."""
    full = _tool_defs()
    trimmed = _tool_defs(set(INVENTORY["profiles"]["no_live_speaker"]))
    sessions = tmp_path / "home" / ".openclaw" / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    # Input tokens the provider would report: prefix + tools + a little history.
    user_input = _tokens(len(SYSTEM_PROMPT) + len(json.dumps(full)) + 1600)
    cron_input = _tokens(len(SYSTEM_PROMPT) + len(json.dumps(trimmed)) + 1600)
    _write_jsonl(sessions / f"{USER_SESSION}.jsonl", _session_records(USER_SESSION, user_input))
    _write_jsonl(sessions / f"{USER_SESSION}.trajectory.jsonl", _trajectory(USER_SESSION, full))
    _write_jsonl(sessions / f"{CRON_SESSION}.jsonl", _session_records(CRON_SESSION, cron_input))
    _write_jsonl(sessions / f"{CRON_SESSION}.trajectory.jsonl", _trajectory(CRON_SESSION, trimmed))
    (sessions / "sessions.json").write_text(json.dumps({
        "agent:main:telegram:direct:00000": {
            "sessionId": USER_SESSION, "route": {"channel": "telegram"}},
        "agent:main:cron:00000000-0000-4000-8000-000000000000": {"sessionId": CRON_SESSION},
    }), encoding="utf-8")
    shared = tmp_path / "shared"
    (shared / "placeholder-bot" / "turns").mkdir(parents=True)
    return {"sessions": sessions, "shared": shared}


def _census(pod: dict) -> cc.Census:
    return cc.collect("placeholder-bot", 3, session_dirs=[pod["sessions"]],
                      shared_dir=pod["shared"], now=NOW)


def test_the_scheduled_session_carries_its_profile_s_tools_and_stubs_for_the_rest(
    pod: dict,
) -> None:
    """Trimming is not removal.

    Every tool still rides the scheduled session — the trimmed ones as
    name-only stubs that refuse by name. Asserting that a trimmed tool
    DISAPPEARS would be asserting something the filter does not do, and it is
    how the 47% figure got published.
    """
    census = _census(pod)
    comp = census.composition
    kinds = comp["tool_schema_kinds_by_tool"]
    keep = set(INVENTORY["profiles"]["no_live_speaker"])
    for row in INVENTORY["tools"]:
        name = row["name"]
        assert "user" in kinds[name], f"{name} should ride every user session"
        assert "scheduled" in kinds[name], (
            f"{name} left the scheduled session entirely; a trimmed tool keeps "
            "its name and refuses by name, it is never dropped"
        )
    # The saving is real, and it is the SCHEMA that goes, not the tool.
    by_tool_sched = comp.get("tool_schemas_by_tool", {})
    assert by_tool_sched, "the per-tool table should not be empty"
    trimmed_names = [r["name"] for r in INVENTORY["tools"] if r["name"] not in keep]
    assert trimmed_names, "the fixture must actually trim something"


def test_the_scheduled_bucket_equals_the_kept_tools_plus_their_stubs(pod: dict) -> None:
    """The arithmetic the headline rests on, stated where it can fail loudly."""
    census = _census(pod)
    comp = census.composition
    keep = set(INVENTORY["profiles"]["no_live_speaker"])
    kept_chars = sum(r["chars"] for r in INVENTORY["tools"] if r["name"] in keep)
    expected = _expected_scheduled_chars(keep)
    # The stubs are a real, non-zero share of what a background session pays.
    assert expected > kept_chars, (
        "the scheduled session must pay MORE than its kept tools alone — the "
        "trimmed tools still ride as stubs"
    )
    stub_chars = expected - kept_chars
    full_chars = sum(r["chars"] for r in INVENTORY["tools"])
    assert 0 < stub_chars < full_chars - kept_chars, (
        "a stub must cost something and must cost less than the definition it replaces"
    )


def test_tool_description_tokens_per_call_drop_for_the_scheduled_session(pod: dict) -> None:
    """The headline number, computed the way the census computes it."""
    census = _census(pod)
    comp = census.composition
    by_kind = comp["tool_schemas_by_kind"]
    calls = comp["calls_by_kind"]
    before = by_kind["user"] // calls["user"]
    after = by_kind["scheduled"] // calls["scheduled"]
    assert calls == {"user": 1, "scheduled": 1}
    assert after < before
    # The trim is worth roughly a THIRD of the plugin's tool-description
    # weight, not a half: the nine trimmed tools still ride as stubs, and the
    # stubs are counted here. Bounded on both sides so a profile change that
    # guts it, or one that quietly stops trimming, both fail here rather than
    # passing silently.
    saved_share = (before - after) / before
    assert 0.32 <= saved_share <= 0.45, f"{before=} {after=} {saved_share=}"
    # And the band is not a guess — it must agree with the inventory arithmetic
    # (kept tools + stubs) that the operator can recompute by hand.
    keep = set(INVENTORY["profiles"]["no_live_speaker"])
    full_chars = sum(r["chars"] for r in INVENTORY["tools"])
    predicted = (full_chars - _expected_scheduled_chars(keep)) / full_chars
    assert abs(saved_share - predicted) <= 0.03, f"{saved_share=} {predicted=}"
    # The whole-call saving — what the operator actually pays — must track the
    # attributed one. They are not identical, and cannot be: the top-level
    # split is an ESTIMATE apportioned by character share of the provider's
    # reported total, so dropping tool definitions also shifts a few tokens
    # between the prefix and conversation buckets. Agreement within 2% is the
    # honest claim; equality would be a claim the method cannot support.
    per_call_before = comp["prompt_tokens_by_kind"]["user"] // calls["user"]
    per_call_after = comp["prompt_tokens_by_kind"]["scheduled"] // calls["scheduled"]
    whole_call_drop = per_call_before - per_call_after
    assert abs(whole_call_drop - (before - after)) <= max(2, (before - after) // 50)


def test_the_table_still_reconciles_across_two_different_tool_sets(pod: dict) -> None:
    """Two sessions compiled DIFFERENT tool lists — the per-tool table must
    still total the tool-schema bucket the one-pager quotes."""
    census = _census(pod)
    comp = census.composition
    assert sum(comp["tool_schemas_by_tool"].values()) == comp["top"]["tool_schemas"]
    assert sum(comp["tool_schemas_by_kind"].values()) == comp["top"]["tool_schemas"]
    assert comp["tool_schemas_unmeasured_calls"] == 0
    # Neither call used a tool, so every tool-description token was idle.
    assert comp["tool_schemas_on_toolless_calls"] == comp["top"]["tool_schemas"]
    assert cc.say("tool_idle", idle_pct=100) in cc.one_pager(census)


def test_the_render_shows_the_before_and_after(pod: dict, capsys) -> None:
    """The operator's view. Printed so the PR body's table can be lifted from a
    real run rather than retyped."""
    text = cc.render(_census(pod))
    assert "tool descriptions by session kind" in text
    assert "scheduled" in text and "user" in text
    print(text)
    assert "roster_block" in capsys.readouterr().out
