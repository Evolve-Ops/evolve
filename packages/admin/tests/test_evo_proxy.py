"""tests/test_evo_proxy.py — Phase 4.1 admin UI ↔ evo gateway proxy.

Covers ``evolve_admin.evo.proxy``:

  * derive_session_id — page-id → stable session-id mapping.
  * format_page_context — structured + free-text summary blocks.
  * _parse_agent_json — OC's --json output shape, defensive.
  * send_to_evo — subprocess invocation, success / timeout / failure
    paths, page-context wrapping, env-var threading.

Subprocess is mocked via monkeypatch so tests don't depend on a real
openclaw binary or a live gateway.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

from evolve_admin.evo import proxy as P  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# derive_session_id
# ─────────────────────────────────────────────────────────────────────────────


def test_session_id_deterministic_for_page():
    """Same page_id → same session_id. Lets the OC session store
    short-term-cache across page reloads."""
    a = P.derive_session_id("alerts")
    b = P.derive_session_id("alerts")
    assert a == b
    assert a == "admin-ui-alerts"


def test_session_id_normalizes_path_chars():
    """Slashes and spaces become hyphens so session_id is filesystem-
    safe (OC's session store keys off the id; some backends use it as
    a path component)."""
    assert P.derive_session_id("maintenance/system") == "admin-ui-maintenance-system"
    assert P.derive_session_id("App Audit") == "admin-ui-app-audit"


def test_session_id_anonymous_when_page_missing():
    """No page_id, no request_id → uuid-per-call. The legacy isolation
    behavior is preserved for callers that genuinely want a unique
    thread per invocation (eg a CLI repl spawning short-lived threads).

    The route handler at ``/api/home/chat`` does NOT take this path —
    it always passes a per-browser ``request_id`` (cookie value), so
    two consecutive turns from the same browser land on the same OC
    session. See ``test_session_id_anonymous_with_request_id_is_stable``
    for the load-bearing #1367-fix invariant."""
    a = P.derive_session_id(None)
    b = P.derive_session_id(None)
    assert a.startswith("admin-ui-anon-")
    assert b.startswith("admin-ui-anon-")
    assert a != b


def test_session_id_anonymous_with_request_id_is_stable():
    """#1367 follow-up: when ``request_id`` is passed (eg the per-
    browser cookie value the route handler reads), the derived anon
    id is STABLE across calls — consecutive turns from the same
    browser share an OC session instead of fragmenting into fresh
    ``admin-ui-anon-<uuid>`` sessions per request.

    This was the root cause of evo "forgetting" earlier turns after
    idle gaps on the Chat page (see
    docs/diagnosis-evo-session-memory-loss-2026-05-20.md). The Chat-
    page client now always sends a stable ``session_id`` (Part 1),
    but the server-side fallback matters because (a) Telegram and
    other surfaces also hit this code and (b) browser cache
    invalidation can re-create the missing-session-id state for a
    turn or two. Both fixes together close the bug; either alone is
    fragile."""
    a = P.derive_session_id(None, request_id="abc123def456")
    b = P.derive_session_id(None, request_id="abc123def456")
    assert a == b, (
        "Two calls with the same request_id must produce the same "
        "session_id, otherwise the #1367 fragmentation bug is back."
    )
    assert a.startswith("admin-ui-anon-")
    # Different request_ids land on different threads — per-browser
    # isolation, not "everyone-shares-one-thread."
    c = P.derive_session_id(None, request_id="zzzz9999aaaa1111")
    assert c != a


def test_session_id_request_id_sanitized_for_filesystem_safety():
    """``request_id`` flows into a session_id which OC uses as a JSONL
    filename — so anything that confuses path resolution must be
    sanitized. Characters outside the safe charset become hyphens."""
    out = P.derive_session_id(None, request_id="hello/world../etc")
    # No slashes, no dots-in-sequence that would form path traversal.
    assert "/" not in out
    assert ".." not in out
    assert out.startswith("admin-ui-anon-")


def test_session_id_page_id_wins_over_request_id():
    """page_id is the primary anchor; request_id is only consulted as
    an anonymous fallback. Lets the Chat page's
    ``admin-ui-home-<sid>`` continue to work even if the cookie is
    set, and lets each page keep its own thread."""
    out = P.derive_session_id("alerts", request_id="cookie-value-here")
    assert out == "admin-ui-alerts"


def test_session_id_custom_prefix():
    """The prefix is configurable so other surfaces (eg a CLI repl or
    a second admin instance) can keep their own namespace."""
    assert P.derive_session_id("alerts", prefix="cli") == "cli-alerts"


# ─────────────────────────────────────────────────────────────────────────────
# format_session_context
# ─────────────────────────────────────────────────────────────────────────────


def test_session_context_empty_when_no_input():
    """No session_context dict → empty string. Caller drops the block
    entirely rather than emitting a stub that costs tokens for nothing."""
    assert P.format_session_context(None) == ""
    assert P.format_session_context("not a dict") == ""  # type: ignore[arg-type]


def test_session_context_renders_identity_and_authority_always():
    """Operator + authority anchor the block regardless of what else is
    present. Without these the model can't reason about WHO it's serving
    or what it MAY do without confirmation — the two highest-yield
    framings."""
    block = P.format_session_context({})
    assert "<session-context>" in block
    assert "Operator: pod_admin" in block
    assert "authority tier: ask" in block  # default
    assert "</session-context>" in block


