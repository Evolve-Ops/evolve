"""tests/test_exec_outcome_watchdog.py — exec_outcome_watchdog producer tests.

Phase 1 (annotation-based): tool_error_burst detector.
Phase 2 (content-inspection): exec_denied, approval_timeout, preflight_block.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import exec_outcome_watchdog as eow  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────


def _annotation(
    *,
    bot_id: str = "security_bot",
    session_id: str = "s-1",
    turn_id: str | None = None,
    ts: str = "2026-05-28T10:00:00Z",
    tool_error_count: int = 0,
    tool_retry_count: int = 0,
    restart_markers: int = 0,
) -> dict:
    return {
        "type": "turn_annotation",
        "schema_version": 2,
        "turn_id": turn_id or f"t-{session_id}",
        "session_id": session_id,
        "ts": ts,
        "bot_id": bot_id,
        "struggle_features": {
            "tool_error_count": tool_error_count,
            "tool_retry_count": tool_retry_count,
            "restart_markers": restart_markers,
            "clarification_loops": 0,
            "tokens_per_progress": 0,
        },
    }


def _burst_kwargs(**overrides):
    base = dict(window_days=7, min_count=5, min_sessions=2, max_per_run=1)
    base.update(overrides)
    return base


# ── detect_tool_error_burst ─────────────────────────────────────────────────


def test_tool_error_burst_fires_above_threshold():
    annotations = [
        _annotation(session_id="s-1", tool_error_count=3),
        _annotation(session_id="s-2", tool_error_count=4),
    ]
    out = eow.detect_tool_error_burst("security_bot", annotations, **_burst_kwargs())
    assert len(out) == 1
    d = out[0]["details"]
    assert d["tool_error_total"] == 7
    assert d["sessions_with_errors"] == 2
    assert out[0]["severity"] == "warn"


def test_tool_error_burst_alert_severity_at_2x():
    annotations = [
        _annotation(session_id=f"s-{i}", tool_error_count=3) for i in range(5)
    ]
    out = eow.detect_tool_error_burst("team_bot_a", annotations, **_burst_kwargs())
    assert out[0]["severity"] == "alert"


def test_tool_error_burst_quiet_below_min_count():
    annotations = [
        _annotation(session_id="s-1", tool_error_count=2),
        _annotation(session_id="s-2", tool_error_count=2),
    ]
    out = eow.detect_tool_error_burst("admin_bot", annotations, **_burst_kwargs())
    assert out == []


def test_tool_error_burst_quiet_below_min_sessions():
    """All errors in one session — that's session_token_outlier's job."""
    annotations = [
        _annotation(session_id="s-1", tool_error_count=10),
    ]
    out = eow.detect_tool_error_burst("personal_bot", annotations, **_burst_kwargs())
    assert out == []


def test_tool_error_burst_quiet_on_empty_annotations():
    assert eow.detect_tool_error_burst("team_bot_a", [], **_burst_kwargs()) == []


def test_tool_error_burst_worst_session_picked():
    annotations = [
        _annotation(session_id="quiet", tool_error_count=2),
        _annotation(session_id="worst", tool_error_count=8),
        _annotation(session_id="medium", tool_error_count=4),
    ]
    out = eow.detect_tool_error_burst("security_bot", annotations, **_burst_kwargs())
    assert out[0]["details"]["worst_session_id"] == "worst"
    assert out[0]["details"]["worst_session_errors"] == 8


def test_tool_error_burst_signature_per_bot():
    annotations_a = [
        _annotation(bot_id="team_bot_a", session_id="s-1", tool_error_count=3),
        _annotation(bot_id="team_bot_a", session_id="s-2", tool_error_count=3),
    ]
    annotations_b = [
        _annotation(bot_id="team_bot_c", session_id="s-1", tool_error_count=3),
        _annotation(bot_id="team_bot_c", session_id="s-2", tool_error_count=3),
    ]
    out_a = eow.detect_tool_error_burst("team_bot_a", annotations_a, **_burst_kwargs())
    out_b = eow.detect_tool_error_burst("team_bot_c", annotations_b, **_burst_kwargs())
    assert out_a[0]["signature"] != out_b[0]["signature"]


# ── read_turn_annotations ───────────────────────────────────────────────────


def test_read_turn_annotations_skips_non_annotation_records(tmp_path):
    """File may contain session_summary records too — filter to turn_annotation."""
    ann_dir = tmp_path / "annotations" / "security_bot"
    ann_dir.mkdir(parents=True)
    import json
    content = "\n".join([
        json.dumps(_annotation(session_id="s-1", tool_error_count=1)),
        json.dumps({"type": "session_summary", "session_id": "s-1"}),
        json.dumps(_annotation(session_id="s-2", tool_error_count=1)),
    ])
    (ann_dir / "2026-05-28.jsonl").write_text(content)

    out = eow.read_turn_annotations(
        tmp_path, "security_bot", days=1, today=date(2026, 5, 28),
    )
    assert len(out) == 2
    assert all(r["type"] == "turn_annotation" for r in out)


def test_read_turn_annotations_empty_when_missing(tmp_path):
    out = eow.read_turn_annotations(
        tmp_path, "ghost", days=7, today=date(2026, 5, 28),
    )
    assert out == []


def test_read_turn_annotations_sorts_ascending(tmp_path):
    ann_dir = tmp_path / "annotations" / "security_bot"
    ann_dir.mkdir(parents=True)
    import json
    content = "\n".join([
        json.dumps(_annotation(
            session_id="s-late", ts="2026-05-28T20:00:00Z", tool_error_count=1
        )),
        json.dumps(_annotation(
            session_id="s-early", ts="2026-05-28T05:00:00Z", tool_error_count=1
        )),
    ])
    (ann_dir / "2026-05-28.jsonl").write_text(content)

    out = eow.read_turn_annotations(
        tmp_path, "security_bot", days=1, today=date(2026, 5, 28),
    )
    assert [r["session_id"] for r in out] == ["s-early", "s-late"]


# ── allowlist-miss precedence (OC's real production phrasing) ───────────────


def test_classify_allowlist_miss_overrides_timeout():
    """OC's real failure message: 'Exec denied (...approval-timeout
    (allowlist-miss)): cmd'. Both 'denied' and 'approval-timeout' phrases
    are present, but allowlist-miss is the actual root cause. Right
    classification is denied (operator extends allowlist) not timeout
    (operator changes channel — wrong fix)."""
    text = (
        "Exec denied (gateway id=3c2c9afb-8462-4ad6-9395-9648c220a582, "
        "approval-timeout (allowlist-miss)): ps aux | grep pod_admin_user"
    )
    assert eow._classify_tool_result(text) == "denied"


def test_classify_allowlist_subscript_matches_too():
    text = "Exec denied (allowlist): ls /var/log"
    assert eow._classify_tool_result(text) == "denied"


# ── command extraction (OC's nested-parens shape) ──────────────────────────


def test_extract_command_tool_name_handles_nested_parens():
    text = (
        "Exec denied (gateway id=abc, approval-timeout (allowlist-miss)): "
        "ps aux | grep \"pod_admin_user.*openclaw-gateway\""
    )
    assert eow._extract_command_tool_name(text) == "ps"


def test_extract_command_tool_name_handles_simple_form():
    text = "Exec denied: python3 ops/tools/refresh.py --dry"
    assert eow._extract_command_tool_name(text) == "python3"


def test_extract_command_tool_name_falls_back_to_exec():
    assert eow._extract_command_tool_name("approval timed out") == "exec"


# ── OC wrapped session record format ────────────────────────────────────────


def test_classify_oc_session_extracts_async_failure_text():
    """Real OC shape: type=message wrapper with nested message.role/content,
    user-role text content starting with the async-failure template."""
    records = [
        {
            "type": "session", "id": "s-1", "timestamp": "2026-05-21T11:02:41Z",
        },
        {
            "type": "message", "id": "m-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "tc-1", "name": "ps_check",
                     "arguments": {"command": "ps aux"}},
                ],
            },
        },
        {
            "type": "message", "id": "m-2",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "[Thu 2026-05-21 04:02 PDT] An async command the "
                        "user already approved has completed.\n"
                        "Exec denied (gateway id=abc, approval-timeout "
                        "(allowlist-miss)): ps aux | grep pod_admin_user\n"
                        "Continue the task if needed."
                    )},
                ],
            },
        },
    ]
    outcomes = eow._classify_oc_session_messages("s-1", records)
    assert len(outcomes) == 1
    assert outcomes[0]["classification"] == "denied"
    # Command-extraction picks `ps` from the failure text, not the
    # assistant's tool_call name (because text-shape extraction wins
    # when "Exec" is in the failure surface).
    assert outcomes[0]["tool_name"] == "ps"
    assert outcomes[0]["session_id"] == "s-1"
    assert outcomes[0]["turn_id"] == "m-2"


def test_classify_oc_session_handles_anthropic_flat_shape():
    """Test fixtures use the flat shape {role, content} without
    type=message wrapper. Both shapes must work through the unwrapper."""
    records = [
        {
            "id": "t-1", "role": "user",
            "content": [
                {"type": "text",
                 "text": "Exec denied: python3 broken.py"},
            ],
        },
    ]
    outcomes = eow._classify_oc_session_messages("flat-1", records)
    assert len(outcomes) == 1
    assert outcomes[0]["classification"] == "denied"
    assert outcomes[0]["tool_name"] == "python3"


def test_classify_oc_session_skips_non_message_records():
    """type=session, type=model_change, etc. shouldn't emit outcomes."""
    records = [
        {"type": "session", "id": "s-1"},
        {"type": "model_change", "modelId": "claude-haiku-4-5"},
        {"type": "thinking_level_change"},
    ]
    assert eow._classify_oc_session_messages("s-1", records) == []


