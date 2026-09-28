"""tests/test_cost_watchdog_fallback_exhaustion.py — chain-exhausted detector.

When OC's model failover can't resolve any model in the configured chain
(every step throws ``model_not_found``), each turn surfaces
``chain_exhausted`` to the caller. Each walk can bill input tokens
before the failure is declared final, so a steady stream is "money
burning right now."

These tests pin the parser shape (real OpenClaw log line schema —
``tslog`` JSON with ``"0"/"1"/"2"/_meta`` keys), the windowing,
threshold, and Signal payload. Reference incident: 2026-06-03
personal-bot, $36 in two background turns before the operator noticed.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import cost_watchdog  # noqa: E402


# Real shape lifted from the reference pod's
# /Users/<personal-bot-user>/.openclaw/logs/openclaw.log — keep the
# test fixture faithful so a future OC log schema change is caught by
# these tests rather than only at deploy.
def _exhausted_log_line(
    *,
    date_iso: str,
    requested_model: str = "claude-haiku-4-5",
    requested_provider: str = "anthropic",
    chain_position: int = 1,
) -> str:
    event = {
        "event": "model_fallback_decision",
        "tags": ["error_handling", "model_fallback", "candidate_failed"],
        "runId": "abc-123",
        "decision": "candidate_failed",
        "requestedProvider": requested_provider,
        "requestedModel": requested_model,
        "candidateProvider": requested_provider,
        "candidateModel": requested_model,
        "attempt": 1,
        "total": 1,
        "reason": "model_not_found",
        "errorPreview": (
            f"Unknown model: {requested_provider}/{requested_model}. "
            f"Found agents.defaults.models[\"{requested_provider}/"
            f"{requested_model}\"], but no matching "
            f"models.providers[\"{requested_provider}\"].models[] entry."
        ),
        "errorHash": "sha256:dcaa92b07c3c",
        "fallbackStepType": "fallback_step",
        "fallbackStepFromModel": f"{requested_provider}/{requested_model}",
        "fallbackStepFromFailureReason": "model_not_found",
        "fallbackStepFromFailureDetail": "Unknown model: ...",
        "fallbackStepChainPosition": chain_position,
        "fallbackStepFinalOutcome": "chain_exhausted",
        "isPrimary": True,
        "requestedModelMatched": True,
        "fallbackConfigured": False,
    }
    return json.dumps({
        "0": '{"subsystem":"model-fallback/decision"}',
        "1": event,
        "2": "model fallback decision",
        "_meta": {
            "runtime": "node",
            "runtimeVersion": "22.22.3",
            "name": '{"subsystem":"model-fallback/decision"}',
            "date": date_iso,
            "logLevelId": 4,
            "logLevelName": "WARN",
        },
    }, separators=(",", ":"))


def _other_log_line(date_iso: str) -> str:
    """A non-relevant log line that must NOT be counted."""
    return json.dumps({
        "0": '{"subsystem":"diagnostic"}',
        "1": "some other message",
        "2": "noise",
        "_meta": {"date": date_iso},
    }, separators=(",", ":"))


# ── parse_fallback_exhaustion_events ─────────────────────────────────────────


def test_parser_picks_only_chain_exhausted_model_not_found():
    """Pre-filter on substrings + JSON-validate the structure. Only
    lines whose event payload has BOTH ``fallbackStepFinalOutcome ==
    chain_exhausted`` AND ``reason == model_not_found`` are counted."""
    text = "\n".join([
        _exhausted_log_line(date_iso="2026-06-03T08:00:00.000Z"),
        _other_log_line("2026-06-03T08:01:00.000Z"),
        _exhausted_log_line(
            date_iso="2026-06-03T08:02:00.000Z",
            requested_model="claude-sonnet-4-6",
        ),
        "this line isn't even JSON",
        "{not valid json{",
    ])
    events = cost_watchdog.parse_fallback_exhaustion_events(text)
    assert len(events) == 2
    assert {e["requested_model"] for e in events} == {
        "claude-haiku-4-5", "claude-sonnet-4-6",
    }


def test_parser_applies_since_window():
    """Events older than the ``since`` cutoff must be dropped — the
    detector's rolling window is what makes the threshold meaningful."""
    now = datetime(2026, 6, 3, 8, 30, tzinfo=timezone.utc)
    text = "\n".join([
        _exhausted_log_line(date_iso="2026-06-03T07:00:00.000Z"),  # 90m ago
        _exhausted_log_line(date_iso="2026-06-03T08:15:00.000Z"),  # 15m ago
        _exhausted_log_line(date_iso="2026-06-03T08:25:00.000Z"),  # 5m ago
    ])
    events = cost_watchdog.parse_fallback_exhaustion_events(
        text, since=now - timedelta(minutes=30),
    )
    # 90m-ago dropped, two in-window survive.
    assert len(events) == 2