def test_session_context_authority_validation():
    """Garbage authority value clamps to 'ask' (most conservative).
    Mirrors the same validation the route handler does — but defense in
    depth at the formatter level too."""
    block = P.format_session_context({"authority": "yolo"})
    assert "authority tier: ask" in block


def test_session_context_renders_local_time():
    """Frontend sends the operator's clock as ISO; the block surfaces it
    verbatim so the model anchors 'this morning' against the right
    timezone."""
    block = P.format_session_context({
        "local_time": "2026-05-19T14:32:00-07:00",
    })
    assert "2026-05-19T14:32:00-07:00" in block
    assert "Operator's local time:" in block


def test_session_context_humanizes_session_age():
    """session_age_seconds renders as compact duration so the model can
    quickly tell 'we just started' vs 'we've been at this for an hour'."""
    block = P.format_session_context({"session_age_seconds": 720})  # 12 min
    assert "12m old" in block


def test_session_context_renders_recent_actions():
    """recent_actions is the system of record for 'what did you just do'.
    Each entry shows the tool, outcome, when, and a short result
    summary."""
    block = P.format_session_context({
        "recent_actions": [
            {"tool": "pod_state.proposals.pending", "outcome": "ok",
             "when": "2m ago", "summary": "count=10"},
            {"tool": "config.bot", "outcome": "ok",
             "when": "4m ago", "summary": "keys=[bot_id, role]"},
        ],
    })
    assert "Your recent actions in this thread" in block
    assert "`pod_state.proposals.pending`" in block
    assert "2m ago" in block
    assert "count=10" in block
    assert "config.bot" in block


def test_session_context_recent_actions_caps_at_ring_size():
    """The block has a budget — show at most the configured ring size,
    not however many the caller supplies."""
    actions = [
        {"tool": f"tool_{i}", "outcome": "ok", "when": f"{i}m ago", "summary": ""}
        for i in range(20)
    ]
    block = P.format_session_context({"recent_actions": actions})
    # The ring size is _RECENT_ACTIONS_RING (currently 5). Count the
    # bullet lines that came from recent_actions specifically.
    lines = [l for l in block.splitlines() if l.startswith("  - ")]
    assert len(lines) == P._RECENT_ACTIONS_RING


def test_session_context_string_actions_pass_through():
    """recent_actions may also be a list of pre-formatted strings (eg
    when the caller has its own formatting). Handled gracefully."""
    block = P.format_session_context({
        "recent_actions": ["did the thing 3m ago"],
    })
    assert "did the thing 3m ago" in block


def test_session_context_does_not_render_zero_age():
    """A brand-new thread (session_age=0) should NOT print 'This chat
    thread is 0s old' — that's noisy and unhelpful."""
    block = P.format_session_context({"session_age_seconds": 0})
    assert "0s old" not in block
    assert "thread is" not in block


# ── tier_preference rendering (docs/spec-user-tier-control-2026-05-26.md) ────


def test_session_context_renders_tier_preference_when_explicit():
    """When the operator picked Fast / Standard / Power, the line
    must appear so the model can reference it if the operator asks.
    Routing itself is enforced by the plugin's before_model_resolve
    hook — this line is informational only."""
    for choice in ("fast", "standard", "power"):
        block = P.format_session_context({"tier_preference": choice})
        assert f"Tier preference: {choice}" in block, choice


def test_session_context_omits_tier_preference_when_auto():
    """Auto is the implicit baseline — surfacing it on every turn
    would churn the prompt cache for no value. Auto / empty / unknown
    should NOT render the line."""
    for choice in ("", "auto", "yolo", None):
        block = P.format_session_context({"tier_preference": choice})
        assert "Tier preference:" not in block, repr(choice)


def test_session_context_tier_preference_is_case_insensitive():
    """Defense-in-depth — even if upstream validation lets a
    mixed-case value through, the formatter normalizes before
    rendering so the prompt is consistent."""
    block = P.format_session_context({"tier_preference": "POWER"})
    assert "Tier preference: power" in block


def test_session_context_tier_preference_order_after_authority():
    """The line lives right after Operator/authority, before
    Local time / Surface / etc. Keeps related framing together —
    the operator's permission posture and the model picked are
    adjacent, so the model reads them as a pair."""
    block = P.format_session_context({
        "tier_preference": "power",
        "local_time": "2026-05-26T10:00:00-07:00",
    })
    authority_idx = block.index("authority tier:")
    tier_idx = block.index("Tier preference:")
    local_time_idx = block.index("Operator's local time:")
    assert authority_idx < tier_idx < local_time_idx


# ─────────────────────────────────────────────────────────────────────────────
# read_recent_actions — reading OC's session jsonl
# ─────────────────────────────────────────────────────────────────────────────


def test_read_recent_actions_missing_file_returns_empty(tmp_path):
    """First turn on a fresh thread — no session jsonl exists yet.
    Should return an empty list, not raise."""
    actions = P.read_recent_actions("nonexistent", sessions_dir=tmp_path)
    assert actions == []


def test_read_recent_actions_empty_session_id_returns_empty(tmp_path):
    """Defensive — empty session_id can't map to any file."""
    assert P.read_recent_actions("", sessions_dir=tmp_path) == []


