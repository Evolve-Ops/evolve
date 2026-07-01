"""tests/test_spec_routes_streaming.py — SSE wiring + tier-driven model
selection for the Create App wizard.

The wizard endpoint switched from a single JSON response to an SSE stream
so the operator sees progress as the LLM generates the spec. These tests
pin the contract:

  * ``_resolve_spec_model`` returns the operator's configured tier2 by
    default and tier1 when ``power=True`` — never a hardcoded literal.
    Refuses non-Anthropic provider resolutions (the call site posts
    directly to the Anthropic Messages API).

  * ``_build_draft_events`` yields the canonical event sequence
    (phase[context] → phase[model] → delta+ → tokens → phase[parse] →
    draft) and the final draft has the SpecDraft shape callers depend
    on.

  * ``POST /api/specs`` (SSE) emits `event: phase`, `event: delta`,
    `event: tokens`, `event: done` frames; a 'done' event payload
    carries {session_id, status, draft}. The persisted SpecSession is
    discoverable via ``GET /api/specs/<id>``.

  * Sync ``_build_draft`` wrapper (used by evo's chat wizard) returns
    the same dict shape it always did — back-compat with the existing
    ``test_evo_app_create`` stub.
"""

from __future__ import annotations

import io
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


# ── Fake Anthropic SSE response ───────────────────────────────────────────────

def _make_fake_anthropic_stream(text_chunks: list[str], *, output_tokens: int = 0) -> bytes:
    """Build a byte stream shaped like Anthropic's SSE response. Each chunk
    becomes a content_block_delta; we cap the stream with a message_delta
    carrying usage and a final [DONE] sentinel."""
    lines: list[str] = []
    lines.append('data: {"type":"message_start","message":{"usage":{"input_tokens":42}}}')
    lines.append('')
    lines.append('data: {"type":"content_block_start","index":0}')
    lines.append('')
    for chunk in text_chunks:
        payload = json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": chunk},
        })
        lines.append(f"data: {payload}")
        lines.append('')
    lines.append('data: {"type":"content_block_stop","index":0}')
    lines.append('')
    msg_delta = json.dumps({"type": "message_delta", "usage": {"output_tokens": output_tokens}})
    lines.append(f"data: {msg_delta}")
    lines.append('')
    lines.append('data: [DONE]')
    lines.append('')
    return ("\n".join(lines) + "\n").encode("utf-8")


class _FakeUrlopen:
    """File-like that yields the fake SSE bytes line-by-line.

    Production code reads via ``resp.readline()`` (the explicit-readline
    loop is what makes ``socket.timeout`` recoverable as a keepalive).
    ``__iter__``/``__next__`` are retained for any future callers that
    iterate the response directly.
    """

    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def readline(self):
        return self._buf.readline()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        try:
            self._buf.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


