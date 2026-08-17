"""tests/test_arbiter_pod_defaults_phase5.py — Phase 5 of the cost-cap
normalization (spec: docs/spec-cost-caps-2026-06-05.md).

Exercises:
- POST /api/arbiter/bot-setup/<bot_id>: accepts the 4 new graduated-ladder
  fields (tier_downgrade_usd, l2_breaker_usd, weekly_warn_usd), validates
  the ladder ordering, and accepts l1_breaker_usd as a spec-name alias
  for daily_hard_usd.
- GET  /api/arbiter/bot-setup/<bot_id>: returns the new fields alongside
  the existing ones.
- GET  /api/arbiter/pod-defaults: returns the pod-default budget block
  including the new ladder defaults.
- POST /api/arbiter/pod-defaults: writes pod defaults; per-bot overrides
  remain sticky.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def arbiter_app(tmp_path):
    from evolve_admin.web.server import create_app

    shared_dir = tmp_path / "evolve"
    (shared_dir / "better-engine").mkdir(parents=True)
    network = {"members": ["team_bot_a"], "sharedDir": str(shared_dir)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, shared_dir


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/bot-setup/<bot_id> — Phase 5 fields
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_setup_accepts_tier_downgrade(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={"tier_downgrade_usd": 8.50},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    assert body["tier_downgrade_usd"] == 8.50


def test_bot_setup_accepts_l2_breaker(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={"l2_breaker_usd": 25.00},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["l2_breaker_usd"] == 25.00


def test_bot_setup_accepts_weekly_warn(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={"weekly_warn_usd": 12.00},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["weekly_warn_usd"] == 12.00


def test_bot_setup_l1_breaker_alias_writes_daily_hard(arbiter_app):
    """l1_breaker_usd is a spec-name alias; it writes to the existing
    daily_hard_usd storage. Phase 8 will rename the storage."""
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={"l1_breaker_usd": 15.00},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["daily_hard_usd"] == 15.00


def test_bot_setup_rejects_inverted_ladder(arbiter_app):
    """L2 < L1 → reject with 400 and a descriptive error mentioning the
    inverted pair."""
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={
            "daily_warn_usd": 5.00,
            "tier_downgrade_usd": 8.00,
            "l1_breaker_usd": 50.00,
            "l2_breaker_usd": 25.00,   # below L1 — invalid
        },
    )
    assert r.status_code == 400, r.get_json()
    body = r.get_json()
    assert body.get("kind") == "remediation_ladder_inverted"
    assert "l2_breaker" in body["error"] and "l1_breaker" in body["error"]


def test_bot_setup_rejects_warn_above_tier_downgrade(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={
            "daily_warn_usd": 10.00,
            "tier_downgrade_usd": 5.00,   # below warn — invalid
        },
    )
    assert r.status_code == 400


def test_bot_setup_accepts_well_ordered_full_ladder(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={
            "daily_warn_usd": 5.00,
            "tier_downgrade_usd": 8.00,
            "l1_breaker_usd": 12.00,
            "l2_breaker_usd": 25.00,
        },
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["daily_warn_usd"] == 5.00
    assert body["tier_downgrade_usd"] == 8.00
    assert body["daily_hard_usd"] == 12.00
    assert body["l2_breaker_usd"] == 25.00


def test_bot_setup_partial_ladder_is_valid(arbiter_app):
    """Operator sets warn + L1 only — missing tier_downgrade + L2 means
    'no enforcement at that tier'. Should accept."""
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={
            "daily_warn_usd": 5.00,
            "l1_breaker_usd": 12.00,
        },
    )
    assert r.status_code == 200, r.get_json()


def test_bot_setup_clear_l2_with_null(arbiter_app):
    """Setting a field to null clears it (back to default / no enforcement)."""
    app, _shared = arbiter_app
    client = app.test_client()
    # First set a value.
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a", json={"l2_breaker_usd": 25.00},
    )
    assert r.status_code == 200
    # Then clear.
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a", json={"l2_breaker_usd": None},
    )
    assert r.status_code == 200
    assert r.get_json()["l2_breaker_usd"] is None


def test_bot_setup_rejects_negative_l2(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/bot-setup/team_bot_a", json={"l2_breaker_usd": -5},
    )
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/arbiter/bot-setup/<bot_id> — Phase 5 fields in response
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_setup_get_returns_phase5_fields(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    # Seed some values via POST.
    client.post(
        "/api/arbiter/bot-setup/team_bot_a",
        json={
            "tier_downgrade_usd": 8.00,
            "l1_breaker_usd": 12.00,
            "l2_breaker_usd": 25.00,
            "weekly_warn_usd": 30.00,
        },
    )
    r = client.get("/api/arbiter/bot-setup/team_bot_a")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["tier_downgrade_usd"] == 8.00
    assert body["daily_hard_usd"] == 12.00
    assert body["l2_breaker_usd"] == 25.00
    assert body["weekly_warn_usd"] == 30.00
    # pod_weekly_warn_usd defaults to None until pod_defaults sets it.
    assert body["pod_weekly_warn_usd"] is None


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/arbiter/pod-defaults
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_defaults_get_returns_compiled_baseline(arbiter_app):
    """Fresh install → compiled defaults are returned."""
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.get("/api/arbiter/pod-defaults")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["ok"] is True
    # Compiled baseline values from better_engine_config._COMPILED_DEFAULTS.
    assert body["monthly_cap_usd"] == 50.00
    assert body["per_bot_daily_warn_usd"] == 2.00
    assert body["per_bot_daily_hard_usd"] == 5.00
    # Phase 5 ladder defaults default to None (opt-in).
    assert body["pod_weekly_warn_usd"] is None
    assert body["tier_downgrade_usd"] is None
    assert body["l2_breaker_usd"] is None
    assert body["weekly_warn_usd"] is None


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/arbiter/pod-defaults
# ─────────────────────────────────────────────────────────────────────────────


def test_pod_defaults_post_sets_phase5_defaults(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/pod-defaults",
        json={
            "tier_downgrade_usd": 10.00,
            "l2_breaker_usd": 50.00,
            "pod_weekly_warn_usd": 35.00,
        },
    )
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["ok"] is True
    assert body["changed"] is True
    assert set(body["applied"]) == {
        "tier_downgrade_usd", "l2_breaker_usd", "pod_weekly_warn_usd",
    }
    # Round-trip via GET.
    r2 = client.get("/api/arbiter/pod-defaults")
    body2 = r2.get_json()
    assert body2["tier_downgrade_usd"] == 10.00
    assert body2["l2_breaker_usd"] == 50.00
    assert body2["pod_weekly_warn_usd"] == 35.00


def test_pod_defaults_post_clears_with_null(arbiter_app):
    """Opt-in ladder fields can be cleared by sending null."""
    app, _shared = arbiter_app
    client = app.test_client()
    client.post(
        "/api/arbiter/pod-defaults",
        json={"l2_breaker_usd": 50.00},
    )
    r = client.post(
        "/api/arbiter/pod-defaults",
        json={"l2_breaker_usd": None},
    )
    assert r.status_code == 200
    r2 = client.get("/api/arbiter/pod-defaults")
    assert r2.get_json()["l2_breaker_usd"] is None


def test_pod_defaults_post_does_not_cascade_to_existing_overrides(arbiter_app):
    """Per-bot overrides are sticky — changing a pod default does NOT
    overwrite a bot that has an explicit per-bot value."""
    app, _shared = arbiter_app
    client = app.test_client()
    # Bot sets explicit L1 of $20.
    client.post(
        "/api/arbiter/bot-setup/team_bot_a", json={"l1_breaker_usd": 20.00},
    )
    # Pod default L1 changes to $5.
    client.post(
        "/api/arbiter/pod-defaults", json={"per_bot_daily_hard_usd": 5.00},
    )
    # Bot's own daily_hard_usd remains 20.00 (sticky).
    r = client.get("/api/arbiter/bot-setup/team_bot_a")
    body = r.get_json()
    assert body["daily_hard_usd"] == 20.00


def test_pod_defaults_post_rejects_negative_value(arbiter_app):
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/pod-defaults", json={"l2_breaker_usd": -5},
    )
    assert r.status_code == 400


def test_pod_defaults_post_legacy_fields_cannot_clear_with_null(arbiter_app):
    """The legacy fields (monthly_cap_usd / per_bot_daily_warn_usd /
    per_bot_daily_hard_usd) always have a compiled default; sending null
    is rejected since clearing has no meaningful semantics."""
    app, _shared = arbiter_app
    client = app.test_client()
    r = client.post(
        "/api/arbiter/pod-defaults", json={"per_bot_daily_warn_usd": None},
    )
    assert r.status_code == 400


def test_pod_defaults_post_accepts_canonical_short_name_aliases(arbiter_app):
    """The Cost & Caps matrix on the client uses the canonical short names
    from the cost-cap spec (``daily_hard_usd``, ``daily_warn_usd``,
    ``l1_breaker_usd``, ``session_cost_cap_usd``). Each must alias to the
    BE-config storage key with the ``per_bot_`` prefix; without aliasing,
    the POST silently dropped the unknown field and the next reload
    showed the unchanged previous value — the "L1 keeps reverting" bug.
    """
    app, _shared = arbiter_app
    client = app.test_client()

    # Send each alias and verify the canonical pod-default field updates.
    for alias, expected_key, value in [
        ("daily_hard_usd",       "per_bot_daily_hard_usd",       17.50),
        ("daily_warn_usd",       "per_bot_daily_warn_usd",        3.00),
        ("l1_breaker_usd",       "per_bot_daily_hard_usd",       22.00),
        ("session_cost_cap_usd", "per_bot_session_cost_cap_usd",  4.25),
    ]:
        r = client.post("/api/arbiter/pod-defaults", json={alias: value})
        assert r.status_code == 200, (
            f"alias {alias!r} → {expected_key!r} POST failed: "
            f"{r.status_code} {r.get_data(as_text=True)}"
        )
        body = r.get_json()
        assert body["ok"] is True
        # The "applied" list reports the canonical storage key — confirms the
        # alias translation reached the writer, not the silent-drop branch.
        assert expected_key in body["applied"], (
            f"alias {alias!r} routed to {body['applied']!r} instead of "
            f"{expected_key!r}"
        )

    # Round-trip via GET to confirm the values stuck.
    r = client.get("/api/arbiter/pod-defaults")
    body = r.get_json()
    assert body["per_bot_daily_warn_usd"] == 3.00
    # daily_hard_usd was set twice (via daily_hard_usd and l1_breaker_usd);
    # the second write wins.
    assert body["per_bot_daily_hard_usd"] == 22.00
    assert body["session_cost_cap_usd"] == 4.25