def _make_oc_session_jsonl(tmp_path: Path, session_id: str, *records: dict) -> Path:
    """Write a synthetic OC session jsonl to mirror the production
    shape. Tests assemble (tool_use, toolResult) pairs and pass them
    in chronological order."""
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_read_recent_actions_pairs_tool_use_with_result(tmp_path):
    """A tool_use from the assistant + matching toolResult get paired
    into a single entry — that's what the model needs for context."""
    _make_oc_session_jsonl(
        tmp_path, "test-1",
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "timestamp": 1000,
                "content": [{
                    "type": "tool_use", "id": "toolu_1",
                    "name": "evo_tools__pod_state-bots",
                    "input": {},
                }],
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "timestamp": 2000,
                "toolCallId": "toolu_1",
                "content": [{
                    "type": "text",
                    "text": '{"count": 7, "bots": []}',
                }],
            },
        },
    )
    actions = P.read_recent_actions("test-1", sessions_dir=tmp_path)
    assert len(actions) == 1
    assert actions[0]["tool"] == "pod_state.bots"
    assert actions[0]["outcome"] == "ok"
    assert "count=7" in actions[0]["summary"]


def test_read_recent_actions_strips_oc_namespace():
    """OC namespaces MCP tools as ``<server>__<tool-with-hyphens>``. The
    summary should show the registered tool name (``pod_state.bots``),
    not the OC mangled form, so it matches AGENTS.md's page-tool map."""
    assert P._strip_oc_namespace("evo_tools__pod_state-bots") == "pod_state.bots"
    assert P._strip_oc_namespace("evo_tools__action-signal-snooze") == "action.signal.snooze"
    # Tools without the namespace prefix pass through
    assert P._strip_oc_namespace("exec") == "exec"


def test_read_recent_actions_labels_facade_calls_with_enum(tmp_path):
    """B7 Phase 2 tool diet: a facade call (``bot_action``, ``pod_state``,
    ...) arrives under the same advertised name for every member — the
    enum arg is what distinguishes a restart from a rollback. The ring
    must record ``facade(value)`` so 'did I already snooze that?' stays
    answerable."""
    _make_oc_session_jsonl(
        tmp_path, "test-facade",
        {"type": "message", "message": {
            "role": "assistant", "timestamp": 100,
            "content": [{"type": "tool_use", "id": "a",
                         "name": "evo_tools__bot_action",
                         "input": {"action": "restart", "bot_id": "atlas"}}],
        }},
        {"type": "message", "message": {
            "role": "toolResult", "timestamp": 200, "toolCallId": "a",
            "content": [{"type": "text", "text": '{"ok": true}'}],
        }},
        {"type": "message", "message": {
            "role": "assistant", "timestamp": 300,
            "content": [{"type": "tool_use", "id": "b",
                         "name": "evo_tools__pod_state",
                         "input": {"query": "signals.firing"}}],
        }},
        {"type": "message", "message": {
            "role": "toolResult", "timestamp": 400, "toolCallId": "b",
            "content": [{"type": "text", "text": '{"count": 2}'}],
        }},
    )
    actions = P.read_recent_actions("test-facade", sessions_dir=tmp_path)
    assert [a["tool"] for a in actions] == [
        "pod_state(signals.firing)", "bot_action(restart)",
    ]


def test_ring_tool_label_facade_without_enum_falls_back_to_name():
    """A facade call missing its enum arg (or with a non-dict input)
    still records the bare facade name rather than crashing."""
    assert P._ring_tool_label("evo_tools__bot_action", {}) == "bot_action"
    assert P._ring_tool_label("evo_tools__bot_action", None) == "bot_action"
    # Legacy canonical names (deprecated aliases / pre-diet history)
    # pass through the namespace strip unchanged.
    assert P._ring_tool_label(
        "evo_tools__action-signal-snooze", {"signal_id": "x"}
    ) == "action.signal.snooze"


def test_read_recent_actions_orders_most_recent_first(tmp_path):
    """The block lists actions newest-first because that matches how
    the operator references them ('the one you just did')."""
    _make_oc_session_jsonl(
        tmp_path, "test-2",
        {"type": "message", "message": {
            "role": "assistant", "timestamp": 100,
            "content": [{"type": "tool_use", "id": "a",
                         "name": "evo_tools__pod_state-bots"}],
        }},
        {"type": "message", "message": {
            "role": "toolResult", "timestamp": 200, "toolCallId": "a",
            "content": [{"type": "text", "text": '{"count": 1}'}],
        }},
        {"type": "message", "message": {
            "role": "assistant", "timestamp": 300,
            "content": [{"type": "tool_use", "id": "b",
                         "name": "evo_tools__pod_state-host"}],
        }},
        {"type": "message", "message": {
            "role": "toolResult", "timestamp": 400, "toolCallId": "b",
            "content": [{"type": "text", "text": '{"cpu": 0.1}'}],
        }},
    )
    actions = P.read_recent_actions("test-2", sessions_dir=tmp_path)
    assert [a["tool"] for a in actions] == ["pod_state.host", "pod_state.bots"]


def test_read_recent_actions_caps_at_limit(tmp_path):
    """Caller's `limit` is respected so we don't expand the block past
    its budget even when the session has many calls."""
    records = []
    for i in range(10):
        records.append({"type": "message", "message": {
            "role": "assistant", "timestamp": i * 100,
            "content": [{"type": "tool_use", "id": f"t{i}",
                         "name": "evo_tools__pod_state-bots"}],
        }})
        records.append({"type": "message", "message": {
            "role": "toolResult", "timestamp": i * 100 + 50,
            "toolCallId": f"t{i}",
            "content": [{"type": "text", "text": '{"count": 1}'}],
        }})
    _make_oc_session_jsonl(tmp_path, "test-3", *records)
    actions = P.read_recent_actions("test-3", limit=3, sessions_dir=tmp_path)
    assert len(actions) == 3