@pytest.fixture
def stub_anthropic(monkeypatch):
    """Patch urlopen to return a fake SSE stream containing the given JSON."""
    from evolve_admin.web import spec_routes as sr

    def install(*, draft_json: dict, output_tokens: int = 137):
        text = json.dumps(draft_json)
        # Split into a few chunks so multi-delta is exercised.
        n = max(1, len(text) // 3)
        chunks = [text[i:i + n] for i in range(0, len(text), n)]
        payload = _make_fake_anthropic_stream(chunks, output_tokens=output_tokens)

        def fake_urlopen(req, timeout=None):
            return _FakeUrlopen(payload)

        monkeypatch.setattr(sr.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(sr, "_resolve_api_key", lambda: "test-key")
        monkeypatch.setattr(
            sr,
            "_resolve_spec_model",
            lambda power=False: (
                "tier1" if power else "tier2",
                "claude-opus-4-6" if power else "claude-sonnet-4-6",
                "anthropic/claude-opus-4-6" if power else "anthropic/claude-sonnet-4-6",
            ),
        )

    return install


# ── _resolve_spec_model ───────────────────────────────────────────────────────

def test_resolve_spec_model_defaults_to_configured_tier2(monkeypatch):
    """Default path returns whatever the analyzer resolves for tier2 —
    never a hardcoded literal. Operator's AI Optimization choice wins."""
    from evolve_admin.web import spec_routes as sr

    calls: list[tuple[str, dict]] = []

    def fake_resolve(tier, config, bot_id=None):
        calls.append((tier, config))
        return "anthropic/claude-sonnet-4-6-imagined"

    monkeypatch.setattr("models.resolve_tier", fake_resolve)
    monkeypatch.setattr("models.check_tier_policy", lambda *a, **k: None)
    monkeypatch.setattr("evolve_config.load_config", lambda: {"_test": True})

    tier, bare, full = sr._resolve_spec_model(power=False)
    assert tier == "tier2"
    assert bare == "claude-sonnet-4-6-imagined"
    assert full == "anthropic/claude-sonnet-4-6-imagined"
    assert calls == [("tier2", {"_test": True})]


def test_resolve_spec_model_power_uses_tier1(monkeypatch):
    """power=True → tier1, and check_tier_policy gets called for telemetry."""
    from evolve_admin.web import spec_routes as sr

    policy_calls: list[tuple] = []
    monkeypatch.setattr("models.resolve_tier", lambda tier, *a, **k: f"anthropic/{tier}-model")
    monkeypatch.setattr(
        "models.check_tier_policy",
        lambda tier, context, config=None, **k: policy_calls.append((tier, context)),
    )
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    tier, bare, full = sr._resolve_spec_model(power=True)
    assert tier == "tier1"
    assert bare == "tier1-model"
    assert full == "anthropic/tier1-model"
    assert policy_calls and policy_calls[0][0] == "tier1"


def test_resolve_spec_model_rejects_non_anthropic(monkeypatch):
    """Spec generation posts directly to api.anthropic.com — if the
    operator's tier resolves to OpenAI/Google/etc, fail loudly with a
    fix-it message rather than producing a meaningless HTTP error."""
    from evolve_admin.web import spec_routes as sr

    monkeypatch.setattr("models.resolve_tier", lambda *a, **k: "openai/gpt-4o")
    monkeypatch.setattr("models.check_tier_policy", lambda *a, **k: None)
    monkeypatch.setattr("evolve_config.load_config", lambda: {})

    with pytest.raises(RuntimeError, match="Anthropic-provider model"):
        sr._resolve_spec_model(power=False)


# ── _build_draft_events ───────────────────────────────────────────────────────

_VALID_DRAFT_JSON = {
    "display_name": "Test App",
    "description": "Does a test thing.",
    "build_spec": "# Test\nA build spec body.",
    "application_tags": ["productivity"],
    "requirements": {"integrations": [], "python_packages": []},
    "app_dependencies": [],
    "test_command": "pytest -q",
    "test_exemption_reason": "",
    "usage": {},
    "conflicts": [],
    "suggestions": [],
}


def test_build_draft_events_yields_canonical_sequence(tmp_path, stub_anthropic):
    """The event stream the SSE route forwards must follow:
    phase[context] → phase[model] → delta+ → tokens → phase[parse] → draft.
    The final draft carries a populated SpecDraft-shaped dict."""
    from evolve_admin.web import spec_routes as sr

    stub_anthropic(draft_json=_VALID_DRAFT_JSON, output_tokens=421)

    events = list(sr._build_draft_events(
        description="A small app for testing.",
        target_bots=["bot_a"],
        shared_dir=tmp_path,
        version=1,
        power=False,
    ))

    types = [e["type"] for e in events]
    phases = [e["phase"] for e in events if e["type"] == "phase"]
    assert phases == ["context", "model", "parse"]
    assert "delta" in types and "tokens" in types
    assert types.index("phase") < types.index("delta")     # context phase before deltas
    assert types[-1] == "draft"

    model_event = next(e for e in events if e["type"] == "phase" and e["phase"] == "model")
    assert model_event["tier"] == "tier2"
    assert model_event["model"] == "anthropic/claude-sonnet-4-6"

    tokens_event = next(e for e in events if e["type"] == "tokens")
    assert tokens_event["output"] == 421

    draft = events[-1]["draft"]
    assert draft["display_name"] == "Test App"
    assert draft["version"] == 1
    assert draft["build_spec"].startswith("# Test")
    assert "created_at" in draft


def test_build_draft_events_power_advertises_tier1(tmp_path, stub_anthropic):
    """The phase[model] event must surface the chosen tier so the UI can
    label the streaming progress with the right model + tier badge."""
    from evolve_admin.web import spec_routes as sr

    stub_anthropic(draft_json=_VALID_DRAFT_JSON)

    events = list(sr._build_draft_events(
        description="A test.", target_bots=[], shared_dir=tmp_path, power=True,
    ))
    model_event = next(e for e in events if e["type"] == "phase" and e["phase"] == "model")
    assert model_event["tier"] == "tier1"
    assert "opus" in model_event["model"]


def test_build_draft_events_surfaces_api_key_error(tmp_path, monkeypatch):
    """No API key configured → single error event, no crash."""
    from evolve_admin.web import spec_routes as sr

    monkeypatch.setattr(sr, "_resolve_api_key", lambda: None)

    events = list(sr._build_draft_events(
        description="anything", target_bots=[], shared_dir=tmp_path,
    ))
    assert events[-1]["type"] == "error"
    assert "API key" in events[-1]["message"]


# ── Sync _build_draft wrapper ─────────────────────────────────────────────────

def test_sync_build_draft_returns_dict_for_evo_chat_path(tmp_path, stub_anthropic):
    """Evo's chat-flow app-create handler calls _build_draft synchronously.
    The wrapper must drain the event stream and return the final draft."""
    from evolve_admin.web import spec_routes as sr

    stub_anthropic(draft_json=_VALID_DRAFT_JSON)

    draft = sr._build_draft(
        description="A small app.",
        target_bots=["bot_a"],
        shared_dir=tmp_path,
    )
    assert draft["display_name"] == "Test App"
    assert draft["test_command"] == "pytest -q"
    # The evo handler indexes by these keys; back-compat check.
    for key in ("version", "display_name", "description", "build_spec",
                "application_tags", "requirements", "app_dependencies",
                "test_command", "test_exemption_reason", "usage",
                "conflicts", "suggestions", "created_at"):
        assert key in draft, f"sync wrapper dropped {key!r}"


def test_sync_build_draft_raises_on_stream_error(tmp_path, monkeypatch):
    """Stream errors must raise so evo's chat handler can render its
    'draft failed' prompt instead of pretending nothing went wrong."""
    from evolve_admin.web import spec_routes as sr

    monkeypatch.setattr(sr, "_resolve_api_key", lambda: None)

    with pytest.raises(RuntimeError, match="API key"):
        sr._build_draft(description="x", target_bots=[], shared_dir=tmp_path)


# ── End-to-end SSE route ──────────────────────────────────────────────────────

def _make_flask_app(shared_dir: Path):
    from flask import Flask
    from evolve_admin.web.spec_routes import register_spec_routes

    app = Flask(__name__)
    register_spec_routes(app, shared_dir)
    return app


def _consume_sse(byte_iter) -> list[dict]:
    """Parse a Flask test_client.iter_encoded() stream of SSE frames into
    a list of {event, data} dicts."""
    out: list[dict] = []
    buf = b""
    for chunk in byte_iter:
        buf += chunk
        while b"\n\n" in buf:
            frame, _, buf = buf.partition(b"\n\n")
            current_event = None
            data_lines: list[str] = []
            for raw in frame.split(b"\n"):
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not current_event and not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines)) if data_lines else {}
            except json.JSONDecodeError:
                payload = {"_raw": "\n".join(data_lines)}
            out.append({"event": current_event or "message", "data": payload})
    return out


