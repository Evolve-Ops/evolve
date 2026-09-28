"""Tests for /api/cascade/health.

Verifies the snapshot computation against synthesized on-disk
artifacts. The route handler itself is a thin wrapper around
``_compute_health``, so most tests exercise that directly and the
end-to-end Flask test is a single smoke.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
# packages/analyzer is needed because routes_cascade.py's
# _iter_recent_spans lazily imports observability.session_rollup
# from there. Without it, the lazy import raises ImportError and
# the route silently yields zero spans.
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.routes_cascade import (  # noqa: E402
    _compute_health,
    register_cascade_routes,
)


def _write_pressure_flags(shared: Path, heartbeat_iso: str) -> None:
    """Write the watchdog's output file with a chosen heartbeat
    timestamp. Other fields default to "no flags fired" — they're
    not what the tile cares about."""
    (shared / "cascade").mkdir(parents=True, exist_ok=True)
    (shared / "cascade" / "pressure_flags.json").write_text(json.dumps({
        "watchdog_heartbeat": heartbeat_iso,
        "pod_tier1_active_sessions": 0,
        "escalations_in_15min": 0,
    }))


def _write_span(
    shared: Path,
    day: str,
    attrs: dict,
    end_time: str | None = None,
    bot_id: str = "team_bot_a",
) -> None:
    """Append one span to the per-bot jsonl file with given attributes.

    Matches the path the plugin's CascadeTelemetry.ts writes to:
    {shared}/{bot_id}/spans/spans-{day}.jsonl. Earlier this wrote to
    `observability/spans/<day>.jsonl` and the route happened to read
    that path too — but on real pods the plugin writes per-bot, so
    the tests were validating a path layout that doesn't exist in
    production. The route now goes through iter_turn_spans which
    reads per-bot; tests follow the production layout.
    """
    spans_dir = shared / bot_id / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    span = {
        # OpikSpan.from_dict requires `name`, `start_time`, `end_time`.
        # `producer` must be "cascade_telemetry" so iter_turn_spans's
        # filter doesn't drop it. Match the plugin's actual schema.
        "name": "bot_session_turn",
        "producer": "cascade_telemetry",
        "bot_id": bot_id,
        "end_time": end_time or f"{day}T12:00:00+00:00",
        "start_time": end_time or f"{day}T12:00:00+00:00",
        "attributes": attrs,
    }
    with (spans_dir / f"spans-{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(span) + "\n")


def _write_label(shared: Path, day: str) -> None:
    labels_dir = shared / "cascade" / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    with (labels_dir / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"session_id":"s1","label":"correctly_held"}\n')


def _write_signal(shared: Path, producer: str, name: str) -> None:
    firing = shared / "signals" / "firing"
    firing.mkdir(parents=True, exist_ok=True)
    # Phase B: the count reads through signals.store.iter_active, which only
    # yields records that deserialize, so include the required Signal fields.
    (firing / f"{name}.json").write_text(json.dumps({
        "id": name, "producer": producer, "type": "anomaly_cost_per_turn",
        "signature": f"{producer}:anomaly_cost_per_turn:{name}",
        "flavor": "maintenance", "severity": "warn", "scope": "pod",
        "state": "firing",
    }))


# ── No-data path ────────────────────────────────────────────────────────────


def test_empty_pod_returns_no_data(tmp_path: Path):
    """Brand-new pod with no cascade artifacts → state="no_data",
    every metric is None or zero. No 500."""
    snap = _compute_health(tmp_path, now=datetime.now(timezone.utc))
    assert snap["state"] == "no_data"
    assert snap["watchdog"]["heartbeat_age_seconds"] is None
    assert "rate_pct" not in snap["disagreement"] or snap["disagreement"].get("rate_pct") is None
    assert snap["spans_today"] == 0
    assert snap["signals_firing"] == 0


# ── Heartbeat scenarios ─────────────────────────────────────────────────────


def test_fresh_heartbeat_with_recent_span_yields_ok(tmp_path: Path):
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    _write_span(
        tmp_path, now.date().isoformat(),
        attrs={"cascade.shadow_verdict.tier": "tier2", "cascade.trigger_kind": "user_turn"},
        end_time=now.isoformat(),
    )
    snap = _compute_health(tmp_path, now=now)
    assert snap["state"] == "ok"
    assert snap["watchdog"]["heartbeat_age_seconds"] == 0


def test_stale_heartbeat_yields_warn(tmp_path: Path):
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(seconds=200)).isoformat()
    _write_pressure_flags(tmp_path, stale)
    _write_span(
        tmp_path, now.date().isoformat(),
        attrs={"cascade.shadow_verdict.tier": "tier2", "cascade.trigger_kind": "user_turn"},
        end_time=now.isoformat(),
    )
    snap = _compute_health(tmp_path, now=now)
    assert snap["state"] == "warn"
    assert snap["watchdog"]["heartbeat_age_seconds"] >= 180


def test_dead_heartbeat_yields_alert(tmp_path: Path):
    now = datetime.now(timezone.utc)
    dead = (now - timedelta(minutes=20)).isoformat()
    _write_pressure_flags(tmp_path, dead)
    _write_span(
        tmp_path, now.date().isoformat(),
        attrs={"cascade.shadow_verdict.tier": "tier2", "cascade.trigger_kind": "user_turn"},
        end_time=now.isoformat(),
    )
    snap = _compute_health(tmp_path, now=now)
    assert snap["state"] == "alert"


def test_malformed_pressure_flags_treated_as_missing(tmp_path: Path):
    """Garbage in the watchdog file → treat as no heartbeat, not
    as state=alert. The watchdog could be in the middle of an atomic
    rewrite when we read it; we shouldn't paint the tile red on a
    transient read error."""
    (tmp_path / "cascade").mkdir(parents=True)
    (tmp_path / "cascade" / "pressure_flags.json").write_text("not valid json")
    snap = _compute_health(tmp_path, now=datetime.now(timezone.utc))
    assert snap["watchdog"]["heartbeat_age_seconds"] is None


# ── Disagreement-rate scenarios ─────────────────────────────────────────────


def test_disagreement_rate_computed_from_shadow_verdicts(tmp_path: Path):
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    # 4 spans with shadow verdict, 1 disagrees.
    for i, disagrees in enumerate([False, False, True, False]):
        _write_span(
            tmp_path, now.date().isoformat(),
            attrs={
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.shadow_verdict.disagrees": disagrees,
                "cascade.trigger_kind": "user_turn",
            },
            end_time=now.isoformat(),
        )
    snap = _compute_health(tmp_path, now=now)
    assert snap["disagreement"]["total"] == 4
    assert snap["disagreement"]["disagreeing"] == 1
    assert snap["disagreement"]["rate_pct"] == 25.0


def test_disagreement_rate_above_50_yields_alert(tmp_path: Path):
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    # 8 spans, 6 disagree → 75%, above alert threshold.
    for i in range(8):
        _write_span(
            tmp_path, now.date().isoformat(),
            attrs={
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.shadow_verdict.disagrees": i < 6,
                "cascade.trigger_kind": "user_turn",
            },
            end_time=now.isoformat(),
        )
    snap = _compute_health(tmp_path, now=now)
    assert snap["disagreement"]["rate_pct"] == 75.0
    assert snap["state"] == "alert"


def test_disagreement_ignores_spans_without_shadow_verdict(tmp_path: Path):
    """A span emitted before the controller was wired won't have
    cascade.shadow_verdict.tier — it must NOT count toward the
    denominator."""
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    # 3 spans WITHOUT shadow verdict (pre-PR-1652-style spans).
    for _ in range(3):
        _write_span(
            tmp_path, now.date().isoformat(),
            attrs={"cascade.trigger_kind": "user_turn"},
            end_time=now.isoformat(),
        )
    # 2 spans WITH shadow verdict, neither disagrees.
    for _ in range(2):
        _write_span(
            tmp_path, now.date().isoformat(),
            attrs={
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.trigger_kind": "user_turn",
            },
            end_time=now.isoformat(),
        )
    snap = _compute_health(tmp_path, now=now)
    assert snap["disagreement"]["total"] == 2
    assert snap["disagreement"]["disagreeing"] == 0
    assert snap["disagreement"]["rate_pct"] == 0.0


# ── Ask-hint rate scenarios ─────────────────────────────────────────────────


def test_ask_hint_rate_only_counts_user_facing(tmp_path: Path):
    """heartbeat / cron_app spans are NOT in the ask-hint denominator
    (the bot doesn't ask its user on background turns)."""
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    # 1 user_turn span where ask_hint emitted
    _write_span(
        tmp_path, now.date().isoformat(),
        attrs={
            "cascade.shadow_verdict.tier": "tier2",
            "cascade.trigger_kind": "user_turn",
            "cascade.shadow_verdict.ask_hint_emitted": True,
        },
        end_time=now.isoformat(),
    )
    # 9 heartbeat spans (none ask-hint) — denominator stays at 1
    for _ in range(9):
        _write_span(
            tmp_path, now.date().isoformat(),
            attrs={
                "cascade.shadow_verdict.tier": "tier3",
                "cascade.trigger_kind": "heartbeat",
            },
            end_time=now.isoformat(),
        )
    snap = _compute_health(tmp_path, now=now)
    assert snap["ask_hint"]["user_facing"] == 1
    assert snap["ask_hint"]["asked"] == 1
    assert snap["ask_hint"]["rate_pct"] == 100.0


def test_ask_hint_above_10pct_yields_alert(tmp_path: Path):
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    # 4 user-facing spans, 1 asked → 25% ask-hint rate, above alert.
    for i in range(4):
        _write_span(
            tmp_path, now.date().isoformat(),
            attrs={
                "cascade.shadow_verdict.tier": "tier2",
                "cascade.trigger_kind": "user_turn",
                "cascade.shadow_verdict.ask_hint_emitted": i == 0,
            },
            end_time=now.isoformat(),
        )
    snap = _compute_health(tmp_path, now=now)
    assert snap["ask_hint"]["rate_pct"] == 25.0
    assert snap["state"] == "alert"


# ── Labels + signals ─────────────────────────────────────────────────────────


def test_labels_count_today_and_yesterday(tmp_path: Path):
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()
    _write_label(tmp_path, today)
    _write_label(tmp_path, today)
    _write_label(tmp_path, yesterday)
    snap = _compute_health(tmp_path, now=now)
    assert snap["labels"] == {"today": 2, "yesterday": 1}


def test_signals_firing_counts_only_cascade_audit_producer(tmp_path: Path):
    """Signals from other producers must not inflate the count."""
    now = datetime.now(timezone.utc)
    _write_pressure_flags(tmp_path, now.isoformat())
    _write_signal(tmp_path, "cascade_audit", "sig-1")
    _write_signal(tmp_path, "cascade_audit", "sig-2")
    _write_signal(tmp_path, "alerts_loop_monitor", "sig-3")  # other producer
    snap = _compute_health(tmp_path, now=now)
    assert snap["signals_firing"] == 2


# ── End-to-end Flask route ──────────────────────────────────────────────────


def test_route_returns_json_shape(tmp_path: Path):
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"sharedDir": str(tmp_path / "shared")}))
    (tmp_path / "shared").mkdir()

    app = Flask(__name__)
    register_cascade_routes(app, network_path)
    client = app.test_client()
    resp = client.get("/api/cascade/health")
    assert resp.status_code == 200
    body = resp.get_json()
    # Confirm the keys the UI tile reads exist (contract pin).
    for key in ("state", "watchdog", "disagreement", "ask_hint", "labels", "signals_firing"):
        assert key in body, f"missing key {key!r} from /api/cascade/health"
    assert body["state"] == "no_data"
