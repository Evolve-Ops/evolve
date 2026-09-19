"""GET/PATCH /api/bot/openclaw-cost — the ``auto`` cache tier on the Cost page.

``auto`` is the only cost setting whose STORED value does not state its
effect: it resolves to 5m or 1h at deploy time from the bot's own inter-turn
gaps. A knob whose effect the operator cannot read is a knob they cannot
trust, so the resolution ships beside the setting in the same response —
these pin that, and that the write path accepts the value at all.
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
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import routes_cost_measures  # noqa: E402
from evolve_admin.web.routes_cost_measures import (  # noqa: E402
    register_cost_measures_routes,
)

DAY = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _sparse_turns(n: int = 12, gap_minutes: int = 18, days: int = 7) -> list[dict]:
    """The finding's shape, one session a day for a week.

    ``auto`` will not move a tier on less than ``MIN_WINDOW_DAYS_FOR_AUTO``
    days of OBSERVED history, and the span comes out of the records — so a
    fixture that expects a decision has to cover real days.
    """
    rows = []
    for d in range(days):
        for i in range(n):
            rows.append({
                "session_id": f"s{d}",
                "ts": (
                    DAY + timedelta(days=d, minutes=gap_minutes * i)
                ).isoformat(),
                "cache_write_tokens": 45_000,
                "cache_read_tokens": 0,
            })
    return rows


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "evolve",
        "members": ["admin_bot"],
    }))
    # cost_profiles reads the bot's openclaw.json off disk; stub it so these
    # tests are about the cache tier and not about deploying a bot.
    import cost_profiles
    monkeypatch.setattr(
        cost_profiles, "read_openclaw_cost_settings",
        lambda bot_id, **kw: {"heartbeat": {}, "contextPruning": {}},
    )
    monkeypatch.setattr(
        cost_profiles, "write_openclaw_cost_settings",
        lambda bot_id, body, force=False: (True, None),
    )
    monkeypatch.setattr(
        cost_profiles, "save_cost_snapshot", lambda *a, **kw: None,
    )
    # The resolution is memoized per (bot, value, UTC hour) so a page load
    # and its save do not each re-parse a week of turn logs. Tests share a
    # bot id, so start each one from an empty memo.
    routes_cost_measures._RESOLUTION_MEMO.clear()
    a = Flask(__name__)
    register_cost_measures_routes(a, network_path)
    a.config["TESTING"] = True
    a.shared_dir = shared
    return a


def _write_be(shared: Path, bot_id: str, retention: str | None) -> None:
    budget = {} if retention is None else {"per_bot_cache_retention": retention}
    (shared / "better-engine-config.json").write_text(json.dumps({
        "schema_version": 1, "pod_defaults": {},
        "bots": {bot_id: {"budget": budget}},
    }))


def test_auto_reports_what_it_resolves_to(app, monkeypatch):
    import cache_shape
    monkeypatch.setattr(
        cache_shape, "load_turn_records", lambda bot_id, **kw: _sparse_turns(),
    )
    _write_be(app.shared_dir, "admin_bot", "auto")
    with app.test_client() as c:
        body = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
    settings = body["settings"]
    assert settings["cache_retention"] == "auto"
    resolved = settings["cache_retention_resolved"]
    assert resolved["retention"] == "long"
    assert resolved["label"] == "1h"
    assert "re-write factor" in resolved["reason"]
    assert resolved["metrics"]["writes_under_short_cache"] > (
        resolved["metrics"]["writes_under_long_cache"]
    )


def test_auto_that_declines_says_so(app, monkeypatch):
    import cache_shape
    monkeypatch.setattr(
        cache_shape, "load_turn_records",
        lambda bot_id, **kw: _sparse_turns(n=2, days=2),
    )
    _write_be(app.shared_dir, "admin_bot", "auto")
    with app.test_client() as c:
        body = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
    resolved = body["settings"]["cache_retention_resolved"]
    assert resolved["retention"] is None
    assert "declined" in resolved["reason"]


def test_an_explicit_tier_resolves_to_itself_without_reading_turns(app, monkeypatch):
    """No turn read at all for a pinned tier — the resolution is the pin."""
    import cache_shape

    def _never(bot_id, **kw):
        raise AssertionError("turns must not be read for an explicit pin")

    monkeypatch.setattr(cache_shape, "load_turn_records", _never)
    _write_be(app.shared_dir, "admin_bot", "long")
    with app.test_client() as c:
        body = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
    resolved = body["settings"]["cache_retention_resolved"]
    assert resolved["retention"] == "long"
    assert "pinned" in resolved["reason"]


def test_unset_resolves_to_the_oc_default(app):
    _write_be(app.shared_dir, "admin_bot", None)
    with app.test_client() as c:
        body = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
    resolved = body["settings"]["cache_retention_resolved"]
    assert resolved["retention"] is None
    assert "OpenClaw's 5m default" in resolved["reason"]


def test_patch_accepts_auto_and_echoes_the_resolution(app, monkeypatch):
    import cache_shape
    monkeypatch.setattr(
        cache_shape, "load_turn_records", lambda bot_id, **kw: _sparse_turns(),
    )
    _write_be(app.shared_dir, "admin_bot", None)
    with app.test_client() as c:
        resp = c.patch(
            "/api/bot/openclaw-cost?bot=admin_bot",
            json={"cache_retention": "auto"},
        )
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True
    assert body["settings"]["cache_retention"] == "auto"
    assert body["settings"]["cache_retention_resolved"]["retention"] == "long"
    # And it landed in BE config, not openclaw.json.
    on_disk = json.loads(
        (app.shared_dir / "better-engine-config.json").read_text()
    )
    assert (
        on_disk["bots"]["admin_bot"]["budget"]["per_bot_cache_retention"] == "auto"
    )


def test_unreadable_turns_read_as_a_decline_not_a_choice(app, monkeypatch):
    """The on-disk failure mode: ``load_turns`` raises, ``load_turn_records``
    absorbs it into ``None``, and the resolver declines with a reason. "I
    could not look" must never render as a measured tier."""
    import usage_analytics

    def _boom(*a, **kw):
        raise OSError("turns dir vanished")

    monkeypatch.setattr(usage_analytics, "load_turns", _boom)
    _write_be(app.shared_dir, "admin_bot", "auto")
    with app.test_client() as c:
        resp = c.get("/api/bot/openclaw-cost?bot=admin_bot")
    assert resp.status_code == 200
    resolved = resp.get_json()["settings"]["cache_retention_resolved"]
    assert resolved["retention"] is None
    assert "could not measure" in resolved["reason"]