def test_read_recent_actions_handles_error_outcomes(tmp_path):
    """A tool that errored shows outcome='error' so the model can tell
    it shouldn't claim that data was retrieved."""
    _make_oc_session_jsonl(
        tmp_path, "test-4",
        {"type": "message", "message": {
            "role": "assistant", "timestamp": 100,
            "content": [{"type": "tool_use", "id": "x",
                         "name": "evo_tools__pod_state-proposals-pending"}],
        }},
        {"type": "message", "isError": True, "message": {
            "role": "toolResult", "timestamp": 200, "toolCallId": "x",
            "content": [{"type": "text",
                         "text": '{"error": "arbiter store unavailable"}'}],
        }},
    )
    actions = P.read_recent_actions("test-4", sessions_dir=tmp_path)
    assert len(actions) == 1
    assert actions[0]["outcome"] == "error"
    assert "error" in actions[0]["summary"].lower()


def test_read_recent_actions_malformed_lines_skipped(tmp_path):
    """A corrupt line in the jsonl shouldn't break the whole parse —
    skip it and continue. The jsonl can have garbage at any line and
    we should still get the rest of the actions."""
    p = tmp_path / "test-5.jsonl"
    p.write_text(
        "not json\n"
        + json.dumps({"type": "message", "message": {
            "role": "assistant", "timestamp": 100,
            "content": [{"type": "tool_use", "id": "a",
                         "name": "evo_tools__pod_state-bots"}],
        }}) + "\n"
        + json.dumps({"type": "message", "message": {
            "role": "toolResult", "timestamp": 200, "toolCallId": "a",
            "content": [{"type": "text", "text": '{"count": 1}'}],
        }}) + "\n"
        + "another not-json line\n"
    )
    actions = P.read_recent_actions("test-5", sessions_dir=tmp_path)
    assert len(actions) == 1
    assert actions[0]["tool"] == "pod_state.bots"


# ─────────────────────────────────────────────────────────────────────────────
# format_page_context
# ─────────────────────────────────────────────────────────────────────────────


def test_format_page_context_returns_empty_when_no_useful_content():
    """Missing / empty page_context → empty block. Avoids paying
    tokens for a zero-information wrapper."""
    assert P.format_page_context(None) == ""
    assert P.format_page_context({}) == ""
    assert P.format_page_context({"page_id": "", "summary": ""}) == ""
    assert P.format_page_context("not a dict") == ""  # type: ignore[arg-type]


def test_format_page_context_renders_page_id_and_view_as_attrs():
    """page_id + view land as XML attributes so the model can scan
    them at a glance before reading the body. ``surface`` defaults to
    ``admin_ui`` so the model knows this is browser-not-Telegram —
    closes the failure mode where evo suggested ``evo fail`` to an
    operator who was already in the admin UI."""
    block = P.format_page_context({
        "page_id": "alerts", "view": "firing", "summary": "hi",
    })
    assert block.startswith('<page-context surface="admin_ui" page="alerts" view="firing">')
    assert block.endswith("</page-context>")


def test_format_page_context_surface_attribute_always_present():
    """``surface`` is the first attribute on every emitted block, even
    when no page_id is supplied. Without it the model can't tell whether
    it's talking to a browser operator or a Telegram user."""
    block = P.format_page_context({"page_label": "X", "summary": "y"})
    assert 'surface="admin_ui"' in block
    # surface attr precedes page attr — order matters for legibility
    assert block.index("surface=") < block.index("page=")


def test_format_page_context_surface_override():
    """A caller can pin a different surface (eg an inline 'embed' or
    future Slack equivalent) — defaults but isn't forced."""
    block = P.format_page_context({
        "page_id": "x", "summary": "y", "surface": "embed",
    })
    assert 'surface="embed"' in block


def test_format_page_context_freetext_summary_passes_through():
    """A string summary is wrapped verbatim — lets the frontend
    override with bespoke prose when the structured shape doesn't fit."""
    block = P.format_page_context({
        "page_id": "x", "summary": "free text summary here",
    })
    assert "free text summary here" in block


def test_format_page_context_structured_summary_includes_all_sections():
    """Structured summary renders headline + counts + items +
    elided + tool_pointers in a deterministic, model-scannable form."""
    block = P.format_page_context({
        "page_id": "alerts", "view": "firing",
        "summary": {
            "headline": "43 firing alerts",
            "counts": {"total": 43, "critical": 2, "warn": 38},
            "items": [
                {"title": "Google_Workspace (oauth) on admin_bot",
                 "producer": "integration_probe"},
                "team_bot_a cache invalidation 52%",
            ],
            "elided_count": 38,
            "tool_pointers": [
                {"tool": "pod_state.signals.firing",
                 "for": "the full firing-alerts list"},
            ],
        },
    })
    # Each section appears
    assert "43 firing alerts" in block
    assert "total=43" in block
    assert "Google_Workspace" in block
    assert "team_bot_a cache invalidation 52%" in block
    assert "38 additional items NOT shown" in block
    assert "`pod_state.signals.firing`" in block
    assert "the full firing-alerts list" in block


def test_format_page_context_elided_zero_omits_section():
    """elided_count=0 → no 'N omitted' line. The instruction is
    redundant when nothing was elided."""
    block = P.format_page_context({
        "page_id": "alerts",
        "summary": {
            "headline": "all visible",
            "items": [{"id": "1"}],
            "elided_count": 0,
        },
    })
    assert "NOT shown above" not in block


