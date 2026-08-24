"""Tests for the Value view (spec internal/spec-value-baseline-2026-06-10.md §7.2).

Covers two surfaces:

  * Backend — GET /api/better/value serves the latest nightly rollup
    ranked with the spec §9 key, with the §5.3 staleness flag; absence
    of a rollup reads as "not available", never as an empty table of
    zeros.
  * UI structural pins — the Improvements page carries the Value subtab,
    the subtab dispatch wires _loadValueView(), and the renderer maps
    utilization_state to the §7.3 plain-words labels without
    re-implementing the predicate.

Backend tests stand up a minimal Flask app with just the better routes
so they don't require real shared_dir or per-bot infrastructure.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = REPO_ROOT / "packages" / "admin" / "evolve_admin" / "web"
INDEX_HTML = _WEB_DIR / "index.html"
SELF_IMPROVEMENT_JS = (
    _WEB_DIR / "static" / "js" / "pages" / "self-improvement.js"
)


# ── fixtures ────────────────────────────────────────────────────────────────


def _entry(state: str, human: int | None, runs: int | None) -> dict:
    return {
        "utilization_state": state,
        "state_reason": "test reason",
        "active_human_days_7d": {"value": 1, "measurable_days": 7, "window_days": 7},
        "active_human_days_28d": {
            "value": human, "measurable_days": 27, "window_days": 28,
        },
        "proactive_runs_7d": {"value": 0, "measurable_days": 7, "window_days": 7},
        "proactive_runs_28d": {
            "value": runs, "measurable_days": 27, "window_days": 28,
        },
        "app_coverage_28d": {"value": 0.5, "apps_total": 2, "apps_used": 1},
        "value_trend_28d": {"value": None, "current": None, "prior": None},
        "usage_breadth_28d": {"value": 3},
        "age_days": 120,
        "anchor_date": "2026-06-09",
    }


@pytest.fixture
def value_app(tmp_path):
    """Minimal Flask app with the better routes + a tmp shared dir."""
    from evolve_admin.web.routes_better import register_better_routes

    shared_dir = tmp_path / "evolve"
    shared_dir.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "members": ["bot-a"],
        "sharedDir": str(shared_dir),
    }))
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_better_routes(app, network_path)
    return app, shared_dir


def _write_rollup(shared_dir: Path, bots: dict, *, anchor: str = "2026-06-09",
                  computed_at: str | None = None) -> None:
    value_dir = shared_dir / "metrics" / "value"
    value_dir.mkdir(parents=True, exist_ok=True)
    (value_dir / f"{anchor}.json").write_text(json.dumps({
        "version": 1,
        "computed_at": computed_at
        or datetime.now(timezone.utc).isoformat(),
        "anchor_date": anchor,
        "bots": bots,
    }))


# ── GET /api/better/value ───────────────────────────────────────────────────


def test_value_endpoint_unavailable_without_rollup(value_app):
    app, _shared = value_app
    resp = app.test_client().get("/api/better/value")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["bots"] == []


def test_value_endpoint_returns_ranked_bots(value_app):
    app, shared = value_app
    _write_rollup(shared, {
        "bot-idle": _entry("underused", 0, 0),
        "bot-daily": _entry("active", 20, 5),
        "bot-briefing": _entry("active", 0, 28),
        "bot-new": _entry("unmeasurable", None, None),
    })
    body = app.test_client().get("/api/better/value").get_json()
    assert body["ok"] is True and body["available"] is True
    assert body["anchor_date"] == "2026-06-09"
    assert body["stale"] is False
    # Spec §9 rank key: state (underused last), then human days desc,
    # then proactive runs desc — applied server-side.
    assert [b["bot_id"] for b in body["bots"]] == [
        "bot-daily", "bot-briefing", "bot-new", "bot-idle",
    ]
    # Entries carry the rollup fields the table renders.
    daily = body["bots"][0]
    assert daily["utilization_state"] == "active"
    assert daily["active_human_days_28d"]["value"] == 20
    assert daily["app_coverage_28d"]["apps_total"] == 2


def test_value_endpoint_flags_stale_rollup(value_app):
    app, shared = value_app
    old = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    _write_rollup(shared, {"bot-a": _entry("active", 5, 0)}, computed_at=old)
    body = app.test_client().get("/api/better/value").get_json()
    assert body["available"] is True
    assert body["stale"] is True


# ── UI structural pins ──────────────────────────────────────────────────────


def test_improvements_page_has_value_subtab():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "subTab(this,'self-improvement','value')" in html
    assert 'id="self-improvement-value"' in html
    assert 'id="si-value-table"' in html
    # Subtab activation dispatches the loader.
    assert "if (name === 'value') _loadValueView();" in html


def test_value_renderer_defined_and_wired():
    js = SELF_IMPROVEMENT_JS.read_text(encoding="utf-8")
    assert "async function _loadValueView()" in js
    assert "/api/better/value" in js
    # The renderer maps the three states to §7.3 plain-words labels —
    # it must read utilization_state, never re-derive it from metrics.
    assert "'In use'" in js or '"In use"' in js
    assert "Idle 4 weeks" in js
    assert "Not enough data" in js
    # §7.3 reference copy for the can't-tell state.
    assert "Not enough data to tell —" in js


def test_value_view_visible_copy_passes_plex_test():
    """Spec §7.3: no internal vocabulary on the operator surface. Checks
    the Value subtab-page markup (visible copy lives there; the HTML
    comment above the div legitimately cites spec terms, so the slice
    starts after the div opens)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('id="self-improvement-value"')
    end = html.index('id="si-value-table"', start)
    visible = html[start:end].lower()
    for banned in ("baseline", "signal", "producer", "tri-state", "measurable"):
        assert banned not in visible, f"Value view copy leaks {banned!r}"
