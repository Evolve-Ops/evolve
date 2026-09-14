"""evolve_admin.evo.streaming — SSE wire format + jsonl tailer for
/api/home/chat.

The buffered ``send_to_evo`` path waits for OC's ``agent`` subprocess to
finish before returning anything; on a heavy-context turn that fans out
into multiple tool calls, the chat panel can sit silent for up to 270s
(the proxy timeout) before either succeeding or failing. The user
reads that as broken.

This module adds an incremental wire format on top of the same
subprocess invocation: while the OC child runs, a tailer thread
watches the session jsonl OC writes synchronously during the turn and
forwards each new entry as a Server-Sent Event. Final result (the
parsed ``ProxyResult``) lands as a terminal ``done`` event when the
subprocess completes.

Design choices:

* **No change to OC** — we read the jsonl OC already writes. The
  tailer is purely additive; failure to read the jsonl just means
  fewer interim events, not a failed turn (subprocess.run is the
  source of truth).

* **No assistant-text streaming.** OC's ``agent --json`` returns the
  reply on stdout as one buffered blob, not chunks. Streaming that
  would require swapping subprocess.run for Popen + an incremental
  stdout reader and changes to OC's output format. Out of scope.

* **Heartbeats** every 10s so the proxy sees continuous traffic and
  the 270s hard wall becomes a per-chunk keepalive instead. (The
  subprocess timeout itself stays 270s — the wall isn't *removed*,
  but a slow turn that's making progress now has each chunk reset
  the visible-progress clock from the user's perspective.)

* **Accept-header negotiation** at the route — see
  :func:`evolve_admin.web.home_chat_routes.api_home_chat`.

Event types yielded by :func:`iter_sse_events`:

  ``meta`` — once at start; the resolved session_id and the offset
  the tailer started from. Lets the client confirm it's wired up.

  ``tool_call`` — a new ``tool_use`` entry showed up in the jsonl.
  Carries the OC tool id, the namespace-stripped tool name, and the
  start timestamp.

  ``tool_result`` — the matching ``toolResult`` arrived. Carries the
  tool id, ok/error outcome, and a short summary text.

  ``assistant_text`` — an interim ``role: "assistant"`` message with
  text content (e.g. between tool fans). The final reply does NOT
  come through this event; it lands in ``done``.

  ``heartbeat`` — emitted every ``_HEARTBEAT_S`` seconds when the
  tailer hasn't seen new content. Carries the elapsed turn time so
  the client can render "evo is working… (12s)".

  ``done`` — terminal event. Carries the parsed ProxyResult fields
  (text, session_id, model, usage, error). After this the server
  closes the stream.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator

log = logging.getLogger(__name__)


# How often the tailer polls for new bytes. Tight enough that the user
# sees tool activity within a fraction of a second; loose enough that
# a slow turn doesn't burn a core spinning on stat() calls.
_TAIL_POLL_S = 0.1

# How long between heartbeats when no jsonl activity has fired. The
# operator's tolerance for a silent chat is ~5-10s; aim for the lower
# end so even a tool-less Sonnet turn produces visible progress.
_HEARTBEAT_S = 10.0


@dataclass(frozen=True)
class StreamEvent:
    """One SSE event. ``event`` is the SSE event-type string;
    ``data`` is the payload that gets json-encoded.
    """
    event: str
    data: dict[str, Any]

    def encode(self) -> bytes:
        """Render as the SSE wire format.

        ``event: <type>\\ndata: <json>\\n\\n`` per the SSE spec.
        Always UTF-8.
        """
        blob = json.dumps(self.data, separators=(",", ":"))
        return f"event: {self.event}\ndata: {blob}\n\n".encode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# JSONL tailer
# ─────────────────────────────────────────────────────────────────────


def _strip_oc_namespace_local(name: str) -> str:
    """Mirror proxy._strip_oc_namespace without importing it to avoid
    a circular dependency (proxy will eventually call into us).

    OC tool names are namespaced like ``namespace__name``; the chat
    panel wants the bare name for display."""
    if not name:
        return name
    parts = name.split("__", 1)
    return parts[1] if len(parts) == 2 else name


def _summarize_result_text_local(text: str) -> str:
    """Mirror proxy._summarize_result_text. Truncate to ~140 chars
    with a soft cut at a word boundary if possible."""
    if not text:
        return ""
    text = " ".join(text.split())  # collapse whitespace
    if len(text) <= 140:
        return text
    cut = text[:140]
    # Soft cut at last space if there is one in the back half — keeps
    # the truncation readable.
    last_space = cut.rfind(" ")
    if last_space > 70:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def parse_jsonl_line(line: str) -> list[StreamEvent]:
    """Parse one OC session-jsonl line into 0..N stream events.

    A single OC message can contain multiple ``content`` blocks
    (e.g. an assistant turn with text + tool_use + tool_use), so this
    returns a list. Empty/invalid/non-message lines return ``[]``.
    """
    line = line.strip()
    if not line:
        return []
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return []
    if rec.get("type") != "message":
        return []
    msg = rec.get("message") or {}
    role = msg.get("role")
    timestamp_ms = msg.get("timestamp") or 0
    content = msg.get("content") or []
    out: list[StreamEvent] = []
    if role == "assistant":
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                tool_name = c.get("name") or "?"
                out.append(StreamEvent("tool_call", {
                    "id": c.get("id") or "",
                    "tool": _strip_oc_namespace_local(tool_name),
                    "started_ms": int(timestamp_ms),
                }))
            elif c.get("type") == "text":
                text = (c.get("text") or "").strip()
                if text:
                    # Interim assistant text (e.g. between tool fans).
                    # The FINAL reply does NOT come through here — OC
                    # surfaces it on stdout, parsed in `done`.
                    out.append(StreamEvent("assistant_text", {
                        "text": text,
                        "timestamp_ms": int(timestamp_ms),
                    }))
    elif role == "toolResult":
        tool_call_id = msg.get("toolCallId") or ""
        is_error = bool(rec.get("isError")) or msg.get("status") == "error"
        # Pull a short summary out of the result text.
        summary_text = ""
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "text":
                summary_text = c.get("text", "")
                break
        out.append(StreamEvent("tool_result", {
            "id": tool_call_id,
            "outcome": "error" if is_error else "ok",
            "summary": _summarize_result_text_local(summary_text),
            "finished_ms": int(timestamp_ms),
        }))
    return out


def tail_session_jsonl(
    jsonl_path: Path,
    *,
    start_offset: int,
    stop_event: threading.Event,
    on_event: Callable[[StreamEvent], None],
    poll_interval_s: float = _TAIL_POLL_S,
    offset_holder: list[int] | None = None,
) -> None:
    """Watch ``jsonl_path`` from ``start_offset`` and call ``on_event``
    for every new fully-formed jsonl line.

    Designed to run inside a thread. Returns when ``stop_event`` is
    set (caller signals after the OC subprocess exits). Failures to
    read the file (e.g. file doesn't exist yet on a brand-new
    session) are silently retried; the tailer is best-effort.

    ``offset_holder`` (optional one-element list) is updated in place
    on every poll so the caller knows where the tailer stopped — used
    by :func:`drain_remaining` to pick up the trailing writes that
    landed between the last poll and ``stop_event.set()`` without
    re-emitting events the tailer already streamed.

    The caller is responsible for one final drain after the
    subprocess exits — OC may flush its last writes between the loop
    iteration and the stop_event.set() call. See
    :func:`drain_remaining` for the helper.
    """
    offset = start_offset
    buf = ""
    if offset_holder is not None:
        offset_holder[0] = offset
    while not stop_event.is_set():
        try:
            if not jsonl_path.exists():
                time.sleep(poll_interval_s)
                continue
            with jsonl_path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
                if offset_holder is not None:
                    offset_holder[0] = offset
        except OSError as exc:
            # Permissions, deleted between exists() and open(), etc.
            # All best-effort — sleep and retry.
            log.debug("tailer read failed (%s); retrying", exc)
            time.sleep(poll_interval_s)
            continue
        if not chunk:
            time.sleep(poll_interval_s)
            continue
        try:
            buf += chunk.decode("utf-8")
        except UnicodeDecodeError:
            # OC writes UTF-8; a decode error here is almost certainly
            # a partial multi-byte sequence at the chunk boundary.
            # Roll the offset back one byte so we re-read on next pass.
            # Conservative: drop the offending byte and continue.
            buf += chunk.decode("utf-8", errors="replace")
        # Process complete lines (\n-terminated). Anything after the
        # last \n stays buffered for the next pass.
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            for event in parse_jsonl_line(line):
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 — caller's queue must not break tailer
                    log.exception("on_event raised; dropping event")


def drain_remaining(
    jsonl_path: Path, *, start_offset: int,
    on_event: Callable[[StreamEvent], None],
) -> int:
    """Read everything appended past ``start_offset`` once.

    Called after the OC subprocess exits to pick up any writes that
    landed between the tailer's last poll and the exit. Returns the
    new offset (caller usually ignores).
    """
    if not jsonl_path.exists():
        return start_offset
    try:
        with jsonl_path.open("rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
            new_offset = fh.tell()
    except OSError:
        return start_offset
    if not data:
        return new_offset
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return new_offset
    for line in text.split("\n"):
        if not line.strip():
            continue
        for event in parse_jsonl_line(line):
            try:
                on_event(event)
            except Exception:  # noqa: BLE001
                log.exception("on_event raised during drain; dropping event")
    return new_offset


def session_jsonl_offset(jsonl_path: Path) -> int:
    """Return the current size of ``jsonl_path``, or 0 if it doesn't
    exist yet. Used as the tailer's start point so we only stream
    events from THIS turn, not all prior history on the same session.
    """
    try:
        return jsonl_path.stat().st_size
    except (OSError, FileNotFoundError):
        return 0


# ─────────────────────────────────────────────────────────────────────
# SSE event generator — composes a tailer thread with the subprocess
# ─────────────────────────────────────────────────────────────────────


def iter_sse_events(
    *,
    session_id: str,
    jsonl_path: Path,
    run_subprocess: Callable[[], Any],
    heartbeat_s: float = _HEARTBEAT_S,
    poll_interval_s: float = _TAIL_POLL_S,
    done_serializer: Callable[[Any, str | None, str], dict[str, Any]] | None = None,
) -> Generator[bytes, None, None]:
    """Yield SSE-encoded event bytes for one chat turn.

    The caller hands in:
      * ``jsonl_path`` — OC's session log we'll tail
      * ``run_subprocess`` — callable that invokes the OC subprocess
        and returns a ProxyResult-like object. We run it in a worker
        thread so this generator can also drive the heartbeat clock
        while it's blocking.

    Sequence emitted:
      1. ``meta`` event — session_id + tailer start offset
      2. interleaved ``tool_call`` / ``tool_result`` /
         ``assistant_text`` events from the tailer + ``heartbeat``
         events when idle past ``heartbeat_s``
      3. ``done`` event — the ProxyResult fields, after the subprocess
         returns and one final jsonl drain pass

    The function never raises — subprocess errors surface as ``done``
    with ``error`` set. The terminal ``done`` event is always emitted
    so the client can rely on it as the end-of-stream marker (the
    SSE protocol has no native EOF signal beyond connection close).
    """
    start_offset = session_jsonl_offset(jsonl_path)

    # Yield the opening meta event so the client knows the stream is
    # open and the server's view of the session matches.
    yield StreamEvent("meta", {
        "session_id": session_id,
        "start_offset": start_offset,
        "started_at_ms": int(time.time() * 1000),
    }).encode()

    event_q: queue.Queue[StreamEvent] = queue.Queue(maxsize=512)
    stop = threading.Event()
    subprocess_done = threading.Event()
    subprocess_result: dict[str, Any] = {}
    # One-element list — the tailer mutates [0] with its current read
    # offset on every poll. The drain pass after subprocess exit reads
    # from this position so it doesn't re-emit events the tailer
    # already streamed.
    tailer_offset = [start_offset]

    def _on_event(ev: StreamEvent) -> None:
        try:
            event_q.put_nowait(ev)
        except queue.Full:
            # Drop the event on overflow rather than block the tailer.
            # 512 entries is far more than a normal turn produces.
            log.warning("SSE event queue full; dropping %s", ev.event)

    tailer_thread = threading.Thread(
        target=tail_session_jsonl,
        kwargs={
            "jsonl_path": jsonl_path,
            "start_offset": start_offset,
            "stop_event": stop,
            "on_event": _on_event,
            "poll_interval_s": poll_interval_s,
            "offset_holder": tailer_offset,
        },
        name="sse-jsonl-tailer",
        daemon=True,
    )

    def _run_subprocess_wrapper() -> None:
        try:
            subprocess_result["result"] = run_subprocess()
        except Exception as exc:  # noqa: BLE001
            log.exception("subprocess worker raised")
            subprocess_result["error"] = str(exc)
        finally:
            subprocess_done.set()

    subprocess_thread = threading.Thread(
        target=_run_subprocess_wrapper,
        name="sse-oc-subprocess",
        daemon=True,
    )

    tailer_thread.start()
    subprocess_thread.start()

    turn_start = time.time()
    last_emit = turn_start
    try:
        while not subprocess_done.is_set():
            try:
                ev = event_q.get(timeout=0.5)
            except queue.Empty:
                # Heartbeat tick. Emit only if we haven't sent anything
                # in heartbeat_s — avoids redundant pings while the
                # tailer is actively producing.
                now = time.time()
                if now - last_emit >= heartbeat_s:
                    yield StreamEvent("heartbeat", {
                        "elapsed_s": round(now - turn_start, 1),
                    }).encode()
                    last_emit = now
                continue
            yield ev.encode()
            last_emit = time.time()

        # Subprocess returned. Stop the tailer, drain any final
        # jsonl writes that landed in the last poll interval, then
        # emit the terminal done event. The drain starts from the
        # tailer's current offset (not start_offset) so we don't
        # re-emit events the tailer already streamed.
        stop.set()
        tailer_thread.join(timeout=poll_interval_s * 3)
        drain_remaining(
            jsonl_path, start_offset=tailer_offset[0],
            on_event=lambda ev: event_q.put_nowait(ev) if not event_q.full() else None,
        )
        # Flush whatever's still in the queue from the tailer's last
        # iteration or the drain pass above.
        while not event_q.empty():
            try:
                ev = event_q.get_nowait()
            except queue.Empty:
                break
            yield ev.encode()

        # Final result. Even on error we still emit done — the client
        # always reads done as the end marker. The route handler can
        # override the payload shape via ``done_serializer`` so the
        # streaming ``done`` event matches the buffered JSON response
        # 1:1 (same tier_capped, model, etc. fields the existing
        # client expects).
        serializer = done_serializer or _serialize_proxy_result
        done_payload = serializer(
            subprocess_result.get("result"),
            subprocess_result.get("error"),
            session_id,
        )
        yield StreamEvent("done", done_payload).encode()
    finally:
        stop.set()


def _serialize_proxy_result(
    result: Any, error_text: str | None, session_id: str,
) -> dict[str, Any]:
    """Render a ProxyResult-like object into the ``done`` payload.

    Accepts either an actual ProxyResult dataclass instance or any
    object with the same attributes (or a dict) so tests can pass
    fixtures without importing the dataclass.
    """
    if result is None:
        return {
            "text": (
                "Subprocess worker failed to return a result: "
                + (error_text or "unknown error")
            ),
            "session_id": session_id,
            "error": error_text or "subprocess_worker_failed",
        }

    def _attr(name: str, default: Any = None) -> Any:
        if isinstance(result, dict):
            return result.get(name, default)
        return getattr(result, name, default)

    return {
        "text": _attr("text") or "",
        "session_id": _attr("session_id") or session_id,
        "model": _attr("model"),
        "usage": _attr("usage") or {},
        "error": _attr("error"),
        "inspector_event": _attr("inspector_event"),
    }
