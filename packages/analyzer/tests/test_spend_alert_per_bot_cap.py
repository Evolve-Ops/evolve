"""Tests for spend_alert's per-bot daily-cap auto-trip path.

The 2026-05-20 incident left the pod with a global ``dailySpendCapUsd``
threshold and per-action mitigations (downgrade-tier / pause-crons / etc).
The new path adds per-bot ``bots.{bot}.daily_cap_usd`` that auto-trips
the L1 cost breaker on cross — heartbeat-disable enforcement runs
synchronously and the next ticks dedup on the breaker file's existence.

These tests exercise the wiring end-to-end with mocked dispatcher +
mocked breaker enforce so the daemon never shells out:

  * _resolve_per_bot_cap honors the bots.{bot}.daily_cap_usd key
  * cap cross triggers breakers.store.trip + breakers_enforce.enforce_trip
  * already-tripped breaker dedups a subsequent tick
  * Telegram event hits the catalog
  * cap below spend → no trip
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spend_alert  # noqa: E402


# ── _resolve_per_bot_cap (BE-config-only, post-Phase 4) ─────────────────────


def _write_be(tmp_path, **bot_budget):
    payload = {
        "schema_version": 1,
        "pod_defaults": {},
        "bots": {"team_bot_a": {"budget": bot_budget}},
    }
    (tmp_path / "better-engine-config.json").write_text(json.dumps(payload))


def test_per_bot_cap_falls_back_to_compiled_default_with_no_config_file(tmp_path):
    """No BE config file → ``load()`` returns the compiled product defaults and
    the L1 rung inherits the $5 pod default from them.

    Previously this resolved to None (no enforcement) while the UI reader read
    the same compiled defaults and displayed $5 — the divergence that let bots
    run uncapped. See test_cost_cap_reader_parity.py.
    """
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) == 5.00


def test_per_bot_cap_returns_none_when_be_budget_empty(tmp_path):
    """A stored config with empty pod_defaults + empty per-bot budget is a
    genuine "no cap" — the fallback stops at what's stored."""
    _write_be(tmp_path)
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) is None


def test_per_bot_cap_returns_float_when_be_config_set(tmp_path):
    _write_be(tmp_path, per_bot_daily_hard_usd=5.0)
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) == 5.0


def test_per_bot_cap_accepts_int_value(tmp_path):
    _write_be(tmp_path, per_bot_daily_hard_usd=20)
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) == 20.0


def test_per_bot_cap_returns_none_for_zero_or_negative(tmp_path):
    _write_be(tmp_path, per_bot_daily_hard_usd=0)
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) is None
    _write_be(tmp_path, per_bot_daily_hard_usd=-1)
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) is None


def test_per_bot_cap_returns_none_for_unparseable(tmp_path):
    _write_be(tmp_path, per_bot_daily_hard_usd="not-a-number")
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}, tmp_path) is None


def test_per_bot_cap_ignores_legacy_network_json_value(tmp_path):
    """Phase 4 removed the network.json::daily_cap_usd fallback. A value
    sitting in bots_cfg is silently ignored — operators must use BE config."""
    bots_cfg = {"team_bot_a": {"daily_cap_usd": 5.0}}
    # Stored BE config with nothing set anywhere → None, regardless of the
    # legacy value. (Written explicitly so the compiled-defaults layer isn't
    # what supplies the answer — this test is about the legacy key.)
    _write_be(tmp_path)
    assert spend_alert._resolve_per_bot_cap("team_bot_a", bots_cfg, tmp_path) is None


def test_per_bot_cap_no_shared_dir_returns_none():
    """Without shared_dir, BE config can't be consulted → no cap."""
    assert spend_alert._resolve_per_bot_cap("team_bot_a", {}) is None
    assert (
        spend_alert._resolve_per_bot_cap(
            "team_bot_a", {"team_bot_a": {"daily_cap_usd": 5.0}}
        )
        is None
    )


# ── _trip_breaker_for_cost_cap (auto-trip path) ─────────────────────────────


@pytest.fixture
def fake_shared(tmp_path: Path) -> Path:
    shared = tmp_path / "shared"
    shared.mkdir()
    return shared