# ── Async background-job tests (2026-06-05 architecture) ─────────────────────
#
# POST /api/specs no longer streams SSE — it dispatches generation to a
# background worker and returns session_id JSON immediately. The client
# polls GET /api/specs/<id> for progress. Tests use a synchronous-thread
# monkeypatch so the worker completes inline, no sleeps needed.

class _SyncThread:
    """Drop-in for threading.Thread that runs target inline on start().

    Tests use this to make spec_jobs.register_worker exercise the worker
    synchronously. After ``start()`` returns, the worker has already
    completed (success or failure) — the session file on disk reflects
    final state. Avoids time.sleep-based polling in tests.
    """
    def __init__(self, target, args=(), name=None, daemon=None):
        self._target = target
        self._args = args
        self._name = name
        self._completed = False

    def start(self):
        self._target(*self._args)
        self._completed = True

    def is_alive(self):
        return False  # We always complete during start()


@pytest.fixture
def sync_workers(monkeypatch):
    """Patch threading.Thread inside spec_jobs to run synchronously.

    Tests that exercise the dispatch + completion path use this so the
    background worker runs inline and the test can assert on the final
    state immediately after the POST returns.
    """
    from evolve_admin.web import spec_jobs
    monkeypatch.setattr(spec_jobs.threading, "Thread", _SyncThread)
    # Reset the active-workers registry between tests so concurrency
    # cap state doesn't leak across runs.
    spec_jobs._active_workers.clear()
    yield
    spec_jobs._active_workers.clear()