def test_unwrap_session_record_both_shapes():
    """Direct unwrapper test — wrapped + flat both yield the same triple."""
    wrapped = {
        "type": "message", "id": "w-1",
        "message": {"role": "user", "content": [{"type": "text"}]},
    }
    flat = {"id": "f-1", "role": "user", "content": [{"type": "text"}]}
    assert eow._unwrap_session_record(wrapped) == ("w-1", "user", [{"type": "text"}])
    assert eow._unwrap_session_record(flat) == ("f-1", "user", [{"type": "text"}])
    # Non-message record returns empty role
    assert eow._unwrap_session_record({"type": "session"}) == ("", "", [])


# ── _classify_tool_result ───────────────────────────────────────────────────


@pytest.mark.parametrize("content,expected", [
    ("exec-policy: denied", "denied"),
    ("Command not allowed", "denied"),
    ("approval timed out after 30 minutes", "timeout"),
    ("Approval timed out — command did not run", "timeout"),
    ("approval expired before operator could respond", "timeout"),
    ("preflight blocked python invocation", "preflight"),
    ("complex syntax blocked: pipes not permitted", "preflight"),
    ("Tool ran successfully", None),
    ("File not found", None),
    ("", None),
])
def test_classify_tool_result(content, expected):
    assert eow._classify_tool_result(content) == expected