def test_format_page_context_truncates_oversized_summary():
    """A page that forgets to summarize and dumps everything gets
    capped at the proxy boundary. The truncation notice tells the
    model to call a tool for the full data."""
    huge = "a" * 8000
    block = P.format_page_context({
        "page_id": "x", "summary": huge,
    })
    assert "[truncated by proxy" in block
    assert len(block) < 6000  # cap + envelope, well under the raw 8000


def test_format_page_context_escapes_attribute_values():
    """A maliciously-shaped page_id can't break the block's XML
    structure. Defensive — a stray quote shouldn't matter to the
    model, but it shouldn't break any downstream tooling either."""
    block = P.format_page_context({
        "page_id": 'evil"id', "summary": "hi",
    })
    assert 'page="evil&quot;id"' in block


def test_format_page_context_renders_dict_items_as_kv():
    """Dict items become 'k=v, k=v' — deterministic + scannable for
    the model. Plain-string items pass through unchanged."""
    block = P.format_page_context({
        "page_id": "x",
        "summary": {"items": [
            {"id": "a1", "score": 9},
            "literal string",
        ]},
    })
    assert "id=a1, score=9" in block
    assert "literal string" in block


def test_format_page_context_renders_available_actions():
    """``available_actions`` surfaces inline buttons so the model can
    answer in terms of what the operator can click — closes the failure
    mode where evo invented 'Dashboard → team_bot_b → Config' instead of
    naming the 'Take this on' button right next to the proposal."""
    block = P.format_page_context({
        "page_id": "recommendations",
        "summary": {
            "headline": "10 pending proposals",
            "available_actions": [
                {"label": "Take this on", "description": "applies the proposal"},
                {"label": "Snooze 1w", "description": "defers a week"},
                {"label": "Dismiss"},
            ],
        },
    })
    assert "On-screen actions" in block
    assert "**Take this on**" in block
    assert "applies the proposal" in block
    assert "**Snooze 1w**" in block
    assert "**Dismiss**" in block
    # Explicit anti-fabrication nudge in the rendered text
    assert "do NOT invent navigation paths" in block


def test_format_page_context_actions_optional():
    """No available_actions → no section. Pages without inline
    affordances (eg a read-only digest) keep the summary lean."""
    block = P.format_page_context({
        "page_id": "x",
        "summary": {"headline": "hi"},
    })
    assert "On-screen actions" not in block


# ─────────────────────────────────────────────────────────────────────────────
# _parse_agent_json — OC's --json output shape
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_agent_json_happy_path():
    """The shape we get from openclaw 2026.5.18 — pinned by an
    integration-tested round-trip on the mini, replicated here."""
    raw = json.dumps({
        "runId": "abc123",
        "status": "ok",
        "result": {
            "payloads": [{"text": "hi from evo", "mediaUrl": None}],
            "meta": {
                "agentMeta": {
                    "sessionId": "admin-ui-alerts",
                    "model": "claude-sonnet-4-6",
                },
                "usage": {"input": 100, "output": 50},
            },
        },
    })
    text, model, usage, run_id = P._parse_agent_json(raw)
    assert text == "hi from evo"
    assert model == "claude-sonnet-4-6"
    assert usage == {"input": 100, "output": 50}
    assert run_id == "abc123"


def test_parse_agent_json_missing_fields_defaults():
    """Defensive against OC shape drift — missing fields → safe
    defaults, never raises."""
    text, model, usage, run_id = P._parse_agent_json("{}")
    assert text == ""
    assert model is None
    assert usage == {}
    assert run_id is None


def test_parse_agent_json_malformed():
    """Non-JSON output → empty reply. Caller's send_to_evo upgrades
    this to an operator-facing error message; the parser is just a
    shape transform."""
    text, model, usage, run_id = P._parse_agent_json("not json at all")
    assert text == ""
    assert model is None


