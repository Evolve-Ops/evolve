"""tests/test_producer_severity_retrofits_wave2.py

Severity-framework (vector, magnitude) tags on the wave-2 producer
retrofits: session_economics, bot_log_signal, alerts_loop_monitor,
deploy_drift_monitor, bot_recovery_monitor.

Spec: internal/spec-severity-framework-2026-05-18.md §2.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import alerts_loop_monitor as alm  # noqa: E402
import bot_log_signal as bls  # noqa: E402
import bot_recovery_monitor as brm  # noqa: E402
import deploy_drift_monitor as ddm  # noqa: E402
import session_economics as se  # noqa: E402
import severity as sev  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# session_economics
# ─────────────────────────────────────────────────────────────────────────────


def _cache_evt(*, cache_state="warm", cache_read=0, cache_write=0, input_tokens=100) -> dict:
    return {
        "schema_version": 1,
        "type": "cost_event",
        "ts": "2026-05-18T10:00:00Z",
        "bot_id": "team_bot_a",
        "session_id": "s1",
        "trigger_kind": "user_turn",
        "model": "claude-sonnet",
        "provider": "anthropic",
        "cache_state": cache_state,
        "input_tokens": input_tokens,
        "output_tokens": 50,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": 0.01,
    }


def test_cache_invalidation_magnitude_scales_with_ratio():
    # 80% invalidated → mag 3
    events = [_cache_evt(cache_state="invalidated", cache_write=100) for _ in range(8)] + \
             [_cache_evt(cache_state="warm", cache_read=100) for _ in range(2)]
    out = se.detect_cache_invalidation_elevated(
        "team_bot_a", events, threshold_ratio=0.4, min_events=5, window_days=7,
    )
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 3


def test_cache_invalidation_magnitude_1_at_threshold():
    # 30% invalidated, threshold 0.2 → fires at mag 1
    events = [_cache_evt(cache_state="invalidated", cache_write=100) for _ in range(3)] + \
             [_cache_evt(cache_state="warm", cache_read=100) for _ in range(7)]
    out = se.detect_cache_invalidation_elevated(
        "team_bot_a", events, threshold_ratio=0.2, min_events=5, window_days=7,
    )
    assert len(out) == 1
    assert out[0]["details"]["magnitude"] == 1


def test_cache_hit_rate_low_magnitude_scales_with_rate():
    # Very low hit rate (writes dominate) — mag 2
    events = [_cache_evt(cache_state="invalidated", cache_write=900, input_tokens=100) for _ in range(10)]
    out = se.detect_cache_hit_rate_low(
        "team_bot_a", events, threshold_ratio=0.5, min_events=5, window_days=7,
    )
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "cost"
    assert out[0]["details"]["magnitude"] == 2


def test_bot_unused_info_tier_quality_vector():
    events = [
        {
            "schema_version": 1, "type": "cost_event",
            "ts": "2026-05-18T10:00:00Z", "bot_id": "team_bot_a",
            "session_id": "s1", "trigger_kind": "heartbeat",
            "model": "claude-sonnet", "provider": "anthropic",
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.01,
        }
    ]
    out = se.detect_bot_unused(
        "team_bot_a", events, unused_days=14, now=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "quality"
    assert out[0]["details"]["magnitude"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# bot_log_signal
# ─────────────────────────────────────────────────────────────────────────────


def _write_status_file(shared_dir: Path, bot_id: str, recent_errors: list[str]):
    p = shared_dir / "status" / f"{bot_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"bot_id": bot_id, "recent_errors": recent_errors}))


def test_bot_log_max_auth_failure_is_cost_vector(tmp_path):
    _write_status_file(tmp_path, "team_bot_a", ["MAX auth failed; falling back"])
    bls.emit_bot_log_signals(tmp_path, ["team_bot_a"])
    sigs = list(signals_store.iter_active(tmp_path, producer="bot_log_monitor"))
    assert len(sigs) == 1
    assert sigs[0].type == "max_auth_failure"
    assert sigs[0].details["vector"] == "cost"
    assert sigs[0].details["magnitude"] == 2
    assert sigs[0].details["severity_active"] is True


def test_bot_log_discord_target_invalid_is_operations(tmp_path):
    _write_status_file(tmp_path, "team_bot_a", ["Discord error: Unknown Channel xyz"])
    bls.emit_bot_log_signals(tmp_path, ["team_bot_a"])
    sigs = list(signals_store.iter_active(tmp_path, producer="bot_log_monitor"))
    assert len(sigs) == 1
    assert sigs[0].type == "discord_target_invalid"
    assert sigs[0].details["vector"] == "operations"
    assert sigs[0].details["magnitude"] == 2


def test_bot_log_tool_delivery_failing_is_operations(tmp_path):
    _write_status_file(tmp_path, "team_bot_a", [
        "[tools] message failed once",
        "[tools] message failed twice",
    ])
    bls.emit_bot_log_signals(tmp_path, ["team_bot_a"])
    sigs = list(signals_store.iter_active(tmp_path, producer="bot_log_monitor"))
    assert len(sigs) == 1
    assert sigs[0].type == "tool_delivery_failing"
    assert sigs[0].details["vector"] == "operations"
    assert sigs[0].details["magnitude"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# alerts_loop_monitor
# ─────────────────────────────────────────────────────────────────────────────


_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


def _rec(**kw) -> dict:
    ts = (_NOW - timedelta(minutes=kw.get("minutes_ago", 0))).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return {
        "ts": ts,
        "source": kw.get("source", "heal"),
        "result": kw.get("result", "sent"),
        "channel": "telegram",
        "message_excerpt": kw.get("message_excerpt", "Config drift on evolve"),
        "error": kw.get("error"),
    }


def test_alerts_loop_dispatcher_failures_operations_2():
    recs = [_rec(result="failed", source="update_watcher", error="timeout") for _ in range(4)]
    out = alm.detect_dispatcher_failures(recs)
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "operations"
    assert out[0]["details"]["magnitude"] == 2
    assert out[0]["details"]["severity_active"] is True


def test_alerts_loop_dispatcher_failures_magnitude_3_at_alert_threshold():
    recs = [_rec(result="failed", source="update_watcher", error="timeout") for _ in range(12)]
    out = alm.detect_dispatcher_failures(recs)
    assert out[0]["details"]["magnitude"] == 3


def test_alerts_loop_repeat_magnitude_1_small_loop():
    recs = [_rec(source="heal") for _ in range(6)]
    out = alm.detect_alert_repeat_loop(recs)
    assert len(out) == 1
    assert out[0]["details"]["vector"] == "operations"
    assert out[0]["details"]["magnitude"] == 1


def test_alerts_loop_repeat_magnitude_2_big_loop():
    recs = [_rec(source="heal") for _ in range(12)]
    out = alm.detect_alert_repeat_loop(recs)
    assert out[0]["details"]["magnitude"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# deploy_drift_monitor
# ─────────────────────────────────────────────────────────────────────────────


def _net(members=("team_bot_a", "team_bot_b", "admin_bot")):
    return {"members": list(members), "bots": {m: {"role": "member"} for m in members}}


def _install(versions):
    return {"bot_versions": {b: {"version": v} for b, v in versions.items()}}


def test_deploy_drift_magnitude_1_when_1_bot_out():
    spec = ddm.detect_deploy_drift(_net(), _install({"team_bot_a": "v1", "team_bot_b": "v2", "admin_bot": "v2"}), "v2")
    assert spec is not None
    assert spec["details"]["vector"] == "operations"
    assert spec["details"]["magnitude"] == 1


def test_deploy_drift_magnitude_2_when_3_plus_bots_out():
    spec = ddm.detect_deploy_drift(_net(), _install({"team_bot_a": "v1", "team_bot_b": "v1", "admin_bot": "v1"}), "v2")
    assert spec["details"]["magnitude"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# bot_recovery_monitor
# ─────────────────────────────────────────────────────────────────────────────


def test_recovery_signal_is_info_tier_operations_magnitude_0():
    entry = {
        "provider": "anthropic",
        "condition": "context_overflow",
        "error_ts": "2026-05-18T01:30:00Z",
        "recovered_ts": "2026-05-18T02:00:00Z",
        "error_line": "[gateway] context too long",
    }
    spec = brm.build_signal_for_recovery("team_bot_a", entry)
    assert spec is not None
    assert spec["severity"] == "info"
    assert spec["details"]["vector"] == "operations"
    assert spec["details"]["magnitude"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Resolver end-to-end — explicit tag wins, priority bucket sensible
# ─────────────────────────────────────────────────────────────────────────────


def test_resolver_reads_session_economics_explicit_tag(tmp_path):
    events = [_cache_evt(cache_state="invalidated", cache_write=100) for _ in range(8)] + \
             [_cache_evt(cache_state="warm", cache_read=100) for _ in range(2)]
    specs = se.detect_cache_invalidation_elevated(
        "team_bot_a", events, threshold_ratio=0.4, min_events=5, window_days=7,
    )
    rating = sev.resolve_severity(specs[0])
    assert rating.vector == "cost"
    assert rating.magnitude == 3


def test_resolver_reads_bot_log_max_auth_as_cost(tmp_path):
    _write_status_file(tmp_path, "team_bot_a", ["fell back to metered billing"])
    bls.emit_bot_log_signals(tmp_path, ["team_bot_a"])
    sigs = list(signals_store.iter_active(tmp_path, producer="bot_log_monitor"))
    rating = sev.resolve_severity(sigs[0].to_dict())
    assert rating.vector == "cost"
    assert rating.magnitude == 2


def test_alerts_loop_failures_pod_wide_active_clears_in_narrative_bucket():
    """A 12-failure dispatcher cluster (mag 3, pod-wide, active outage) should
    clear at least the in_narrative bucket so operators see it on Home."""
    recs = [_rec(result="failed", source="update_watcher", error="timeout") for _ in range(12)]
    spec = alm.detect_dispatcher_failures(recs)[0]
    rating = sev.resolve_severity(spec)
    score = sev.compose_priority(rating, scope="pod", is_active_outage=True)
    bucket = sev.priority_bucket(score)
    assert bucket in ("lead", "in_narrative")


def test_bot_recovery_info_lands_in_small_bucket():
    """Info-tier recovery never crowds the narrative — magnitude 0
    composes to priority 0.0 regardless of pod weight."""
    entry = {
        "provider": "anthropic",
        "condition": "context_overflow",
        "error_ts": "2026-05-18T01:30:00Z",
        "recovered_ts": "2026-05-18T02:00:00Z",
        "error_line": "[gateway] context too long",
    }
    spec = brm.build_signal_for_recovery("team_bot_a", entry)
    rating = sev.resolve_severity(spec)
    score = sev.compose_priority(rating, scope="bot")
    assert sev.priority_bucket(score) == "small"
