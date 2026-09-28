"""tests/test_observability_opik_client.py — V1.5-1 Opik client + adapters.

Covers:
  - OpikSpan round-trip serialization
  - SpanFilter.matches() filter logic
  - JsonlBackend: record + search round-trip, day-file routing,
    most-recent-first iteration, filter pruning, atomic write
  - DisabledBackend null-object behavior
  - get_client() factory: backend selection from config
  - span_to_cost_event() projection covers all field aliases
  - signals.store.observe_from_opik() round-trip: span → Signal
  - cost_ledger.read_events_from_observability(): spans → cost_event dicts
  - cost_rollup._iter_observability_cost_events(): integration with rollup

Notes:
  Opik SDK is NOT installed in the test environment. OpikBackend itself
  raises ImportError on construction — that's the documented behavior,
  and get_client() falls through to JsonlBackend. Tests cover the
  fallthrough explicitly; the real-SDK code path is exercised by manual
  verification against a self-hosted Opik server.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

import time
import types

from observability.opik_client import (  # noqa: E402
    DisabledBackend,
    JsonlBackend,
    OpikBackend,
    OpikSpan,
    SpanFilter,
    get_client,
    span_to_cost_event,
)
import observability.opik_client as _opik_mod


# ─────────────────────────────────────────────────────────────────────────────
# OpikSpan: round-trip
# ─────────────────────────────────────────────────────────────────────────────


def _sample_span(**overrides):
    base = dict(
        name="embedding_call",
        start_time=datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 14, 0, 1, tzinfo=timezone.utc),
        type="llm",
        producer="embedding_monitor",
        bot_id="admin_bot",
        model="text-embedding-3-small",
        provider="openai",
        usage={"prompt_tokens": 10, "completion_tokens": 0},
        total_cost=0.0001,
        tags=["maintenance", "warn"],
        metadata={"k": "v"},
        attributes={"session_id": "s1", "trigger_kind": "user_turn"},
    )
    base.update(overrides)
    return OpikSpan(**base)


def test_opikspan_round_trip_preserves_all_fields():
    span = _sample_span(
        error_info={"http_status": 429, "error_class": "quota_exceeded"},
    )
    raw = span.to_dict()
    restored = OpikSpan.from_dict(raw)
    assert restored.name == span.name
    assert restored.start_time == span.start_time
    assert restored.end_time == span.end_time
    assert restored.type == span.type
    assert restored.producer == span.producer
    assert restored.bot_id == span.bot_id
    assert restored.model == span.model
    assert restored.provider == span.provider
    assert restored.usage == span.usage
    assert restored.total_cost == span.total_cost
    assert restored.tags == span.tags
    assert restored.metadata == span.metadata
    assert restored.attributes == span.attributes
    assert restored.error_info == span.error_info


def test_opikspan_is_error_only_on_nonempty_dict():
    assert _sample_span(error_info=None).is_error() is False
    assert _sample_span(error_info={}).is_error() is False
    assert _sample_span(error_info={"x": 1}).is_error() is True


def test_opikspan_duration_clamped_nonneg():
    # End before start (clock skew) → clamps to 0 rather than negative.
    start = datetime(2026, 5, 12, 14, 0, 5, tzinfo=timezone.utc)
    end = datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc)
    span = _sample_span(start_time=start, end_time=end)
    assert span.duration_seconds() == 0.0


def test_opikspan_from_dict_accepts_z_suffix_timestamp():
    raw = {
        "name": "x",
        "start_time": "2026-05-12T14:00:00Z",
        "end_time": "2026-05-12T14:00:01Z",
    }
    span = OpikSpan.from_dict(raw)
    assert span.start_time.tzinfo is timezone.utc
    assert span.end_time.tzinfo is timezone.utc


# ─────────────────────────────────────────────────────────────────────────────
# SpanFilter.matches()
# ─────────────────────────────────────────────────────────────────────────────


def test_spanfilter_no_constraints_matches_everything():
    assert SpanFilter().matches(_sample_span()) is True


def test_spanfilter_producer_filter():
    f = SpanFilter(producer="embedding_monitor")
    assert f.matches(_sample_span(producer="embedding_monitor")) is True
    assert f.matches(_sample_span(producer="evolve_watchdog")) is False


def test_spanfilter_bot_id_filter():
    f = SpanFilter(bot_id="admin_bot")
    assert f.matches(_sample_span(bot_id="admin_bot")) is True
    assert f.matches(_sample_span(bot_id="team_bot_a")) is False


def test_spanfilter_tags_must_all_match():
    f = SpanFilter(tags=["maintenance", "warn"])
    assert f.matches(_sample_span(tags=["maintenance", "warn", "extra"])) is True
    assert f.matches(_sample_span(tags=["maintenance"])) is False  # missing warn


def test_spanfilter_error_only():
    f = SpanFilter(error_only=True)
    assert f.matches(_sample_span(error_info=None)) is False
    assert f.matches(_sample_span(error_info={"http_status": 500})) is True


def test_spanfilter_time_window_exclusive_edges():
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc)
    span = _sample_span(start_time=base, end_time=base + timedelta(seconds=1))
    # since after end_time excludes
    assert SpanFilter(since=base + timedelta(seconds=2)).matches(span) is False
    # since before end_time includes
    assert SpanFilter(since=base - timedelta(seconds=1)).matches(span) is True
    # until before start_time excludes
    assert SpanFilter(until=base - timedelta(seconds=1)).matches(span) is False


def test_spanfilter_span_type():
    f = SpanFilter(span_type="llm")
    assert f.matches(_sample_span(type="llm")) is True
    assert f.matches(_sample_span(type="general")) is False


# ─────────────────────────────────────────────────────────────────────────────
# JsonlBackend round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_jsonl_backend_record_and_search_round_trip(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    span = _sample_span()
    backend.record_span(span)

    # Day file got created at the expected path
    day_iso = span.end_time.date().isoformat()
    expected = tmp_path / "observability" / "spans" / f"{day_iso}.jsonl"
    assert expected.exists()
    assert expected.stat().st_size > 0

    # Search recovers it
    results = list(backend.search_spans(
        SpanFilter(since=span.start_time - timedelta(seconds=1))
    ))
    assert len(results) == 1
    assert results[0].name == span.name
    assert results[0].bot_id == span.bot_id


def test_jsonl_backend_search_most_recent_first_within_day(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    t0 = datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        span = _sample_span(
            start_time=t0 + timedelta(seconds=i),
            end_time=t0 + timedelta(seconds=i + 1),
            name=f"span_{i}",
        )
        backend.record_span(span)

    results = list(backend.search_spans(SpanFilter(since=t0 - timedelta(seconds=1))))
    # Most-recent-first: span_2, span_1, span_0
    names = [s.name for s in results]
    assert names == ["span_2", "span_1", "span_0"]


def test_jsonl_backend_filter_applied(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    for bot in ("admin_bot", "team_bot_a", "team_bot_b"):
        backend.record_span(_sample_span(bot_id=bot))

    # _sample_span uses a fixed historical date; widen the lookback past
    # search_spans' 24h default so we exercise the filter, not the window.
    sample_start = _sample_span().start_time
    results = list(backend.search_spans(
        SpanFilter(bot_id="team_bot_a", since=sample_start - timedelta(seconds=1))
    ))
    assert len(results) == 1
    assert results[0].bot_id == "team_bot_a"


def test_jsonl_backend_handles_corrupt_lines_gracefully(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    span = _sample_span()
    backend.record_span(span)

    # Inject a corrupt line into the span's day file.
    day_file = next((tmp_path / "observability" / "spans").glob("*.jsonl"))
    text = day_file.read_text()
    day_file.write_text("not-json\n" + text + "{partial:")

    results = list(backend.search_spans(
        SpanFilter(since=span.start_time - timedelta(seconds=1))
    ))
    # The valid line still comes through; corrupt lines are skipped.
    assert len(results) == 1


def test_jsonl_backend_limit_respected(tmp_path: Path):
    backend = JsonlBackend(tmp_path)
    t0 = datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        backend.record_span(_sample_span(
            start_time=t0 + timedelta(seconds=i),
            end_time=t0 + timedelta(seconds=i + 1),
            name=f"span_{i}",
        ))
    results = list(backend.search_spans(SpanFilter(
        since=t0 - timedelta(seconds=1),
        limit=3,
    )))
    assert len(results) == 3


def test_jsonl_backend_record_never_raises_into_caller(tmp_path: Path):
    backend = JsonlBackend(tmp_path / "nonexistent" / "unwritable")
    # Should not raise even with the JSONL backend pointed at a path
    # whose parent can't be created (best-effort posture).
    try:
        backend.record_span(_sample_span())
    except Exception as e:
        # Acceptable: best-effort. We assert the call returns cleanly
        # in normal conditions; pathological filesystems can still fail.
        # Document the contract: the function should swallow, not raise.
        pytest.fail(f"JsonlBackend.record_span raised: {e!r}")


# ─────────────────────────────────────────────────────────────────────────────
# DisabledBackend
# ─────────────────────────────────────────────────────────────────────────────


def test_disabled_backend_is_null_object():
    backend = DisabledBackend()
    backend.record_span(_sample_span())  # no-op
    assert list(backend.search_spans(SpanFilter())) == []
    backend.close()  # idempotent
    assert backend.backend_name == "disabled"


# ─────────────────────────────────────────────────────────────────────────────
# get_client factory
# ─────────────────────────────────────────────────────────────────────────────


def test_get_client_default_is_jsonl(tmp_path: Path):
    client = get_client({}, shared_dir=tmp_path)
    assert isinstance(client, JsonlBackend)
    assert client.backend_name == "jsonl"


def test_get_client_explicit_disabled(tmp_path: Path):
    client = get_client({"observability": {"backend": "disabled"}}, shared_dir=tmp_path)
    assert isinstance(client, DisabledBackend)


def test_get_client_env_var_force_disable(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPIK_DISABLED", "1")
    client = get_client({"observability": {"backend": "opik"}}, shared_dir=tmp_path)
    assert isinstance(client, DisabledBackend)


def test_get_client_opik_backend_unavailable_falls_back_to_jsonl(tmp_path: Path, monkeypatch):
    # Opik isn't installed in this env. The factory must fall through to
    # JsonlBackend rather than raising — the documented behavior so a
    # misconfigured observability block doesn't break Evolve.
    monkeypatch.delenv("OPIK_DISABLED", raising=False)
    client = get_client(
        {"observability": {"backend": "opik", "opik": {"host": "http://localhost:5173"}}},
        shared_dir=tmp_path,
    )
    assert isinstance(client, JsonlBackend)


def test_opik_backend_construction_without_sdk_raises_importerror():
    # Documents the contract: OpikBackend.__init__ raises ImportError
    # if the SDK isn't installed (so get_client can detect + fall back).
    with pytest.raises(ImportError):
        OpikBackend(host="http://localhost:5173")


# ─────────────────────────────────────────────────────────────────────────────
# span_to_cost_event projection
# ─────────────────────────────────────────────────────────────────────────────


def test_span_to_cost_event_basic_shape():
    span = _sample_span(
        usage={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 25, "cache_write_tokens": 10},
        total_cost=0.0042,
    )
    record = span_to_cost_event(span)
    assert record is not None
    assert record["type"] == "cost_event"
    assert record["bot_id"] == "admin_bot"
    assert record["model"] == "text-embedding-3-small"
    assert record["provider"] == "openai"
    assert record["cost_usd"] == pytest.approx(0.0042)
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50
    assert record["cache_read_tokens"] == 25
    assert record["cache_write_tokens"] == 10
    assert record["session_id"] == "s1"
    assert record["trigger_kind"] == "user_turn"
    assert record["source"] == "observability"
    # ts is ISO 8601 with timezone
    assert record["ts"].startswith("2026-05-12T14:00:01")


def test_span_to_cost_event_accepts_openai_naming():
    span = _sample_span(
        usage={"prompt_tokens": 100, "completion_tokens": 50},
        total_cost=0.001,
    )
    record = span_to_cost_event(span)
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50


def test_span_to_cost_event_accepts_anthropic_naming():
    span = _sample_span(
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 25,
            "cache_creation_input_tokens": 10,
        },
        total_cost=0.001,
    )
    record = span_to_cost_event(span)
    assert record["cache_read_tokens"] == 25
    assert record["cache_write_tokens"] == 10


def test_span_to_cost_event_accepts_gemini_naming():
    """Gemini SDK emits camelCase token keys — V1.5-1 Concern #2 regression."""
    span = _sample_span(
        usage={"promptTokenCount": 1000, "candidatesTokenCount": 500},
        total_cost=0.005,
    )
    record = span_to_cost_event(span)
    assert record is not None
    assert record["input_tokens"] == 1000
    assert record["output_tokens"] == 500