# ── content-inspection detectors (with injected loader) ─────────────────────


def _tool_call_record(
    *,
    turn_id: str = "t-1",
    tool_name: str = "Bash",
    tool_result: str = "ok",
) -> dict:
    """Build a turn record with one tool_use/tool_result pair."""
    return {
        "id": turn_id,
        "role": "assistant",
        "content": [
            {
                "type": "tool_use", "id": "tu-1",
                "name": tool_name, "input": {"command": "echo hi"},
            },
            {
                "type": "tool_result", "tool_use_id": "tu-1",
                "content": tool_result,
            },
        ],
    }


def test_iter_failed_tool_outcomes_classifies_each_call():
    annotations = [
        _annotation(session_id="s-deny", tool_error_count=2),
        _annotation(session_id="s-timeout", tool_error_count=1),
        _annotation(session_id="s-preflight", tool_error_count=1),
    ]

    def loader(bot_id, session_id):
        if session_id == "s-deny":
            return [
                _tool_call_record(
                    tool_name="Bash",
                    tool_result="exec-policy denied: command not in allowlist",
                ),
            ]
        if session_id == "s-timeout":
            return [
                _tool_call_record(
                    tool_name="Bash",
                    tool_result="approval timed out",
                ),
            ]
        if session_id == "s-preflight":
            return [
                _tool_call_record(
                    tool_name="Bash",
                    tool_result="preflight blocked: python not permitted",
                ),
            ]
        return []

    out = eow._iter_failed_tool_outcomes(loader, "security_bot", annotations)
    classifications = sorted(o["classification"] for o in out)
    assert classifications == ["denied", "preflight", "timeout"]