# ─────────────────────────────────────────────────────────────────────────────
# send_to_evo — subprocess invocation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patched_subprocess(monkeypatch):
    """Replaces subprocess.run with a recorder. Tests get a `calls`
    list of (cmd, kwargs) plus knobs to control returncode + stdout."""

    class Recorder:
        def __init__(self):
            self.calls: list[tuple[list[str], dict]] = []
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""
            self.raise_exc: Exception | None = None

        def __call__(self, cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if self.raise_exc:
                raise self.raise_exc
            return subprocess.CompletedProcess(
                args=cmd, returncode=self.returncode,
                stdout=self.stdout, stderr=self.stderr,
            )

    rec = Recorder()
    monkeypatch.setattr(P.subprocess, "run", rec)
    return rec


def _make_oc_json(text="hi", model="claude-sonnet-4-6", run_id="r1"):
    """Helper — build a plausible openclaw agent --json envelope."""
    return json.dumps({
        "runId": run_id, "status": "ok",
        "result": {
            "payloads": [{"text": text}],
            "meta": {"agentMeta": {"model": model, "sessionId": "x"}},
        },
    })


def test_send_to_evo_invokes_openclaw_agent(patched_subprocess, tmp_path):
    """Happy path — successful subprocess, JSON parsed, ProxyResult
    populated. Verifies the cmd shape so a future migration to direct
    WS-RPC has a single explicit boundary."""
    patched_subprocess.stdout = _make_oc_json(text="real reply")
    result = P.send_to_evo(
        "hello",
        session_id="admin-ui-test",
        network_path=tmp_path / "network.json",
    )
    assert result.text == "real reply"
    assert result.session_id == "admin-ui-test"
    assert result.model == "claude-sonnet-4-6"
    assert result.error is None

    # Inspect the command — verifies CLI surface OC depends on
    cmd, _kw = patched_subprocess.calls[0]
    assert "agent" in cmd
    assert "--json" in cmd
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "admin-ui-test"
    assert "--message" in cmd


def test_send_to_evo_wraps_page_context(patched_subprocess, tmp_path):
    """page_context is rendered as <page-context> at the head of the
    user message — the proxy's whole reason for existing."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "what's wrong?",
        session_id="s1",
        network_path=tmp_path / "n.json",
        page_context={"page_id": "alerts",
                      "summary": "43 firing alerts"},
    )
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    assert msg.startswith("<page-context")
    assert "43 firing alerts" in msg
    assert msg.endswith("what's wrong?")


def test_send_to_evo_wraps_session_context_before_page(patched_subprocess, tmp_path):
    """Session-context renders FIRST so identity + authority frame the
    interaction before the page-state. Layering convention from spec
    §3.7 — identity precedes situation precedes message."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hello",
        session_id="s1",
        network_path=tmp_path / "n.json",
        page_context={"page_id": "alerts", "summary": "x"},
        session_context={"authority": "auto-small",
                         "local_time": "2026-05-19T10:00:00Z",
                         "recent_actions": []},
    )
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    sc_idx = msg.index("<session-context>")
    pc_idx = msg.index("<page-context")
    assert sc_idx < pc_idx, (
        "session-context must precede page-context in the user message"
    )
    assert "authority tier: auto-small" in msg
    assert "2026-05-19T10:00:00Z" in msg


def test_send_to_evo_session_context_without_page(patched_subprocess, tmp_path):
    """Session-context can stand alone (eg Telegram-like clients with no
    page state). Block renders, message follows."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hi",
        session_id="s1",
        network_path=tmp_path / "n.json",
        session_context={"authority": "ask", "recent_actions": []},
    )
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    assert msg.startswith("<session-context>")
    assert "<page-context" not in msg
    assert msg.endswith("hi")


def test_send_to_evo_injects_branch_d_followup(patched_subprocess, tmp_path, monkeypatch):
    """Branch-D follow-through (2026-07-28 Backup-page incident): a
    pending no_evidence_reject marker on this session is consumed and
    injected as an <inspector-follow-through> block ahead of the
    operator's message, naming the promised read tool. Consume-on-read:
    the second turn carries no block."""
    from evolve_admin.evo.inspector import record_pending_followup

    monkeypatch.setenv("EVOLVE_SHARED_DIR", str(tmp_path))
    assert record_pending_followup(
        "s1",
        read_tool="pod_state.backup_status(bot_id=...)",
        original_excerpt="run sudo chown to fix the backup",
        surface="admin_ui",
        surface_type="laptop",
    ) is True

    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "any luck?",
        session_id="s1",
        network_path=tmp_path / "n.json",
        session_context={"authority": "ask", "recent_actions": []},
    )
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    assert "<inspector-follow-through>" in msg
    assert "pod_state.backup_status(bot_id=...)" in msg
    # Layering: identity first, then the follow-through directive, then
    # the operator's message last.
    assert msg.index("<session-context>") < msg.index("<inspector-follow-through>")
    assert msg.endswith("any luck?")
    # Telemetry marks the injection (dead-end measurability).
    telemetry = (tmp_path / "logs" / "inspector.jsonl").read_text()
    assert "no_evidence_followup_injected" in telemetry

    # Second turn on the same session: marker consumed, no block.
    P.send_to_evo(
        "and now?",
        session_id="s1",
        network_path=tmp_path / "n.json",
        session_context={"authority": "ask", "recent_actions": []},
    )
    cmd2, _ = patched_subprocess.calls[1]
    msg2 = cmd2[cmd2.index("--message") + 1]
    assert "<inspector-follow-through>" not in msg2


def test_send_to_evo_no_followup_block_without_marker(patched_subprocess, tmp_path, monkeypatch):
    """No pending marker → the message shape is unchanged (regression
    guard for the injection seam)."""
    monkeypatch.setenv("EVOLVE_SHARED_DIR", str(tmp_path))
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hi",
        session_id="s-clean",
        network_path=tmp_path / "n.json",
        session_context={"authority": "ask", "recent_actions": []},
    )
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    assert "<inspector-follow-through>" not in msg
    assert msg.endswith("hi")


def test_send_to_evo_auto_fills_recent_actions_from_session_log(
    patched_subprocess, tmp_path, monkeypatch,
):
    """When session_context omits recent_actions, the proxy reads them
    from OC's session jsonl. Closes the audit-recall gap automatically
    — callers don't have to track state themselves."""
    patched_subprocess.stdout = _make_oc_json()

    # Stub read_recent_actions so the test doesn't depend on a real
    # session file layout
    captured: dict = {}

    def fake_reader(session_id, *, limit=5, sessions_dir=None):
        captured["called_with"] = session_id
        return [{"tool": "pod_state.bots", "outcome": "ok",
                 "when": "1m ago", "summary": "count=7"}]

    monkeypatch.setattr(P, "read_recent_actions", fake_reader)

    P.send_to_evo(
        "hi",
        session_id="admin-ui-test",
        network_path=tmp_path / "n.json",
        session_context={"authority": "ask"},  # no recent_actions
    )
    assert captured["called_with"] == "admin-ui-test"
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    assert "1m ago" in msg
    assert "`pod_state.bots`" in msg