def test_parser_skips_events_without_chain_exhausted_outcome():
    """Lines that pass the substring pre-filter but whose parsed event
    payload says e.g. ``fallbackStepFinalOutcome == candidate_succeeded``
    must NOT be counted — they're successful retries, not exhausted
    chains."""
    succeeded = json.dumps({
        "0": "x",
        "1": {
            "event": "model_fallback_decision",
            "reason": "model_not_found",
            "fallbackStepFinalOutcome": "candidate_succeeded",
            # ↑ this branch — even though "chain_exhausted" appears
            # later in the line as a possible value, the payload
            # explicitly says success, so it doesn't count.
            "notes": "chain_exhausted is a possible outcome but not this one",
        },
        "_meta": {"date": "2026-06-03T08:00:00.000Z"},
    })
    events = cost_watchdog.parse_fallback_exhaustion_events(succeeded)
    assert events == []


def test_parser_skips_other_reasons():
    """A chain that exhausted because of e.g. rate-limit or 5xx is a
    different mode — different detector's job (embedding_monitor for
    embedding-side, future detector for runtime-side). Only the
    ``model_not_found`` cause fires this Signal."""
    rate_limited = _exhausted_log_line(
        date_iso="2026-06-03T08:00:00.000Z"
    ).replace('"reason":"model_not_found"', '"reason":"rate_limited"')
    events = cost_watchdog.parse_fallback_exhaustion_events(rate_limited)
    assert events == []


def test_parser_tolerates_missing_meta_date():
    """The OC log occasionally drops _meta.date on rotated/torn lines.
    Those events are kept with ts=None and pass the since filter only
    when no cutoff is set."""
    bad_ts = _exhausted_log_line(date_iso="2026-06-03T08:00:00.000Z")
    # Strip the date field entirely.
    bad_ts = bad_ts.replace(
        '"date":"2026-06-03T08:00:00.000Z"', '"unrelated":"x"',
    )
    # Without since: kept (ts=None).
    events = cost_watchdog.parse_fallback_exhaustion_events(bad_ts)
    assert len(events) == 1
    assert events[0]["ts"] is None
    # With since: dropped (can't prove it's in-window).
    events = cost_watchdog.parse_fallback_exhaustion_events(
        bad_ts, since=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    assert events == []


# ── detect_model_fallback_exhaustion ─────────────────────────────────────────


def _events(count: int, **kw) -> list[dict]:
    return [
        {
            "ts": datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc),
            "requested_provider": "anthropic",
            "requested_model": kw.get("model", "claude-haiku-4-5"),
            "from_model": "anthropic/claude-haiku-4-5",
            "error_preview": "Unknown model: anthropic/claude-haiku-4-5. ...",
        }
        for _ in range(count)
    ]


def test_detector_noop_below_threshold():
    """Under-threshold counts return no detections (the steady-state
    case where one stray exhaustion happens during a normal day)."""
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", _events(2), window_minutes=30, threshold=3,
    )
    assert out == []


def test_detector_noop_on_empty_events():
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", [], window_minutes=30, threshold=3,
    )
    assert out == []


def test_detector_noop_on_zero_threshold():
    """Defensive — operators may set threshold=0 to disable the
    detector entirely. That MUST return [] rather than firing on every
    event (which is the naive ``len(events) >= 0`` reading)."""
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", _events(5), window_minutes=30, threshold=0,
    )
    assert out == []