def test_iter_failed_tool_outcomes_returns_empty_without_loader():
    annotations = [_annotation(session_id="s-1", tool_error_count=1)]
    assert eow._iter_failed_tool_outcomes(None, "security_bot", annotations) == []


def test_iter_failed_tool_outcomes_skips_clean_sessions():
    """Sessions with zero tool_error_count shouldn't even hit the loader."""
    annotations = [_annotation(session_id="s-1", tool_error_count=0)]

    def loader(bot_id, session_id):
        raise AssertionError("loader should not be called for clean sessions")

    assert eow._iter_failed_tool_outcomes(loader, "security_bot", annotations) == []


# ── detect_exec_denied ──────────────────────────────────────────────────────


def _outcome(
    *,
    classification: str = "denied",
    tool_name: str = "Bash",
    session_id: str = "s-1",
    result: str = "denied content",
) -> dict:
    return {
        "session_id": session_id,
        "turn_id": "t-1",
        "tool_name": tool_name,
        "tool_input_preview": "echo hi",
        "tool_result_preview": result,
        "classification": classification,
    }


def test_exec_denied_fires_above_count():
    outcomes = [
        _outcome(tool_name="Bash") for _ in range(3)
    ]
    out = eow.detect_exec_denied(
        "team_bot_a", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert len(out) == 1
    assert out[0]["details"]["tool_name"] == "Bash"
    assert out[0]["details"]["denial_count"] == 3


def test_exec_denied_groups_by_tool_name():
    outcomes = [
        _outcome(tool_name="Bash"),
        _outcome(tool_name="Bash"),
        _outcome(tool_name="WebFetch"),
    ]
    out = eow.detect_exec_denied(
        "team_bot_a", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    tools = sorted(d["details"]["tool_name"] for d in out)
    assert tools == ["Bash", "WebFetch"]


def test_exec_denied_quiet_when_no_denials():
    outcomes = [_outcome(classification="timeout")]
    out = eow.detect_exec_denied(
        "team_bot_a", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert out == []


def test_exec_denied_signature_per_bot_tool_pair():
    outcomes = [_outcome(tool_name="Bash")]
    out_team_bot_a = eow.detect_exec_denied(
        "team_bot_a", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    out_team_bot_c = eow.detect_exec_denied(
        "team_bot_c", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert out_team_bot_a[0]["signature"] != out_team_bot_c[0]["signature"]


# ── detect_approval_timeout ─────────────────────────────────────────────────


def test_approval_timeout_fires_on_first_occurrence():
    outcomes = [_outcome(classification="timeout", tool_name="Bash")]
    out = eow.detect_approval_timeout(
        "security_bot", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert len(out) == 1
    assert out[0]["details"]["timeout_count"] == 1
    assert out[0]["details"]["distinct_tools"] == ["Bash"]


def test_approval_timeout_emits_per_tool_counts():
    """Phase 4: details carries per_tool_counts + max_single_tool_count
    so the investigator can split recurring-tool vs scattered cases."""
    outcomes = [
        _outcome(classification="timeout", tool_name="python3") for _ in range(3)
    ] + [
        _outcome(classification="timeout", tool_name="ps"),
    ]
    out = eow.detect_approval_timeout(
        "security_bot", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert len(out) == 1
    details = out[0]["details"]
    assert details["per_tool_counts"] == {"python3": 3, "ps": 1}
    assert details["max_single_tool_count"] == 3
    assert details["top_tool"] == "python3"


def test_approval_timeout_top_tool_picks_highest_count():
    """When multiple tools tie or one dominates, top_tool is the highest-count."""
    outcomes = [
        _outcome(classification="timeout", tool_name="ps"),
        _outcome(classification="timeout", tool_name="curl"),
        _outcome(classification="timeout", tool_name="curl"),
        _outcome(classification="timeout", tool_name="curl"),
    ]
    out = eow.detect_approval_timeout(
        "security_bot", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert out[0]["details"]["top_tool"] == "curl"
    assert out[0]["details"]["max_single_tool_count"] == 3


def test_approval_timeout_aggregates_at_bot_scope():
    """Multiple timeouts on different tools → one Signal at bot scope."""
    outcomes = [
        _outcome(classification="timeout", tool_name="Bash"),
        _outcome(classification="timeout", tool_name="WebFetch"),
        _outcome(classification="timeout", tool_name="Read"),
    ]
    out = eow.detect_approval_timeout(
        "security_bot", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert len(out) == 1
    assert out[0]["details"]["timeout_count"] == 3
    assert sorted(out[0]["details"]["distinct_tools"]) == [
        "Bash", "Read", "WebFetch"
    ]


def test_approval_timeout_alert_severity_at_3x():
    outcomes = [
        _outcome(classification="timeout", tool_name="Bash") for _ in range(3)
    ]
    out = eow.detect_approval_timeout(
        "security_bot", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert out[0]["severity"] == "alert"


# ── detect_preflight_block ──────────────────────────────────────────────────


def test_preflight_block_fires():
    outcomes = [
        _outcome(classification="preflight", tool_name="Bash",
                 result="preflight blocked python"),
    ]
    out = eow.detect_preflight_block(
        "personal_bot", outcomes, window_days=7, min_count=1, max_per_run=5,
    )
    assert len(out) == 1
    assert out[0]["details"]["block_count"] == 1


def test_preflight_block_quiet_below_min():
    out = eow.detect_preflight_block(
        "personal_bot", [], window_days=7, min_count=2, max_per_run=5,
    )
    assert out == []


# ── collect_for_bot end-to-end ──────────────────────────────────────────────


def test_collect_for_bot_burst_only_without_loader(tmp_path):
    ann_dir = tmp_path / "annotations" / "team_bot_a"
    ann_dir.mkdir(parents=True)
    import json
    content = "\n".join([
        json.dumps(_annotation(
            session_id=f"s-{i}", tool_error_count=3, ts="2026-05-28T10:00:00Z",
        )) for i in range(3)
    ])
    (ann_dir / "2026-05-28.jsonl").write_text(content)

    detections = eow.collect_for_bot(
        "team_bot_a", tmp_path, config={}, today=date(2026, 5, 28),
    )
    types = sorted(d["type"] for d in detections)
    # No session_loader => content-inspection detectors silent;
    # tool_error_burst still fires from annotation-only data.
    assert types == ["tool_error_burst"]


def test_collect_for_bot_full_team_bot_a_shape(tmp_path):
    """Team_bot_a's protein-tracker shape: tool_error_burst + exec_denied
    cooperate when the loader is wired."""
    ann_dir = tmp_path / "annotations" / "team_bot_a"
    ann_dir.mkdir(parents=True)
    import json
    content = "\n".join([
        json.dumps(_annotation(
            session_id=f"s-{i}", tool_error_count=2, ts="2026-05-28T10:00:00Z",
        )) for i in range(3)
    ])
    (ann_dir / "2026-05-28.jsonl").write_text(content)

    def loader(bot_id, session_id):
        return [_tool_call_record(
            tool_name="Bash",
            tool_result="exec-policy denied: command not allowed",
        )]

    detections = eow.collect_for_bot(
        "team_bot_a", tmp_path, config={}, today=date(2026, 5, 28),
        session_loader=loader,
    )
    types = sorted(d["type"] for d in detections)
    assert "tool_error_burst" in types
    assert "exec_denied" in types
