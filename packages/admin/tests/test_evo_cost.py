"""tests/test_evo_cost.py — `evo cost` handler.

Covers:
  * Per-bot rendering (1d/7d/30d windows)
  * Missing rollup files render gracefully
  * Top-model line appears when by_model data is present
  * Pod-wide rendering on the ``evolve`` bot (members loop, evolve excluded)
  * Pod-wide with zero data → no "By bot" section
"""

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


def _write_rollup(shared: Path, bot: str, d: date, total: float,
                  by_model: dict[str, float] | None = None) -> None:
    out = shared / "metrics" / bot / f"cost-{d.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "bot_id": bot,
        "date": d.isoformat(),
        "total_usd": total,
    }
    if by_model:
        body["by_model"] = {
            m: {"cost_usd": c} for m, c in by_model.items()
        }
    out.write_text(json.dumps(body))


def _call(tmp_path: Path, bot_id: str, network: dict | None = None):
    from evolve_admin.evo.handlers.cost import render
    network = network or {"sharedDir": str(tmp_path), "members": [bot_id]}
    return render(role="primary", bot_id=bot_id, args="", network=network)


# ─────────────────────────────────────────────────────────────────────────────
# Per-bot
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_with_no_data_returns_friendly_message(tmp_path):
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "No cost data recorded yet" in body
    assert "admin_bot" in body


def test_bot_sums_windows_correctly(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today, 0.10)
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=1), 1.00)
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=6), 5.00)
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=15), 10.00)
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=29), 20.00)

    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""

    # Today: just 0.10
    assert "Today: $0.10" in body
    # 7d: 0.10 + 1.00 + 5.00 = 6.10
    assert "7 days: $6.10" in body
    # 30d: 0.10 + 1.00 + 5.00 + 10.00 + 20.00 = 36.10
    assert "30 days: $36.10" in body


def test_bot_drops_data_beyond_30d(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today, 1.00)
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=45), 100.00)

    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "30 days: $1.00" in body


def test_bot_renders_top_model(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today, 5.00,
                  by_model={"claude-opus-4-7": 3.00, "claude-sonnet-4-6": 2.00})
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=5), 4.00,
                  by_model={"claude-opus-4-7": 4.00})

    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "Top model (30d): claude-opus-4-7 ($7.00)" in body


def test_bot_skips_top_model_when_empty(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today, 1.00)  # no by_model
    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "Top model" not in body


def test_bot_skips_malformed_rollup(tmp_path):
    today = date.today()
    bad = tmp_path / "metrics" / "admin_bot" / f"cost-{today.isoformat()}.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json")
    _write_rollup(tmp_path, "admin_bot", today - timedelta(days=1), 1.00)

    r = _call(tmp_path, "admin_bot")
    body = r.direct_send_message or ""
    assert "7 days: $1.00" in body  # the good file still sums


# ─────────────────────────────────────────────────────────────────────────────
# Pod-wide (evolve bot)
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_aggregates_across_members(tmp_path):
    today = date.today()
    _write_rollup(tmp_path, "admin_bot", today, 1.00)
    _write_rollup(tmp_path, "team_bot_a", today, 2.00)
    _write_rollup(tmp_path, "evolve", today, 99.00)  # should NOT be counted

    network = {
        "sharedDir": str(tmp_path),
        "members": ["admin_bot", "team_bot_a", "evolve"],
    }
    r = _call(tmp_path, "evolve", network=network)
    body = r.direct_send_message or ""

    assert "**Cost — pod**" in body
    assert "Today: $3.00" in body  # admin_bot + team_bot_a, NOT evolve itself
    assert "By bot (30d):" in body
    # By-bot lines sorted descending
    assert body.index("team_bot_a: $2.00") < body.index("admin_bot: $1.00")


def test_pod_with_no_active_bots_omits_breakdown(tmp_path):
    network = {"sharedDir": str(tmp_path), "members": ["admin_bot", "team_bot_a"]}
    r = _call(tmp_path, "evolve", network=network)
    body = r.direct_send_message or ""
    assert "**Cost — pod**" in body
    assert "Today: $0.00" in body
    assert "By bot" not in body  # nothing to break down


def test_pod_handles_missing_metrics_dir(tmp_path):
    network = {"sharedDir": str(tmp_path / "nonexistent"), "members": ["admin_bot"]}
    r = _call(tmp_path, "evolve", network=network)
    body = r.direct_send_message or ""
    assert "**Cost — pod**" in body


# ─────────────────────────────────────────────────────────────────────────────
# DispatchResult envelope shape
# ─────────────────────────────────────────────────────────────────────────────


def test_returns_speak_mode_dispatch_result(tmp_path):
    r = _call(tmp_path, "admin_bot")
    assert r.subcommand == "cost"
    assert r.mode == "speak"
    assert r.direct_send_message  # plugin can direct-send to Telegram
    assert "Respond ONLY with the following message" in (r.system_append or "")