def test_an_unexpected_resolver_fault_does_not_500_the_settings_page(
    app, monkeypatch,
):
    """A settings page that fails because a cache measurement blew up is
    worse than one that shows a little less. The field goes null; the page
    renders the raw setting alone rather than an invented resolution."""
    import cache_shape

    def _boom(bot_id, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cache_shape, "load_turn_records", _boom)
    _write_be(app.shared_dir, "admin_bot", "auto")
    with app.test_client() as c:
        resp = c.get("/api/bot/openclaw-cost?bot=admin_bot")
    assert resp.status_code == 200
    settings = resp.get_json()["settings"]
    assert settings["cache_retention"] == "auto"
    assert settings["cache_retention_resolved"] is None


def test_the_resolution_is_measured_once_per_hour_not_once_per_request(
    app, monkeypatch,
):
    """Finding 6: resolving ``auto`` parses a week of turn JSONL three times
    over, and the settings endpoint did it on every GET **and** every PATCH.
    On a busy bot that is tens of thousands of rows between the click and the
    page. The memo is keyed by (bot, value, UTC hour), so a page load and the
    save that follows it share one measurement."""
    import cache_shape

    reads: list[str] = []

    def _counted(bot_id, **kw):
        reads.append(bot_id)
        return _sparse_turns()

    monkeypatch.setattr(cache_shape, "load_turn_records", _counted)
    _write_be(app.shared_dir, "admin_bot", "auto")
    with app.test_client() as c:
        first = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
        second = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
        third = c.patch(
            "/api/bot/openclaw-cost?bot=admin_bot",
            json={"cache_retention": "auto"},
        ).get_json()
    assert len(reads) == 1, f"turn logs re-parsed {len(reads)} times"
    resolved = first["settings"]["cache_retention_resolved"]
    assert resolved["retention"] == "long"
    assert second["settings"]["cache_retention_resolved"] == resolved
    assert third["settings"]["cache_retention_resolved"] == resolved


def test_a_different_bot_is_measured_separately(app, monkeypatch):
    """The memo is per bot — one bot's answer must never be served for another."""
    import cache_shape

    seen: list[str] = []

    def _per_bot(bot_id, **kw):
        seen.append(bot_id)
        return _sparse_turns() if bot_id == "admin_bot" else _sparse_turns(n=2, days=2)

    monkeypatch.setattr(cache_shape, "load_turn_records", _per_bot)
    _write_be(app.shared_dir, "admin_bot", "auto")
    with app.test_client() as c:
        a = c.get("/api/bot/openclaw-cost?bot=admin_bot").get_json()
    _write_be(app.shared_dir, "other_bot", "auto")
    with app.test_client() as c:
        b = c.get("/api/bot/openclaw-cost?bot=other_bot").get_json()
    assert seen == ["admin_bot", "other_bot"]
    assert a["settings"]["cache_retention_resolved"]["retention"] == "long"
    assert b["settings"]["cache_retention_resolved"]["retention"] is None