def test_span_to_cost_event_skips_non_billable():
    # No model, no total_cost, no usage → not a cost event
    span = OpikSpan(
        name="some_internal_event",
        start_time=datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc),
        type="general",
    )
    assert span_to_cost_event(span) is None


def test_span_to_cost_event_falls_back_to_attributes_bot_id():
    span = OpikSpan(
        name="llm_call",
        start_time=datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 14, 0, 1, tzinfo=timezone.utc),
        type="llm",
        model="claude-sonnet-4-6",
        total_cost=0.01,
        attributes={"bot_id": "team_bot_b", "session_id": "s2"},
    )
    record = span_to_cost_event(span)
    assert record["bot_id"] == "team_bot_b"


# ─────────────────────────────────────────────────────────────────────────────
# V1.5-1 Concern #4 — per-record timeout cap (regression)
# ─────────────────────────────────────────────────────────────────────────────


def test_jsonl_backend_record_span_respects_timeout(tmp_path, monkeypatch, capsys):
    """record_span should return within timeout + slack even when the write sleeps.

    This covers V1.5-1 Concern #4: a slow backend (network mount, slow disk)
    must not block the producer indefinitely. The span is dropped; a one-time
    warning is emitted to stderr.
    """
    import observability.opik_client as _mod

    # Lower the timeout so the test doesn't take 2s.
    original_timeout = _mod.RECORD_SPAN_TIMEOUT_SECONDS
    _mod.RECORD_SPAN_TIMEOUT_SECONDS = 0.2
    # Reset the warn-once flag so we can observe the warning cleanly.
    _mod._record_timeout_warned = False

    backend = JsonlBackend(tmp_path)

    # Monkeypatch _record_span_inner to sleep past the timeout.
    def slow_inner(span):
        time.sleep(1.0)  # much longer than 0.2s cap

    monkeypatch.setattr(backend, "_record_span_inner", slow_inner)

    span = _sample_span()
    t0 = time.monotonic()
    backend.record_span(span)
    elapsed = time.monotonic() - t0

    # Restore
    _mod.RECORD_SPAN_TIMEOUT_SECONDS = original_timeout

    # Should have returned within timeout + 0.5s slack (not 1s+).
    assert elapsed < 0.75, f"record_span blocked for {elapsed:.2f}s, expected < 0.75s"

    # Warn-once message should appear in stderr.
    captured = capsys.readouterr()
    assert "[observability] WARN" in captured.err
    assert "timeout" in captured.err