@pytest.fixture
def fake_network() -> dict:
    return {
        "primary": "team_bot_a",
        "members": ["security_bot"],
        "bots": {
            "team_bot_a": {"user": "team_bot_a"},
            "security_bot": {"user": "security_bot", "daily_cap_usd": 5.0},
        },
    }


def test_trip_breaker_for_cost_cap_writes_breaker(
    fake_shared, fake_network, monkeypatch,
):
    """Calling the helper writes a breaker record + dispatches Telegram."""
    # Mock dispatcher so we capture calls but don't shell out
    dispatched: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)

    # Mock the enforce path — it imports the writer which would try to
    # /Users/security_bot on the real FS. Stub it out.
    fake_enforce = MagicMock()
    fake_enforce.no_op = False
    fake_enforce.ok = True
    import evolve_admin
    if not hasattr(evolve_admin, "breakers_enforce"):
        from evolve_admin import breakers_enforce as _real_be  # noqa: F401
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(return_value=fake_enforce)
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)
    enforce_mod = fake_be

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=5.50, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-23",
    )

    # Breaker file exists
    breaker_file = fake_shared / "breakers" / "security_bot" / "cost.json"
    assert breaker_file.exists()
    rec = json.loads(breaker_file.read_text())
    assert rec["bot_id"] == "security_bot"
    assert rec["type"] == "cost"
    assert rec["initiated_by"] == "auto:spend_alert"
    assert "per-bot daily cap exceeded" in rec["reason"]

    # Fix B (2026-05-28): dispatcher now uses cost.breaker_tripped (the
    # ⚡ + bold "BREAKER TRIPPED" event) for the auto-trip path instead
    # of cost.hard_cap_hit, so the breaker IS the operator-visible lead
    # rather than buried as an action_label sub-line.
    assert len(dispatched) == 1
    assert dispatched[0]["catalog_event"] == "cost.breaker_tripped"
    payload = dispatched[0]["payload"]
    assert payload["bot_id"] == "security_bot"
    assert payload["breaker_type"] == "cost"
    # The reason carries the spend/cap context the operator needs
    assert "$5.50" in payload["reason"]
    assert "$5.00" in payload["reason"]
    # trip_id_short is the first 8 chars of the breaker record's trip_id
    assert payload["trip_id_short"] == rec["trip_id"][:8]
    assert isinstance(payload["duration_hours"], int)
    assert dispatched[0]["severity_name"] == "critical"

    # enforce_trip was called with shared_dir
    enforce_mod.enforce_trip.assert_called_once()
    call_kwargs = enforce_mod.enforce_trip.call_args.kwargs
    assert call_kwargs["scope"] == "security_bot"
    assert call_kwargs["breaker_type"] == "cost"
    assert call_kwargs["shared_dir"] == fake_shared


def test_trip_breaker_dedups_when_already_tripped(
    fake_shared, fake_network, monkeypatch,
):
    """Second call with breaker already tripped is a no-op (no second dispatch)."""
    dispatched: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)

    fake_enforce = MagicMock()
    fake_enforce.no_op = False
    import evolve_admin
    if not hasattr(evolve_admin, "breakers_enforce"):
        from evolve_admin import breakers_enforce as _real_be  # noqa: F401
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(return_value=fake_enforce)
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)
    enforce_mod = fake_be

    # First call → trips
    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=5.50, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-23",
    )
    # Second call → must not trip again, must not redispatch
    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=8.00, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-23",
    )

    assert len(dispatched) == 1, "expected exactly one dispatch on first trip"
    enforce_mod.enforce_trip.assert_called_once()


def test_trip_breaker_no_op_label_when_no_heartbeat(
    fake_shared, fake_network, monkeypatch,
):
    """If enforcement reports no_op (no heartbeat to disable), action_label
    must reflect that — operator shouldn't see "heartbeat disabled" when
    nothing was actually disabled."""
    dispatched: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)

    fake_enforce = MagicMock()
    fake_enforce.no_op = True
    fake_enforce.ok = True
    import evolve_admin
    if not hasattr(evolve_admin, "breakers_enforce"):
        from evolve_admin import breakers_enforce as _real_be  # noqa: F401
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(return_value=fake_enforce)
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=5.50, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-23",
    )

    # Fix B: reason field now carries the no-op note instead of action_label
    assert "No heartbeat scheduled" in dispatched[0]["payload"]["reason"]