def test_post_specs_dispatches_async_generation(tmp_path, stub_anthropic, sync_workers):
    """POST /api/specs returns 200 JSON with session_id immediately,
    NOT a streaming response. The background worker runs (synchronously
    in tests via sync_workers) and the session ends up in status='draft'
    with the new draft appended."""
    stub_anthropic(draft_json=_VALID_DRAFT_JSON, output_tokens=200)

    app = _make_flask_app(tmp_path)
    client = app.test_client()
    resp = client.post(
        "/api/specs",
        json={"description": "Hello world app", "target_bots": ["bot_a"], "power": False},
    )
    assert resp.status_code == 200
    # Critical contract: response is JSON, not text/event-stream.
    assert resp.mimetype == "application/json", (
        f"POST /api/specs must return JSON (background-job pattern), "
        f"got {resp.mimetype}"
    )
    body = resp.get_json()
    assert body["session_id"].startswith("s")
    assert body["status"] in ("gathering", "draft")  # depending on worker timing
    assert body["generation"]["version"] == 1

    # Worker has completed synchronously (sync_workers patch). Polling
    # the session endpoint must show final state.
    session_id = body["session_id"]
    detail_resp = client.get(f"/api/specs/{session_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.get_json()

    assert detail["status"] == "draft"
    assert detail["target_bots"] == ["bot_a"]
    assert len(detail["drafts"]) == 1
    assert detail["drafts"][0]["display_name"] == "Test App"
    assert detail["generation"]["status"] == "completed"
    assert detail["generation"]["phase"] == "done"


def test_post_specs_validation_error_returns_400(tmp_path):
    """Missing description returns HTTP 400 JSON. The wizard frontend
    now consumes JSON, not SSE — validation errors propagate via the
    standard HTTP error path."""
    app = _make_flask_app(tmp_path)
    client = app.test_client()
    resp = client.post("/api/specs", json={"description": "", "target_bots": ["bot_a"]})
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"
    body = resp.get_json()
    assert "description" in body["error"]


def test_post_specs_worker_failure_propagates_to_session(tmp_path, monkeypatch, sync_workers):
    """When the worker hits an Anthropic error, session.generation
    transitions to status='failed' with the error message. The wizard
    polls and surfaces this to the operator."""
    from evolve_admin.web import spec_routes as sr

    monkeypatch.setattr(sr, "_resolve_api_key", lambda: None)  # forces API-key error
    monkeypatch.setattr(
        sr, "_resolve_spec_model",
        lambda power=False: ("tier2", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
    )

    app = _make_flask_app(tmp_path)
    client = app.test_client()
    resp = client.post(
        "/api/specs",
        json={"description": "x", "target_bots": [], "power": False},
    )
    assert resp.status_code == 200  # dispatch succeeded; worker handles the failure
    session_id = resp.get_json()["session_id"]

    detail = client.get(f"/api/specs/{session_id}").get_json()
    assert detail["generation"]["status"] == "failed"
    assert "API key" in detail["generation"]["error"]
    # The session itself stays in "gathering" — no draft was produced.
    assert detail["status"] == "gathering"
    assert detail["drafts"] == []


def test_post_specs_concurrency_cap_returns_503(tmp_path, monkeypatch):
    """Beyond MAX_ACTIVE_WORKERS, dispatch returns 503. Tests don't
    use sync_workers here — we leave real threads "running" to fill the
    registry, then assert the cap fires."""
    from evolve_admin.web import spec_jobs

    # Pre-populate the registry with fake "alive" thread entries.
    class _AliveThread:
        def is_alive(self): return True
        def start(self): pass
    spec_jobs._active_workers.clear()
    for i in range(spec_jobs.MAX_ACTIVE_WORKERS):
        spec_jobs._active_workers[f"s-fake{i}"] = {
            "thread": _AliveThread(),
            "cancel": [False],
        }

    app = _make_flask_app(tmp_path)
    client = app.test_client()
    resp = client.post(
        "/api/specs",
        json={"description": "x", "target_bots": [], "power": False},
    )
    assert resp.status_code == 503
    body = resp.get_json()
    assert "Too many active" in body["error"]
    assert body["active_workers"] == spec_jobs.MAX_ACTIVE_WORKERS
    assert body["max_workers"] == spec_jobs.MAX_ACTIVE_WORKERS

    # Cleanup so subsequent tests don't inherit
    spec_jobs._active_workers.clear()


def test_post_iterate_dispatches_async_iteration(tmp_path, stub_anthropic, sync_workers):
    """Iterate endpoint also dispatches to a background worker (no SSE).
    POST returns 200 JSON with session_id; the worker (synchronous in
    tests) produces draft v2 + appends it to the session."""
    from evolve_admin.applications.spec_session import (
        SpecSession, new_session_id, save_session,
    )
    from evolve_admin.applications.ids import now_iso

    stub_anthropic(
        draft_json={**_VALID_DRAFT_JSON, "display_name": "Iterated App"},
        output_tokens=80,
    )

    ts = now_iso()
    session = SpecSession(
        session_id=new_session_id(),
        status="draft",
        target_bots=["bot_a"],
        input="seed description",
        drafts=[{**_VALID_DRAFT_JSON, "version": 1, "created_at": ts}],
        feedback_history=[],
        approved_version=None,
        forge_jobs=[],
        created_at=ts,
        updated_at=ts,
        created_by="test",
    )
    save_session(session, tmp_path)

    app = _make_flask_app(tmp_path)
    client = app.test_client()
    resp = client.post(
        f"/api/specs/{session.session_id}/iterate",
        json={"feedback": "Add a second feature."},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    body = resp.get_json()
    assert body["status"] == "iterating"
    assert body["generation"]["version"] == 2

    # Worker has completed synchronously; check final session state.
    detail = client.get(f"/api/specs/{session.session_id}").get_json()
    assert detail["status"] == "iterating"
    assert len(detail["drafts"]) == 2
    assert detail["drafts"][1]["display_name"] == "Iterated App"
    assert detail["drafts"][1]["version"] == 2
    assert detail["feedback_history"][0]["feedback"] == "Add a second feature."


# ── Wallclock keepalive on the Anthropic socket (2026-06-05 follow-up) ───────
#
# Background: the worker thread in spec_jobs.run_generation_in_background
# iterates _stream_anthropic for the lifetime of a single Anthropic Messages
# API streaming response. Before this fix, the urlopen ``timeout=60`` made
# the worker fail with "Anthropic stream interrupted" whenever Anthropic
# went silent for more than 60s during a long Opus generation — a real
# failure mode surfaced 2026-06-05 during an operator walkthrough. The
# earlier SSE-layer fix (Phase 1 of the original investigation, PR #2181)
# was rendered moot when PR #2183 moved generation off SSE entirely; this
# fix is the salvaged piece that's still useful under the new architecture
# — it keeps the worker thread itself alive across arbitrarily long quiet
# periods.


class _FakeUrlopenWithTimeouts:
    """Like _FakeUrlopen but ``readline()`` raises ``socket.timeout`` a
    configurable number of times before returning each real line.

    Models the Anthropic API going silent for one or more 15-second windows
    — exactly the case the wallclock recovery is designed to cover.
    """

    def __init__(self, payload: bytes, *, timeouts_per_line: int = 0):
        self._buf = io.BytesIO(payload)
        self._timeouts_per_line = timeouts_per_line
        self._timeouts_remaining = timeouts_per_line
        self.timeout_count = 0  # exposed for assertions

    def readline(self):
        if self._timeouts_remaining > 0:
            self._timeouts_remaining -= 1
            self.timeout_count += 1
            raise socket.timeout("simulated upstream silence")
        line = self._buf.readline()
        if line:
            # Reset the timeout-budget so the NEXT line also gets gated
            # by silences. Without this reset, only the very first line
            # would see timeouts and the rest of the stream would arrive
            # instantly.
            self._timeouts_remaining = self._timeouts_per_line
        return line

    def close(self):
        try:
            self._buf.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def test_stream_anthropic_recovers_socket_timeout_as_keepalive(monkeypatch):
    """When ``readline()`` raises ``socket.timeout`` (no upstream bytes
    within _KEEPALIVE_INTERVAL_SECONDS), ``_stream_anthropic`` must yield
    a ``keepalive`` event AND keep reading — never terminate with an
    error. This is what stops the spec-generation worker thread from
    dying on a quiet Anthropic stream.
    """
    from evolve_admin.web import spec_routes as sr

    payload = _make_fake_anthropic_stream(
        [json.dumps(_VALID_DRAFT_JSON)],
        output_tokens=77,
    )
    fake = _FakeUrlopenWithTimeouts(payload, timeouts_per_line=2)
    monkeypatch.setattr(sr.urllib.request, "urlopen", lambda *a, **kw: fake)

    events = list(sr._stream_anthropic(
        system_prompt="you are a thing",
        user_message="build me an app",
        model="claude-sonnet-4-6",
        api_key="test-key",
    ))

    keepalive_count = sum(1 for e in events if e.get("type") == "keepalive")
    assert keepalive_count >= 4, (
        f"Expected >= 4 wallclock-triggered keepalives (2 timeouts per "
        f"line, multiple lines); got {keepalive_count}. Event types: "
        f"{[e.get('type') for e in events]}"
    )
    assert fake.timeout_count >= 4, (
        f"Test fixture should have raised socket.timeout >= 4 times; "
        f"got {fake.timeout_count}. The production code may be swallowing "
        f"timeouts in a way that breaks the wallclock recovery."
    )

    # Stream must still complete normally — no error event, deltas arrived,
    # final tokens event present.
    types = [e["type"] for e in events]
    assert "error" not in types, (
        f"socket.timeout must be recovered as a keepalive, not surfaced "
        f"as an error. Events: {types}"
    )
    assert "delta" in types, "Real content_block_delta events must still arrive"
    assert types[-1] == "tokens", f"Final event must be tokens, got {types[-1]!r}"


def test_stream_anthropic_real_oserror_still_terminates_stream(monkeypatch):
    """Regression guard: only ``socket.timeout`` is recoverable. A
    non-timeout ``OSError`` (connection reset, TLS error, etc.) must still
    surface as a fatal error event. We don't want the new wallclock
    recovery to mask genuine transport failures.
    """
    from evolve_admin.web import spec_routes as sr

    class _ResetMidStream:
        def __init__(self):
            self._first = True

        def readline(self):
            if self._first:
                self._first = False
                # Emit message_start so the loop chews on something before
                # the reset (proves the reset path doesn't depend on the
                # first read failing).
                return b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n'
            raise OSError("simulated connection reset")

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    monkeypatch.setattr(sr.urllib.request, "urlopen", lambda *a, **kw: _ResetMidStream())

    events = list(sr._stream_anthropic(
        system_prompt="x", user_message="x", model="m", api_key="k",
    ))
    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events, f"Real OSError must surface as error event; got {events}"
    assert "Anthropic stream interrupted" in error_events[0]["message"]


def test_stream_anthropic_uses_keepalive_interval_for_urlopen_timeout(monkeypatch):
    """The socket-level read timeout passed to ``urlopen`` must be
    :data:`_KEEPALIVE_INTERVAL_SECONDS` (not the pre-fix 60s value),
    because that's what arms the wallclock recovery. If a future edit
    reverts the timeout to a multi-minute value, recovery becomes a
    no-op and this test catches it.
    """
    from evolve_admin.web import spec_routes as sr

    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeUrlopen(b"data: [DONE]\n\n")

    monkeypatch.setattr(sr.urllib.request, "urlopen", fake_urlopen)

    list(sr._stream_anthropic(
        system_prompt="x", user_message="x", model="m", api_key="k",
    ))
    assert captured["timeout"] == sr._KEEPALIVE_INTERVAL_SECONDS, (
        f"urlopen timeout must equal _KEEPALIVE_INTERVAL_SECONDS "
        f"({sr._KEEPALIVE_INTERVAL_SECONDS}); got {captured.get('timeout')!r}. "
        f"A larger value lets the worker thread sit idle past the previous "
        f"60s boundary before recovery fires."
    )


def test_build_draft_events_forwards_keepalive(tmp_path, monkeypatch):
    """``_build_draft_events`` must forward ``keepalive`` events from
    ``_stream_anthropic`` to its caller. Without the forward, the worker
    thread (which iterates _build_draft_events) wouldn't get a periodic
    chance to check its cancel_flag during long quiet periods.
    """
    from evolve_admin.web import spec_routes as sr

    payload = _make_fake_anthropic_stream(
        [json.dumps(_VALID_DRAFT_JSON)],
        output_tokens=42,
    )
    fake = _FakeUrlopenWithTimeouts(payload, timeouts_per_line=1)
    monkeypatch.setattr(sr.urllib.request, "urlopen", lambda *a, **kw: fake)
    monkeypatch.setattr(sr, "_resolve_api_key", lambda: "test-key")
    monkeypatch.setattr(
        sr, "_resolve_spec_model",
        lambda power=False: ("tier2", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
    )

    events = list(sr._build_draft_events(
        description="A small app.",
        target_bots=[],
        shared_dir=tmp_path,
        version=1,
        power=False,
    ))

    keepalive_count = sum(1 for e in events if e.get("type") == "keepalive")
    assert keepalive_count >= 2, (
        f"_build_draft_events must forward keepalives from _stream_anthropic. "
        f"Got {keepalive_count} keepalives; expected >= 2 (one per simulated "
        f"timeout). Event types: {[e.get('type') for e in events]}"
    )
    # Final draft must still arrive.
    assert events[-1]["type"] == "draft"
    assert events[-1]["draft"]["display_name"] == "Test App"


def test_worker_completes_despite_anthropic_socket_silence(
    tmp_path, monkeypatch, sync_workers,
):
    """End-to-end: when the Anthropic stream goes silent multiple times
    during a generation, the background worker thread must still complete
    successfully — session.generation.status == "completed", draft appended,
    session.status flipped to "draft". This is the user-facing guarantee
    that long quiet Opus generations no longer fail the Create-App wizard.
    """
    from evolve_admin.web import spec_routes as sr

    payload = _make_fake_anthropic_stream(
        [json.dumps(_VALID_DRAFT_JSON)],
        output_tokens=200,
    )
    fake = _FakeUrlopenWithTimeouts(payload, timeouts_per_line=2)
    monkeypatch.setattr(sr.urllib.request, "urlopen", lambda *a, **kw: fake)
    monkeypatch.setattr(sr, "_resolve_api_key", lambda: "test-key")
    monkeypatch.setattr(
        sr, "_resolve_spec_model",
        lambda power=False: ("tier2", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
    )

    app = _make_flask_app(tmp_path)
    client = app.test_client()
    resp = client.post(
        "/api/specs",
        json={"description": "Quiet long generation", "target_bots": ["bot_a"], "power": False},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    session_id = body["session_id"]

    # Worker ran synchronously via the sync_workers fixture. Final state
    # must be a completed generation + draft appended.
    detail = client.get(f"/api/specs/{session_id}").get_json()
    gen = detail.get("generation") or {}
    assert gen.get("status") == "completed", (
        f"Worker must survive simulated Anthropic silence and complete "
        f"successfully. Got generation.status={gen.get('status')!r}, "
        f"error={gen.get('error')!r}. Timeouts injected by fixture: "
        f"{fake.timeout_count}"
    )
    assert detail["status"] == "draft"
    assert len(detail["drafts"]) == 1
    assert detail["drafts"][0]["display_name"] == "Test App"
    # Confirm the timeouts actually fired in the fixture — otherwise the
    # test is asserting nothing about the recovery path.
    assert fake.timeout_count >= 2, (
        f"Test fixture didn't actually exercise the recovery path "
        f"(timeout_count={fake.timeout_count})."
    )
    assert detail["generation"]["status"] == "completed"