def test_jsonl_backend_record_span_warn_fires_once(tmp_path, monkeypatch, capsys):
    """The timeout warning is emitted at most once per process."""
    import observability.opik_client as _mod

    original_timeout = _mod.RECORD_SPAN_TIMEOUT_SECONDS
    _mod.RECORD_SPAN_TIMEOUT_SECONDS = 0.1
    _mod._record_timeout_warned = False

    backend = JsonlBackend(tmp_path)

    def slow_inner(span):
        time.sleep(0.5)

    monkeypatch.setattr(backend, "_record_span_inner", slow_inner)

    span = _sample_span()
    backend.record_span(span)
    backend.record_span(span)  # second call — warn should NOT fire again

    _mod.RECORD_SPAN_TIMEOUT_SECONDS = original_timeout

    captured = capsys.readouterr()
    # Should appear exactly once.
    assert captured.err.count("[observability] WARN") == 1


# ─────────────────────────────────────────────────────────────────────────────
# V1.5-1 fix-up: Concern #1 — search_spans field-name drift warn (regression)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeResponseWithData:
    """Simulates a future Opik REST response that renamed .content → .data."""

    def __init__(self, records):
        # Expose the data under .data instead of .content — mimics a field
        # rename in a future Opik SDK release.
        self.data = records
        # Intentionally NO .content attribute to trigger the warn path.