def test_trip_breaker_action_label_when_enforce_raises(
    fake_shared, fake_network, monkeypatch,
):
    """If enforce_trip raises, the Telegram action_label must NOT lie
    about heartbeat being disabled. The 2026-05-20 incident's defining
    failure mode was "daemon says everything is fine while broken" —
    perpetuating that here in the very PR meant to bound it would be
    its own incident."""
    dispatched: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)

    import evolve_admin
    if not hasattr(evolve_admin, "breakers_enforce"):
        from evolve_admin import breakers_enforce as _real_be  # noqa: F401
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(side_effect=RuntimeError("simulated failure"))
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=5.50, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-23",
    )

    # Breaker file MUST still be written (intent is recorded so the
    # operator can inspect via CLI)
    breaker_file = fake_shared / "breakers" / "security_bot" / "cost.json"
    assert breaker_file.exists()
    # But the operator-facing reason must NOT claim heartbeat disabled.
    # Fix B: action_label is gone — the reason field now carries the
    # honest enforcement state, surfaced via cost.breaker_tripped.
    reason = dispatched[0]["payload"]["reason"]
    assert "FAILED" in reason
    assert "heartbeat disabled" not in reason, (
        f"Reason must not claim heartbeat disabled when enforce raised, "
        f"got: {reason!r}"
    )


def test_trip_breaker_payload_renders_through_catalog(
    fake_shared, fake_network, monkeypatch,
):
    """End-to-end: spend_alert builds the payload, the cost.breaker_tripped
    catalog event renders it into a message starting with ⚡ +
    "<b>{bot} breaker TRIPPED</b>" — that's the operator-visible lead
    that Fix B replaces the buried-action-label shape with.
    """
    dispatched: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)
    fake_enforce = MagicMock()
    fake_enforce.no_op = False
    fake_enforce.ok = True
    import evolve_admin
    if not hasattr(evolve_admin, "breakers_enforce"):
        from evolve_admin import breakers_enforce as _real_be  # noqa: F401
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(return_value=fake_enforce)
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=5.54, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-28",
    )

    assert len(dispatched) == 1
    payload = dispatched[0]["payload"]
    # Compose the rendered message via the catalog renderer — the
    # same path the dispatcher takes in production.
    from evolve_admin.alerts.catalog import by_key, render_event
    ev = by_key("cost.breaker_tripped")
    rendered = render_event(ev, payload)
    # ⚡ + bold "BREAKER TRIPPED" leads the message
    assert rendered.startswith("⚡ <b>security_bot breaker TRIPPED</b>")
    # Spend/cap info present in the body (carried via the reason field)
    assert "$5.54" in rendered
    assert "$5.00" in rendered
    # Catalog footer present
    assert "subscription: cost.breaker_tripped" in rendered


def test_trip_breaker_action_label_when_enforce_not_ok(
    fake_shared, fake_network, monkeypatch,
):
    """Same contract as the raises path — partial enforcement failure
    (some bot in pod scope failed, etc.) must not be papered over."""
    dispatched: list[dict] = []

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return True

    monkeypatch.setattr(spend_alert, "_dispatch", fake_dispatch)

    fake_enforce = MagicMock()
    fake_enforce.no_op = False
    fake_enforce.ok = False  # write failed, enforce returned non-ok
    import evolve_admin
    if not hasattr(evolve_admin, "breakers_enforce"):
        from evolve_admin import breakers_enforce as _real_be  # noqa: F401
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(return_value=fake_enforce)
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=5.50, cap=5.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-05-23",
    )

    # Fix B: reason carries the honest enforcement state
    reason = dispatched[0]["payload"]["reason"]
    assert "partially failed" in reason or "FAILED" in reason
    assert "heartbeat disabled" not in reason, (
        f"Reason must not claim heartbeat disabled when enforce_result.ok=False, "
        f"got: {reason!r}"
    )


