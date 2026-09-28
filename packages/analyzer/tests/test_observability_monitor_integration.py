"""tests/test_observability_monitor_integration.py — V1.5-1 monitor regression.

Capture-monitor dual/triple write: the existing JSONL + Signal paths
keep working, AND a span lands in observability. Plus the consumer
proof: emit_signals_from_observability_spans round-trips a recorded
span back into a Signal.
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

import embedding_monitor  # noqa: E402
from observability.opik_client import JsonlBackend, SpanFilter  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# evolve_watchdog: regression — JSONL + Signal + Span all written
# ─────────────────────────────────────────────────────────────────────────────


def test_watchdog_write_events_triple_writes(tmp_path: Path):
    """write_events writes JSONL + Signal + observability span."""
    from generators.evolve_watchdog import events as wd_events
    from schema.watchdog import WatchdogEvent

    event = WatchdogEvent(
        id="wd-test-1",
        event_type="proposal_volume_deviation",
        bot_id="admin_bot",
        severity="warn",
        timestamp=datetime.now(timezone.utc).isoformat(),
        details={"deviation": 3.0},
    )
    wd_events.write_events([event], shared_dir=tmp_path)

    # 1. JSONL written
    watchdog_dir = tmp_path / "watchdog"
    assert watchdog_dir.exists()
    jsonl_files = list(watchdog_dir.glob("*.jsonl"))
    assert len(jsonl_files) >= 1
    lines = jsonl_files[0].read_text().splitlines()
    assert any("proposal_volume_deviation" in line for line in lines)

    # 2. Signal observed
    firing = list(signals_store.iter_active(tmp_path))
    assert any(s.type == "proposal_volume_deviation" for s in firing)

    # 3. Observability span written (JSONL backend default)
    span_dir = tmp_path / "observability" / "spans"
    assert span_dir.exists(), "observability span dir missing — triple-write didn't fire"
    span_files = list(span_dir.glob("*.jsonl"))
    assert len(span_files) >= 1
    span_records = [json.loads(line) for line in span_files[0].read_text().splitlines() if line.strip()]
    assert any(
        r.get("name") == "watchdog.proposal_volume_deviation"
        for r in span_records
    ), f"expected span not found in {span_records}"


# ─────────────────────────────────────────────────────────────────────────────
# embedding_monitor: triple-write
# ─────────────────────────────────────────────────────────────────────────────


def test_embedding_monitor_emits_observability_span(tmp_path: Path, monkeypatch):
    """run_for_bot writes JSONL Signal AND triple-writes a span."""
    # Stub out the log read so collect_for_bot has data to detect.
    fake_log = (
        "2026-05-12T14:00:00+00:00 [memory] sync failed (search): "
        "Error: openai embeddings failed: 401\n"
    )
    monkeypatch.setattr(
        embedding_monitor, "read_gateway_errors",
        lambda bot_id, config, *, max_bytes: fake_log,
    )

    config = {"shared_dir": str(tmp_path), "embedding_monitor": {}}
    kept, n_fires = embedding_monitor.run_for_bot(
        "admin_bot", tmp_path, config,
        now=datetime(2026, 5, 12, 14, 0, 30, tzinfo=timezone.utc),
    )
    assert n_fires >= 1

    # Signal landed
    firing = list(signals_store.iter_active(tmp_path))
    assert any(
        s.producer == "embedding_monitor" and s.bot_id == "admin_bot"
        for s in firing
    )

    # Span landed
    backend = JsonlBackend(tmp_path)
    spans = list(backend.search_spans(SpanFilter(
        producer="embedding_monitor",
        since=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )))
    assert len(spans) >= 1
    assert spans[0].bot_id == "admin_bot"
    assert spans[0].is_error()


# ─────────────────────────────────────────────────────────────────────────────
# Consumer proof: read spans -> produce Signals (signals_phase end-to-end)
# ─────────────────────────────────────────────────────────────────────────────


def test_emit_signals_from_observability_spans_round_trips(tmp_path: Path):
    """A recorded error span gets converted back into a Signal via the
    observability path — proving the new pipeline closes the loop."""
    # Record a synthetic span as if produced by some upstream emitter.
    backend = JsonlBackend(tmp_path)
    now = datetime.now(timezone.utc)
    backend.record_event = backend.record_event  # ensure method exists
    backend.record_event(
        name="embedding_monitor.provider_failing",
        producer="embedding_monitor",
        bot_id="admin_bot",
        start_time=now,
        end_time=now,
        provider="openai",
        tags=["maintenance", "warn"],
        attributes={
            "event_type": "provider_failing",
            "severity": "warn",
            "flavor": "maintenance",
            "details": {"provider": "openai", "error_class": "auth_failed", "http_status": 401},
        },
        error_info={"http_status": 401, "error_class": "auth_failed"},
    )

    emitted = embedding_monitor.emit_signals_from_observability_spans(
        tmp_path,
        bot_id="admin_bot",
        lookback_seconds=86400,
        client=backend,
    )
    assert emitted >= 1

    # Signal landed via the observability path
    firing = list(signals_store.iter_active(tmp_path))
    matches = [s for s in firing if s.producer == "embedding_monitor"]
    assert len(matches) >= 1
    sig = matches[0]
    assert sig.bot_id == "admin_bot"
    assert sig.type == "provider_failing"
    # Signature carries the suffix the emitter built
    assert "openai" in sig.signature
    assert "auth_failed" in sig.signature
    # The observability details survived the round-trip
    assert sig.details.get("observability") is not None
