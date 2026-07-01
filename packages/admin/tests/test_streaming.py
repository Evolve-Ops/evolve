"""tests/test_streaming.py — SSE wire format + jsonl tailer + Accept
negotiation on /api/home/chat.

Covers :mod:`evolve_admin.evo.streaming` (the tailer + iter_sse_events
generator) and the SSE branch of :func:`api_home_chat` in
:mod:`evolve_admin.web.home_chat_routes`.

The buffered branch already has full coverage in ``test_home_chat``;
these tests focus on the additive streaming surface — no regression
risk to the buffered path.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

import pytest  # noqa: E402

from evolve_admin.evo import streaming as _stream  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Wire format
# ─────────────────────────────────────────────────────────────────────


def test_stream_event_encodes_to_sse_wire_format():
    ev = _stream.StreamEvent("tool_call", {"id": "abc", "tool": "x"})
    wire = ev.encode().decode("utf-8")
    assert wire.startswith("event: tool_call\n")
    assert "data: " in wire
    assert wire.endswith("\n\n")
    # Round-trip through JSON to confirm the payload is intact.
    data_line = [l for l in wire.split("\n") if l.startswith("data:")][0]
    payload = json.loads(data_line[len("data:"):].strip())
    assert payload == {"id": "abc", "tool": "x"}


# ─────────────────────────────────────────────────────────────────────
# JSONL line parsing
# ─────────────────────────────────────────────────────────────────────


def _msg_assistant_tool_use(*, tool_id: str, tool_name: str,
                            ts_ms: int = 1717000000000) -> str:
    return json.dumps({
        "type": "message", "runId": "r1", "isError": False,
        "message": {
            "role": "assistant", "timestamp": ts_ms,
            "content": [{
                "type": "tool_use",
                "id": tool_id, "name": tool_name,
            }],
        },
    })


def _msg_tool_result(*, tool_id: str, text: str,
                     is_error: bool = False,
                     ts_ms: int = 1717000001000) -> str:
    return json.dumps({
        "type": "message", "runId": "r1", "isError": is_error,
        "message": {
            "role": "toolResult", "timestamp": ts_ms,
            "toolCallId": tool_id,
            "content": [{"type": "text", "text": text}],
        },
    })


def _msg_assistant_text(*, text: str, ts_ms: int = 1717000002000) -> str:
    return json.dumps({
        "type": "message", "runId": "r1", "isError": False,
        "message": {
            "role": "assistant", "timestamp": ts_ms,
            "content": [{"type": "text", "text": text}],
        },
    })


def test_parse_jsonl_line_emits_tool_call_event():
    line = _msg_assistant_tool_use(
        tool_id="t1", tool_name="pod_state__audit",
    )
    events = _stream.parse_jsonl_line(line)
    assert len(events) == 1
    ev = events[0]
    assert ev.event == "tool_call"
    # Namespace stripped — frontend renders the bare tool name.
    assert ev.data["tool"] == "audit"
    assert ev.data["id"] == "t1"
    assert ev.data["started_ms"] == 1717000000000


def test_parse_jsonl_line_emits_tool_result_event():
    line = _msg_tool_result(tool_id="t1", text="returned 3 bots")
    events = _stream.parse_jsonl_line(line)
    assert len(events) == 1
    ev = events[0]
    assert ev.event == "tool_result"
    assert ev.data["id"] == "t1"
    assert ev.data["outcome"] == "ok"
    assert "3 bots" in ev.data["summary"]


def test_parse_jsonl_line_tool_result_error():
    line = _msg_tool_result(tool_id="t1", text="boom", is_error=True)
    events = _stream.parse_jsonl_line(line)
    assert events[0].data["outcome"] == "error"


def test_parse_jsonl_line_emits_assistant_text():
    line = _msg_assistant_text(text="Looking into this now…")
    events = _stream.parse_jsonl_line(line)
    assert len(events) == 1
    assert events[0].event == "assistant_text"
    assert events[0].data["text"] == "Looking into this now…"


def test_parse_jsonl_line_assistant_multiblock_emits_multiple_events():
    """A single assistant message can contain text + tool_use in one
    content array — both should become events."""
    line = json.dumps({
        "type": "message", "runId": "r1", "isError": False,
        "message": {
            "role": "assistant", "timestamp": 1717000000000,
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "t1", "name": "ns__audit"},
            ],
        },
    })
    events = _stream.parse_jsonl_line(line)
    assert [e.event for e in events] == ["tool_call", "assistant_text"] or \
           [e.event for e in events] == ["assistant_text", "tool_call"]


def test_parse_jsonl_line_skips_non_message_types():
    line = json.dumps({"type": "init", "runId": "r1"})
    assert _stream.parse_jsonl_line(line) == []


def test_parse_jsonl_line_handles_garbage():
    assert _stream.parse_jsonl_line("") == []
    assert _stream.parse_jsonl_line("not json") == []
    assert _stream.parse_jsonl_line('{"type":"message"}') == []  # no message key


# ─────────────────────────────────────────────────────────────────────
# Tailer thread — reads from offset, emits events incrementally
# ─────────────────────────────────────────────────────────────────────


def test_tailer_skips_lines_before_start_offset(tmp_path):
    """Prior session history must NOT replay when a new turn starts.

    The tailer captures the file size before the OC subprocess fires
    and only streams lines appended after.
    """
    jsonl = tmp_path / "session.jsonl"
    # Pre-existing history from earlier turns.
    pre = _msg_assistant_tool_use(tool_id="OLD", tool_name="ns__old") + "\n"
    jsonl.write_text(pre)
    start_offset = jsonl.stat().st_size

    seen: list[_stream.StreamEvent] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_stream.tail_session_jsonl,
        kwargs={
            "jsonl_path": jsonl, "start_offset": start_offset,
            "stop_event": stop, "on_event": seen.append,
            "poll_interval_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    # Append a new entry — the current turn's tool call.
    with jsonl.open("a") as fh:
        fh.write(_msg_assistant_tool_use(
            tool_id="NEW", tool_name="ns__new",
        ) + "\n")
    # Give the tailer up to 1s to pick it up.
    deadline = time.time() + 1.0
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=1.0)
    # Should have seen only the NEW event, not the OLD one.
    assert len(seen) == 1
    assert seen[0].data["id"] == "NEW"


def test_tailer_picks_up_multiple_appended_lines(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("")
    start_offset = 0

    seen: list[_stream.StreamEvent] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_stream.tail_session_jsonl,
        kwargs={
            "jsonl_path": jsonl, "start_offset": start_offset,
            "stop_event": stop, "on_event": seen.append,
            "poll_interval_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    with jsonl.open("a") as fh:
        fh.write(_msg_assistant_tool_use(tool_id="t1", tool_name="ns__a") + "\n")
        fh.flush()
    time.sleep(0.1)
    with jsonl.open("a") as fh:
        fh.write(_msg_tool_result(tool_id="t1", text="ok") + "\n")
        fh.flush()
    deadline = time.time() + 1.0
    while time.time() < deadline and len(seen) < 2:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=1.0)
    assert [e.event for e in seen] == ["tool_call", "tool_result"]


def test_tailer_handles_partial_line_writes(tmp_path):
    """A jsonl write that lands mid-line must NOT be parsed until the
    trailing newline arrives — otherwise we'd get JSONDecodeError
    spam and lose data."""
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("")
    seen: list[_stream.StreamEvent] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_stream.tail_session_jsonl,
        kwargs={
            "jsonl_path": jsonl, "start_offset": 0,
            "stop_event": stop, "on_event": seen.append,
            "poll_interval_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    payload = _msg_assistant_tool_use(tool_id="t1", tool_name="ns__a")
    # First write — no trailing \n. Tailer must wait.
    half = len(payload) // 2
    with jsonl.open("a") as fh:
        fh.write(payload[:half]); fh.flush()
    time.sleep(0.1)
    assert seen == []
    # Second write — completes the line.
    with jsonl.open("a") as fh:
        fh.write(payload[half:] + "\n"); fh.flush()
    deadline = time.time() + 1.0
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=1.0)
    assert len(seen) == 1
    assert seen[0].data["id"] == "t1"


def test_tailer_survives_missing_file(tmp_path):
    """When OC hasn't created the session jsonl yet, the tailer
    must not crash — it should poll until the file appears."""
    jsonl = tmp_path / "not-yet.jsonl"
    seen: list[_stream.StreamEvent] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_stream.tail_session_jsonl,
        kwargs={
            "jsonl_path": jsonl, "start_offset": 0,
            "stop_event": stop, "on_event": seen.append,
            "poll_interval_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    # Now create it.
    jsonl.write_text(
        _msg_assistant_tool_use(tool_id="t1", tool_name="ns__a") + "\n"
    )
    deadline = time.time() + 1.0
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=1.0)
    assert len(seen) == 1


# ─────────────────────────────────────────────────────────────────────
# iter_sse_events — the end-to-end generator
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakeResult:
    text: str = "done text"
    session_id: str = "s1"
    model: str | None = "claude-sonnet"
    usage: dict | None = None
    error: str | None = None
    run_id: str | None = "r1"


def _decode_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    """Helper — turn the raw SSE wire bytes back into parsed events
    for assertion-friendly inspection."""
    out: list[tuple[str, dict]] = []
    for chunk in chunks:
        text = chunk.decode("utf-8")
        event = "message"
        data = ""
        for line in text.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        if data:
            out.append((event, json.loads(data)))
    return out


def test_iter_sse_events_emits_meta_then_done_for_silent_turn(tmp_path):
    """No jsonl activity → just meta + done."""
    jsonl = tmp_path / "silent.jsonl"

    def _run():
        return _FakeResult(text="hello")

    chunks = list(_stream.iter_sse_events(
        session_id="s1", jsonl_path=jsonl, run_subprocess=_run,
        heartbeat_s=999,  # disable heartbeats for this test
    ))
    events = _decode_events(chunks)
    types = [e[0] for e in events]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert events[-1][1]["text"] == "hello"
    assert events[-1][1]["session_id"] == "s1"


def test_iter_sse_events_interleaves_tool_calls_with_subprocess(tmp_path):
    """Subprocess writes to jsonl mid-run — tailer surfaces tool_call
    + tool_result before the terminal done event."""
    jsonl = tmp_path / "live.jsonl"

    def _run():
        # Simulate OC writing to its session jsonl during the turn.
        time.sleep(0.05)
        with jsonl.open("a") as fh:
            fh.write(_msg_assistant_tool_use(
                tool_id="t1", tool_name="ns__a",
            ) + "\n")
            fh.flush()
        time.sleep(0.05)
        with jsonl.open("a") as fh:
            fh.write(_msg_tool_result(tool_id="t1", text="ok ok") + "\n")
            fh.flush()
        time.sleep(0.05)
        return _FakeResult(text="final reply")

    chunks = list(_stream.iter_sse_events(
        session_id="s1", jsonl_path=jsonl, run_subprocess=_run,
        heartbeat_s=999, poll_interval_s=0.01,
    ))
    events = _decode_events(chunks)
    types = [e[0] for e in events]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "tool_call" in types
    assert "tool_result" in types
    # done payload includes the final reply text.
    assert events[-1][1]["text"] == "final reply"


def test_iter_sse_events_emits_heartbeat_on_idle(tmp_path):
    """Quiet subprocess → at least one heartbeat fires past the
    threshold."""
    jsonl = tmp_path / "quiet.jsonl"

    def _run():
        # Sleep long enough to trip the heartbeat.
        time.sleep(0.35)
        return _FakeResult(text="ok")

    chunks = list(_stream.iter_sse_events(
        session_id="s1", jsonl_path=jsonl, run_subprocess=_run,
        heartbeat_s=0.15, poll_interval_s=0.01,
    ))
    events = _decode_events(chunks)
    heartbeats = [e for e in events if e[0] == "heartbeat"]
    assert len(heartbeats) >= 1
    # The heartbeat carries the elapsed-since-start seconds.
    assert "elapsed_s" in heartbeats[0][1]


def test_iter_sse_events_done_serializer_override(tmp_path):
    """The route handler can shape the done payload via a serializer
    callback so the streaming done event matches the buffered JSON
    response 1:1."""
    jsonl = tmp_path / "ser.jsonl"

    def _run():
        return _FakeResult(text="from result")

    def _serializer(result, err, sid):
        return {"shaped": True, "text": result.text, "session_id": sid,
                "tier": "auto", "source": "evo"}

    chunks = list(_stream.iter_sse_events(
        session_id="s1", jsonl_path=jsonl, run_subprocess=_run,
        heartbeat_s=999, done_serializer=_serializer,
    ))
    events = _decode_events(chunks)
    done_payload = events[-1][1]
    assert done_payload["shaped"] is True
    assert done_payload["source"] == "evo"


def test_iter_sse_events_no_duplicate_emit_across_drain(tmp_path):
    """Regression: the post-subprocess drain pass must NOT re-emit
    events the tailer already streamed.

    First implementation drained from start_offset, which double-
    emitted every tool call. Fix threads the tailer's current offset
    through and the drain picks up only the trailing bytes.
    """
    jsonl = tmp_path / "dedup.jsonl"
    jsonl.write_text("")

    def _run():
        # Tool fan-out during the subprocess (gets streamed by tailer).
        time.sleep(0.05)
        with jsonl.open("a") as fh:
            fh.write(_msg_assistant_tool_use(
                tool_id="t1", tool_name="ns__a") + "\n")
            fh.flush()
        time.sleep(0.05)
        with jsonl.open("a") as fh:
            fh.write(_msg_tool_result(
                tool_id="t1", text="ok") + "\n")
            fh.flush()
        time.sleep(0.05)
        return _FakeResult(text="reply")

    chunks = list(_stream.iter_sse_events(
        session_id="s1", jsonl_path=jsonl, run_subprocess=_run,
        heartbeat_s=999, poll_interval_s=0.01,
    ))
    events = _decode_events(chunks)
    # Group by event type. Each tool_call and tool_result must appear
    # exactly once.
    tool_call_ids = [e[1]["id"] for e in events if e[0] == "tool_call"]
    tool_result_ids = [e[1]["id"] for e in events if e[0] == "tool_result"]
    assert tool_call_ids == ["t1"], (
        f"tool_call duplicated: {tool_call_ids}"
    )
    assert tool_result_ids == ["t1"], (
        f"tool_result duplicated: {tool_result_ids}"
    )


def test_iter_sse_events_always_emits_done_even_on_worker_error(tmp_path):
    """Subprocess worker exception → done event still emitted with
    error info, so the client's end-of-stream handler always runs."""
    jsonl = tmp_path / "err.jsonl"

    def _run():
        raise RuntimeError("boom")

    chunks = list(_stream.iter_sse_events(
        session_id="s1", jsonl_path=jsonl, run_subprocess=_run,
        heartbeat_s=999,
    ))
    events = _decode_events(chunks)
    assert events[-1][0] == "done"
    payload = events[-1][1]
    assert payload["error"]
    assert "boom" in payload["error"] or payload["error"] == "subprocess_worker_failed"