def test_send_to_evo_explicit_empty_recent_actions_disables_read(
    patched_subprocess, tmp_path, monkeypatch,
):
    """A caller can pass an explicit empty list to suppress the
    auto-fill (eg first turn on a brand-new thread where we KNOW there
    are no prior actions). Without this escape hatch we'd waste a file
    read every time."""
    patched_subprocess.stdout = _make_oc_json()
    called = []
    monkeypatch.setattr(
        P, "read_recent_actions", lambda *a, **k: called.append(1) or [],
    )
    P.send_to_evo(
        "hi", session_id="s1", network_path=tmp_path / "n.json",
        session_context={"authority": "ask", "recent_actions": []},
    )
    assert called == [], "explicit recent_actions=[] should not trigger reader"


def test_send_to_evo_no_page_context_sends_message_bare(patched_subprocess, tmp_path):
    """No page_context → no XML block prefix, just the user's
    message. Cheaper for Telegram-style calls that lack a UI context."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    cmd, _ = patched_subprocess.calls[0]
    msg = cmd[cmd.index("--message") + 1]
    assert msg == "hi"
    assert "<page-context" not in msg


def test_send_to_evo_empty_message_short_circuits(patched_subprocess, tmp_path):
    """Empty/whitespace message → no subprocess. Defensive; the route
    handler also rejects empties at 400 but this is the safety net."""
    result = P.send_to_evo("  ", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "empty_message"
    assert patched_subprocess.calls == []


def test_send_to_evo_timeout_returns_clean_error(patched_subprocess, tmp_path, monkeypatch):
    """Subprocess timeout doesn't raise to the caller — surfaces as a
    ProxyResult with error='timeout' and operator-facing text. Mocks the
    gateway probe to ``live=True`` so the timeout doesn't get reclassified
    as gateway_down (the new fallback path is exercised separately)."""
    patched_subprocess.raise_exc = subprocess.TimeoutExpired(
        cmd=["openclaw"], timeout=1,
    )
    monkeypatch.setattr(P, "_evo_gateway_status", lambda: (True, 19030))
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "timeout"
    assert "didn't respond" in result.text or "timed out" in result.text.lower()


def test_send_to_evo_openclaw_missing_returns_clean_error(
    patched_subprocess, tmp_path
):
    """No openclaw binary → ProxyResult with explicit error. Important
    for the CI-without-OC case + for catching deploy regressions."""
    patched_subprocess.raise_exc = FileNotFoundError("no openclaw")
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "openclaw_not_found"
    assert "not found" in result.text.lower()


def test_send_to_evo_nonzero_exit_surfaces_stderr(patched_subprocess, tmp_path, monkeypatch):
    """openclaw exit ≠ 0 → ProxyResult with stderr tail in text.
    Operator's chat bubble shows something actionable instead of a
    blank bubble. Mocks ``live=True`` so this stays a generic openclaw
    error rather than getting promoted to gateway_down."""
    patched_subprocess.returncode = 7
    patched_subprocess.stderr = "Error: tool registry empty"
    monkeypatch.setattr(P, "_evo_gateway_status", lambda: (True, 19030))
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error is not None
    assert result.error.startswith("openclaw_rc=7")
    assert "tool registry empty" in result.text


def test_send_to_evo_empty_payload_returns_defensive_text(
    patched_subprocess, tmp_path, monkeypatch,
):
    """OC returned 0 but with no payload text — defensive surface so
    the chat bubble doesn't render blank. Mocks the gateway probe to
    ``live=True`` so the empty payload stays the legacy placeholder
    instead of being reclassified as gateway_down."""
    patched_subprocess.stdout = json.dumps({"result": {"payloads": []}})
    monkeypatch.setattr(P, "_evo_gateway_status", lambda: (True, 19030))
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "empty_reply"
    assert result.text == "(evo returned an empty reply)"


# ─────────────────────────────────────────────────────────────────────────────
# Gateway-down fallback — every subprocess failure path with a confirmed
# down gateway must surface ``error="gateway_down"`` so the Chat UI can
# offer the diagnostic-LLM fallback. Regression for the 2026-06-03
# OC-upgrade outage that produced an opaque "(evo returned an empty
# reply)" bubble while every gateway was down.
# ─────────────────────────────────────────────────────────────────────────────


def test_send_to_evo_empty_payload_with_down_gateway_returns_gateway_down(
    patched_subprocess, tmp_path, monkeypatch,
):
    patched_subprocess.stdout = json.dumps({"result": {"payloads": []}})
    monkeypatch.setattr(P, "_evo_gateway_status", lambda: (False, 19030))
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "gateway_down"
    assert "19030" in result.text
    # Operator copy mentions the diagnostic-LLM fallback so the bubble
    # makes the next step obvious without UI help.
    assert "diagnostic" in result.text.lower()


def test_send_to_evo_nonzero_exit_with_down_gateway_returns_gateway_down(
    patched_subprocess, tmp_path, monkeypatch,
):
    """The common outage shape — openclaw fails to connect, exits non-zero,
    gateway probe confirms it's down. The stderr-tail message is replaced
    by the gateway_down structured response."""
    patched_subprocess.returncode = 1
    patched_subprocess.stderr = "Error: connection refused"
    monkeypatch.setattr(P, "_evo_gateway_status", lambda: (False, 19030))
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "gateway_down"
    assert "diagnostic" in result.text.lower()


def test_send_to_evo_timeout_with_down_gateway_returns_gateway_down(
    patched_subprocess, tmp_path, monkeypatch,
):
    patched_subprocess.raise_exc = subprocess.TimeoutExpired(
        cmd=["openclaw"], timeout=1,
    )
    monkeypatch.setattr(P, "_evo_gateway_status", lambda: (False, 19030))
    result = P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    assert result.error == "gateway_down"
    assert "diagnostic" in result.text.lower()


def test_send_to_evo_sets_env_for_subprocess(patched_subprocess, tmp_path):
    """Network path is threaded as EVOLVE_NETWORK_PATH so the bridge
    + any sub-tooling resolves the same network.json the proxy used.
    Matches the env-var pattern the Phase 2.1 MCP bridge uses."""
    patched_subprocess.stdout = _make_oc_json()
    np = tmp_path / "n.json"
    P.send_to_evo("hi", session_id="s1", network_path=np)
    _cmd, kwargs = patched_subprocess.calls[0]
    env = kwargs.get("env") or {}
    assert env.get("EVOLVE_NETWORK_PATH") == str(np)


def test_send_to_evo_uses_tmp_cwd(patched_subprocess, tmp_path):
    """cwd=/tmp because openclaw is Node and calls process.cwd() at
    startup — running from a directory the user can't read aborts
    with EACCES. Same fix the alerts dispatcher uses."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    _cmd, kwargs = patched_subprocess.calls[0]
    assert kwargs.get("cwd") == "/tmp"