def test_detector_fires_alert_at_threshold():
    """At-threshold fires a single Signal with severity=alert (the
    Signal store's ``alert`` maps to the CRITICAL alert channel)."""
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", _events(3), window_minutes=30, threshold=3,
    )
    assert len(out) == 1
    sig = out[0]
    assert sig["severity"] == "alert"
    assert sig["type"] == "model_fallback_exhaustion"
    assert sig["bot_id"] == "personal-bot"
    assert sig["scope"] == "bot"
    assert sig["details"]["event_count"] == 3
    assert sig["details"]["threshold"] == 3
    assert sig["details"]["vector"] == "cost"


def test_detector_signature_is_per_bot_not_per_event():
    """One Signal per bot — successive cost_watchdog ticks update the
    same Signal as the count climbs. If the signature were per-event,
    every 5-minute tick would create a new Signal and the Alerts page
    would flood. Pin the signature shape."""
    sig_a = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", _events(3), window_minutes=30, threshold=3,
    )[0]
    sig_b = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", _events(7), window_minutes=30, threshold=3,
    )[0]
    assert sig_a["signature"] == sig_b["signature"]


def test_detector_lists_requested_models_in_body():
    """Operator should see WHICH models are dangling — that's how they
    know whether to redeploy (registry gap) or roll back a recent
    config change (model id typo)."""
    events = _events(2, model="claude-haiku-4-5") + _events(
        2, model="claude-sonnet-4-6"
    )
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", events, window_minutes=30, threshold=3,
    )
    sig = out[0]
    # Both models appear in details.requested_models AND in body text.
    assert sig["details"]["requested_models"] == [
        "claude-haiku-4-5", "claude-sonnet-4-6",
    ]
    assert "claude-haiku-4-5" in sig["body"]
    assert "claude-sonnet-4-6" in sig["body"]


def test_detector_fix_steps_point_to_deploy_and_breaker():
    """The operator's remediation has two prongs: redeploy (so the
    reconciler refills the registry) and breaker-trip (so the bleed
    stops while verification runs). Pin both in fix_steps so a future
    refactor doesn't silently drop the safety belt."""
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", _events(5), window_minutes=30, threshold=3,
    )
    fix = out[0]["details"]["fix_steps"]
    assert "evolve-admin deploy personal-bot" in fix
    assert "breaker trip personal-bot cost" in fix
    assert "kickstart" in fix  # gateway restart


def test_detector_truncates_long_requested_models_list():
    """A pathological bot with dozens of dangling slugs should still
    produce a one-line body preview, not a page of code."""
    events: list[dict] = []
    for i in range(10):
        events.append({
            "ts": datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc),
            "requested_provider": "anthropic",
            "requested_model": f"claude-model-{i}",
            "from_model": "x",
            "error_preview": "...",
        })
    out = cost_watchdog.detect_model_fallback_exhaustion(
        "personal-bot", events, window_minutes=30, threshold=3,
    )
    body = out[0]["body"]
    # 5 models shown by name; the rest summarized.
    assert body.count("claude-model-") == 5
    assert "+5 more" in body


def test_detector_is_listed_in_suppressible_types():
    """When the operator trips the cost breaker on a bot, the
    cost_watchdog runner must squelch this detector — piling CRITICALs
    on a tripped breaker is noise. The mapping is in
    ``_SUPPRESSIBLE_TYPES_TO_CATEGORY`` and breaks on rename, so the
    test pins the contract."""
    assert (
        cost_watchdog._SUPPRESSIBLE_TYPES_TO_CATEGORY[
            "model_fallback_exhaustion"
        ]
        == "cost"
    )


# ── DEFAULTS ─────────────────────────────────────────────────────────────────


def test_defaults_include_fallback_exhaustion_thresholds():
    """The runner's _thresholds_for_bot pulls these keys; missing them
    would crash collect_for_bot at runtime on every bot."""
    d = cost_watchdog.DEFAULTS
    assert d["fallback_exhaustion_window_minutes"] == 30
    assert d["fallback_exhaustion_threshold"] == 3
    assert d["fallback_exhaustion_log_tail_bytes"] == 512 * 1024