def test_breaker_already_tripped_returns_false_when_no_file(fake_shared):
    """No breakers dir / no file → not tripped."""
    assert spend_alert._breaker_already_tripped(fake_shared, "security_bot") is False


def test_breaker_already_tripped_returns_true_when_active(
    fake_shared, monkeypatch,
):
    """A live (non-expired) cost trip returns True."""
    # Use the real breakers.store helpers to write a live trip
    from breakers import store as _bstore  # noqa: E402

    _bstore.trip(
        shared_dir=fake_shared,
        scope="security_bot",
        breaker_type="cost",
        duration=timedelta(hours=24),
        initiated_by="test",
        reason="test setup",
    )
    assert spend_alert._breaker_already_tripped(fake_shared, "security_bot") is True


def test_breaker_already_tripped_returns_false_when_expired(
    fake_shared, monkeypatch,
):
    """An expired trip is not "in effect" — returns False so a new trip fires."""
    from breakers import store as _bstore  # noqa: E402

    # Trip with -1h duration so it's already expired
    _bstore.trip(
        shared_dir=fake_shared,
        scope="security_bot",
        breaker_type="cost",
        duration=timedelta(hours=-1),
        initiated_by="test",
        reason="expired",
    )
    assert spend_alert._breaker_already_tripped(fake_shared, "security_bot") is False


# ── Enforcement flag written by the L1 trip ─────────────────────────────────
# The spend-cap enforcement flag used to be written only by the pod-wide
# ``thresholds.dailySpendCapUsd`` branch, which the Phase-8 migration made
# unreachable — so on migrated pods both of its consumers went dark:
#   * ModelRouter.isSpendCapActive, the in-band per-turn downgrade to the
#     ``fast`` role (the L1 breaker only pauses heartbeat + background;
#     user chat keeps running on the bot's normal, expensive model);
#   * GET /api/spend-caps active_enforcement + recent_enforcement_history.
# It is now written from the L1 trip, behind the same single resolved cap.


def test_trip_breaker_writes_spend_cap_enforcement_flag(
    fake_shared, fake_network, monkeypatch,
):
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: True)

    import evolve_admin
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(
        return_value=MagicMock(no_op=False, ok=True)
    )
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=33.78, cap=20.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-07-31",
    )

    flag = fake_shared / "spend-caps" / "security_bot-2026-07-31.json"
    assert flag.exists(), "ModelRouter + the Cost Measures page read this file"
    data = json.loads(flag.read_text())
    # Only "downgrade-tier" makes ModelRouter act; any other action is a no-op
    # for the per-turn downgrade.
    assert data["action"] == "downgrade-tier"
    assert data["cleared"] is False
    assert data["cap"] == 20.0
    assert data["spend_at_trigger"] == 33.78
    # The filename must carry the caller's pod-local day, not UTC — it has to
    # match ModelRouter.localDateYMD() for the flag to be found at all.
    assert data["date"] == "2026-07-31"


def test_spend_cap_flag_failure_does_not_block_the_breaker_trip(
    fake_shared, fake_network, monkeypatch,
):
    """The flag is best-effort. A write failure must leave the breaker
    tripped and the operator alerted, not abort enforcement."""
    monkeypatch.setattr(spend_alert, "_dispatch", lambda **kw: True)

    import evolve_admin
    fake_be = MagicMock()
    fake_be.enforce_trip = MagicMock(
        return_value=MagicMock(no_op=False, ok=True)
    )
    monkeypatch.setattr(evolve_admin, "breakers_enforce", fake_be)

    import spend_caps
    monkeypatch.setattr(
        spend_caps, "write_enforcement_flag",
        MagicMock(side_effect=OSError("read-only fs")),
    )

    spend_alert._trip_breaker_for_cost_cap(
        bot_id="security_bot", spend=33.78, cap=20.0,
        shared_dir=fake_shared, network=fake_network,
        today_iso="2026-07-31",
    )

    assert (fake_shared / "breakers" / "security_bot" / "cost.json").exists()
    fake_be.enforce_trip.assert_called_once()