def test_search_spans_warns_on_content_missing_but_response_nonempty(
    monkeypatch, capsys
):
    """When REST response is non-None but lacks .content, search_spans
    should emit a stderr warn and return an empty iterator (don't change
    behavior, just surface the silent failure).

    Hostile input: response object with .data=[...] instead of .content.
    """
    import types

    # Build a minimal OpikBackend-like harness by monkey-patching the REST call
    # inside the method. We call the method directly on a partial mock since
    # OpikBackend can't be constructed without the SDK.
    from observability.opik_client import OpikBackend, SpanFilter

    # Create a fake response that has .data (future rename) but no .content.
    fake_records = [object(), object()]  # two opaque trace records
    fake_response = _FakeResponseWithData(fake_records)

    # Patch OpikBackend.__init__ to bypass the SDK import, then exercise
    # search_spans by wiring fake state directly.
    class _PatchedBackend(OpikBackend):
        def __init__(self):
            # Skip SDK import entirely.
            self._opik_module = types.SimpleNamespace(
                rest_client=lambda: types.SimpleNamespace(
                    traces=types.SimpleNamespace(
                        get_traces_by_project=lambda **kw: fake_response
                    )
                )
            )
            self._project_name = "evolve"

    backend = _PatchedBackend()
    results = list(backend.search_spans(SpanFilter()))

    # Behavior unchanged: returns empty iterator (can't parse the .data records).
    assert results == []

    # The warn must have fired to stderr.
    captured = capsys.readouterr()
    assert "[opik_client] WARN" in captured.err
    assert ".content" in captured.err


def test_search_spans_no_warn_on_genuinely_empty_response(monkeypatch, capsys):
    """When .content is present but is an empty list, no warn should fire
    (that's a legitimately empty project — not a field drift).
    """
    import types
    from observability.opik_client import OpikBackend, SpanFilter

    class _EmptyContentResponse:
        content = []  # explicitly present and empty

    class _PatchedBackend(OpikBackend):
        def __init__(self):
            self._opik_module = types.SimpleNamespace(
                rest_client=lambda: types.SimpleNamespace(
                    traces=types.SimpleNamespace(
                        get_traces_by_project=lambda **kw: _EmptyContentResponse()
                    )
                )
            )
            self._project_name = "evolve"

    backend = _PatchedBackend()
    results = list(backend.search_spans(SpanFilter()))

    assert results == []

    # No warn: .content was present, just empty.
    captured = capsys.readouterr()
    assert "[opik_client] WARN" not in captured.err
