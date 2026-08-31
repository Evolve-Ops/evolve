"""tests/test_evo_usage.py — `evo usage` handler."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for path in (str(_ADMIN_DIR),):
    if path not in sys.path:
        sys.path.insert(0, path)


def _write_rollup(shared: Path, bot: str, d: date, *,
                  event_count: int = 0, input_tokens: int = 0,
                  output_tokens: int = 0, cache_read: int = 0) -> None:
    out = shared / "metrics" / bot / f"cost-{d.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema_version": 1,
        "bot_id": bot,
        "date": d.isoformat(),
        "event_count": event_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "total_usd": 0.0,
    }))


def _call(tmp_path: Path, bot_id: str, network: dict | None = None):
    from evolve_admin.evo.handlers.usage import render
    network = network or {"sharedDir": str(tmp_path), "members": [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


def test_bot_with_no_data_returns_friendly_message(tmp_path):
    r = _call(tmp_path, "admin_bot")
    assert "No usage data yet" in (r.direct_send_message or "")


def test_bot_sums_calls_and_tokens(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today,
                  event_count=5, input_tokens=100, output_tokens=50, cache_read=200)
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=3),
                  event_count=10, input_tokens=500, output_tokens=100, cache_read=400)
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    # Today: 5 calls
    assert "Today: 5 calls" in body
    # 7d: 15 calls
    assert "7 days: 15 calls" in body


def test_bot_cache_hit_pct(tmp_path):
    today = date.today()
    # cache_read=80, input=20 → cache_hit = 80/(80+20) = 80%
    _write_rollup(tmp_path, "admin_bot", today,
                  event_count=1, input_tokens=20, cache_read=80)
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "80% cache hit" in body


def test_pod_aggregates_excluding_evolve(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today, event_count=3)
    _write_rollup(tmp_path, "team_bot_a", today, event_count=7)
    _write_rollup(tmp_path, "evolve", today, event_count=99)
    network = {"sharedDir": str(tmp_path),
               "members": ["admin_bot", "team_bot_a", "evolve"]}
    r = _call(tmp_path, "evolve", network=network)
    body = r.direct_send_message or ""
    assert "**Usage — pod**" in body
    assert "Today: 10 calls" in body  # 3 + 7, NOT including evolve's 99
    assert "By bot (30d):" in body
    assert body.index("team_bot_a: 7 calls") < body.index("admin_bot: 3 calls")


def test_dispatch_envelope(tmp_path):
    r = _call(tmp_path, "admin_bot")
    assert r.subcommand == "usage"
    assert r.mode == "speak"
    assert r.direct_send_message