# ── Tier preference plumbing (docs/spec-user-tier-control-2026-05-26.md) ─────


def test_send_to_evo_sets_tier_preference_env_for_power(patched_subprocess, tmp_path):
    """When the caller supplies tier_preference="power", the
    EVOLVE_TIER_PREFERENCE env var must reach the subprocess so the
    plugin's before_model_resolve hook can pick it up via
    process.env. The plugin is per-turn-stateful (env-per-subprocess
    is exactly per-turn) — this is the entire mechanism by which the
    operator's pick gets to the router."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hi", session_id="s1", network_path=tmp_path / "n.json",
        tier_preference="power",
    )
    _cmd, kwargs = patched_subprocess.calls[0]
    env = kwargs.get("env") or {}
    assert env.get("EVOLVE_TIER_PREFERENCE") == "power"


def test_send_to_evo_normalizes_tier_preference_case(patched_subprocess, tmp_path):
    """The proxy is the trust boundary for env-var values — anything
    we set must already be one of the four valid choices in
    lower-case. Mixed-case input normalizes; the plugin then trusts
    the env value verbatim."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hi", session_id="s1", network_path=tmp_path / "n.json",
        tier_preference="POWER",
    )
    _cmd, kwargs = patched_subprocess.calls[0]
    assert (kwargs.get("env") or {}).get("EVOLVE_TIER_PREFERENCE") == "power"


def test_send_to_evo_omits_tier_env_for_auto(patched_subprocess, tmp_path):
    """tier_preference="auto" (the default) means "no override" —
    the env var must NOT be set, so the plugin falls through to
    classifier-driven routing. Setting it to the literal "auto"
    would either be ignored (current plugin behavior — see
    ModelRouter.setUserTier) or interpreted as a tier, both wrong.
    Cleanest semantics: only set the env when the operator explicitly
    picked a non-Auto value."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hi", session_id="s1", network_path=tmp_path / "n.json",
        tier_preference="auto",
    )
    _cmd, kwargs = patched_subprocess.calls[0]
    env = kwargs.get("env") or {}
    assert "EVOLVE_TIER_PREFERENCE" not in env


def test_send_to_evo_omits_tier_env_when_unset(patched_subprocess, tmp_path):
    """Backwards-compat — callers that haven't been updated to pass
    tier_preference (e.g. older code paths, Telegram surface in v1)
    still work. No tier_preference kwarg → no env var."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo("hi", session_id="s1", network_path=tmp_path / "n.json")
    _cmd, kwargs = patched_subprocess.calls[0]
    env = kwargs.get("env") or {}
    assert "EVOLVE_TIER_PREFERENCE" not in env


def test_send_to_evo_omits_tier_env_for_garbage(patched_subprocess, tmp_path):
    """Defense-in-depth: even if upstream validation lets a bad
    value through, the proxy refuses to set the env. The plugin
    treats unknown values as "no override" anyway, but not setting
    keeps the env clean and makes debugging easier (an env var
    that's set is a positive signal)."""
    patched_subprocess.stdout = _make_oc_json()
    P.send_to_evo(
        "hi", session_id="s1", network_path=tmp_path / "n.json",
        tier_preference="ultra-mega-power",
    )
    _cmd, kwargs = patched_subprocess.calls[0]
    env = kwargs.get("env") or {}
    assert "EVOLVE_TIER_PREFERENCE" not in env


def test_send_to_evo_tier_env_does_not_clobber_network_path(
    patched_subprocess, tmp_path,
):
    """Both env vars travel together — adding tier shouldn't drop the
    existing EVOLVE_NETWORK_PATH plumbing."""
    patched_subprocess.stdout = _make_oc_json()
    np = tmp_path / "net.json"
    P.send_to_evo(
        "hi", session_id="s1", network_path=np,
        tier_preference="fast",
    )
    _cmd, kwargs = patched_subprocess.calls[0]
    env = kwargs.get("env") or {}
    assert env.get("EVOLVE_NETWORK_PATH") == str(np)
    assert env.get("EVOLVE_TIER_PREFERENCE") == "fast"