# ─────────────────────────────────────────────────────────────────────
# Accept-header negotiation on /api/home/chat
# ─────────────────────────────────────────────────────────────────────


class _StubReq:
    def __init__(self, accept: str = "", args: dict | None = None):
        self.headers = {"Accept": accept} if accept else {}
        self.args = args or {}


def test_wants_sse_recognizes_event_stream_accept():
    from evolve_admin.web import home_chat_routes as _hcr
    assert _hcr._wants_sse(_StubReq(accept="text/event-stream"))


def test_wants_sse_recognizes_multi_accept_with_event_stream():
    from evolve_admin.web import home_chat_routes as _hcr
    assert _hcr._wants_sse(_StubReq(accept="text/event-stream, */*"))
    assert _hcr._wants_sse(
        _StubReq(accept="application/json, text/event-stream;q=0.9"),
    )


def test_wants_sse_default_browser_accept_does_not_stream():
    """A bare ``*/*`` from a non-streaming-aware caller (the default
    fetch/api() path) must NOT trigger streaming. Streaming is
    opt-in."""
    from evolve_admin.web import home_chat_routes as _hcr
    assert not _hcr._wants_sse(_StubReq(accept="*/*"))
    assert not _hcr._wants_sse(_StubReq(accept="application/json"))
    assert not _hcr._wants_sse(_StubReq())


def test_wants_sse_query_param_opt_in():
    """``?stream=1`` flips streaming on without an Accept header —
    handy for curl smoke tests."""
    from evolve_admin.web import home_chat_routes as _hcr
    assert _hcr._wants_sse(_StubReq(args={"stream": "1"}))
    # Other values don't.
    assert not _hcr._wants_sse(_StubReq(args={"stream": "0"}))
    assert not _hcr._wants_sse(_StubReq(args={"stream": "yes"}))
