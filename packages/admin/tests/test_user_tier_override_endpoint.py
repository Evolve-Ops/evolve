"""tests/test_user_tier_override_endpoint.py — PUT
/api/admin/config/<bot>/user-tier-override (audit #69 Phase A).

The endpoint writes the per-bot ``userTierOverride`` block into
~/.openclaw/evolve-tiers.json via oc_full_config_set with partial-merge
semantics. The plugin's ModelRouter._resolveOperatorDefaultTier reads
the ``defaultTier`` field to seed user-turn / ambiguous sessions
before the bot-default fallback kicks in.

Coverage:
  - Happy path: each defaultTier choice (auto/fast/standard/power) is
    accepted and written through oc_full_config_set_with_error
  - Partial merges: enabled / dailyCap / allowBotInitiated / defaultTier
    can each be sent independently (no coupling)
  - Validation: unknown keys, bad enums, out-of-range cap, non-bool
    booleans all return 400 without writing
  - Empty body returns 400
  - Audit log entry has action=config.user_tier_override.set,
    oc_keys carrying the "tiers:<key>" names the write landed, full details
    payload
  - Write failure surfaces a 500 with the structured error message
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
def app_and_calls(tmp_path, monkeypatch):
    """Build a Flask app with stubbed oc_full_config_set_with_error +
    audit_log entry, plus a tmp shared dir / network.json."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["admin_bot"],
        "sharedDir": str(shared),
        "bots": {"admin_bot": {"role": "member"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    cfg_set_calls: list[dict] = []
    audit_calls: list[dict] = []

    def _stub_cfg_set_err(bot_id, updates, **kw):
        cfg_set_calls.append({"bot_id": bot_id, "updates": updates})
        # Mirror the real shape — the endpoint echoes userTierOverride, and
        # reports the evolve-tiers.json top-level keys the write landed
        # (``tiersKeysWritten``) so the endpoint can declare them.
        return ({
            "userTierOverride": updates.get("userTierOverride", {}),
            "tiersKeysWritten": ["userTierOverride"],
        }, None)

    def _stub_audit(action, bot_id, details, oc_keys=None):
        audit_calls.append({
            "action": action,
            "bot_id": bot_id,
            "details": details,
            "oc_keys": oc_keys,
        })

    import oc_cli
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error", _stub_cfg_set_err,
    )
    monkeypatch.setattr(srv, "_audit_log_entry", _stub_audit)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, cfg_set_calls, audit_calls


# ── Happy path — each defaultTier choice writes ───────────────────────────


@pytest.mark.parametrize("choice", ["auto", "fast", "standard", "power"])
def test_put_default_tier_each_choice(app_and_calls, choice):
    """All four enum values write through and audit cleanly."""
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/admin_bot/user-tier-override",
            json={"defaultTier": choice},
        )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["bot"] == "admin_bot"

    assert len(cfg_set_calls) == 1
    assert cfg_set_calls[0]["bot_id"] == "admin_bot"
    assert cfg_set_calls[0]["updates"] == {
        "userTierOverride": {"defaultTier": choice},
    }

    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "config.user_tier_override.set"
    assert audit_calls[0]["bot_id"] == "admin_bot"
    assert audit_calls[0]["details"] == {"defaultTier": choice}
    # heal.py credits this endpoint as the writer of the evolve-tiers.json
    # keys it landed, by checking oc_keys.
    #
    # This assertion used to read ``== {"tiers"}`` — a bare name heal NEVER
    # emits. heal namespaces evolve-tiers.json drift PER KEY
    # (``tiers:userTierOverride``), so the bare declaration credited nothing
    # and every write here surfaced as permanent unexplained drift. The
    # endpoint now declares what the writer reported landing, plus ``agents``
    # for the openclaw.json side of a tier write
    # (spec-delta-digest-audit-noise-2026-08-25 D3).
    assert audit_calls[0]["oc_keys"] == {"agents", "tiers:userTierOverride"}


def test_put_default_tier_power_round_trips_through_stub(app_and_calls):
    """Sanity: the stubbed underlying write returns the same value,
    so the UI's optimistic-update path (read from response body)
    works without a follow-up GET."""
    app, _, _ = app_and_calls
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/admin_bot/user-tier-override",
            json={"defaultTier": "power"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["userTierOverride"] == {"defaultTier": "power"}


# ── Partial merges — each field independently ─────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", True),
        ("enabled", False),
        ("dailyCap", 0),
        ("dailyCap", 50),
        ("dailyCap", 100),
        ("allowBotInitiated", True),
        ("allowBotInitiated", False),
    ],
)
def test_put_each_field_independently(app_and_calls, field, value):
    """The whole point of the partial-merge shape (mirroring the cascade
    endpoint) is that the AI Optimization page can ship Phase A as a
    standalone dropdown without touching the existing dailyCap / chip
    surfaces. Verifies each field flows through cleanly on its own."""
    app, cfg_set_calls, _ = app_and_calls
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/admin_bot/user-tier-override",
            json={field: value},
        )
    assert resp.status_code == 200, resp.get_json()
    assert cfg_set_calls[0]["updates"] == {
        "userTierOverride": {field: value},
    }


# ── Payload validation ────────────────────────────────────────────────────


def test_put_empty_body_returns_400(app_and_calls):
    """At least one field required — silent no-op writes hide bugs."""
    app, cfg_set_calls, audit_calls = app_and_calls
    with app.test_client() as c:
        resp = c.put("/api/admin/config/admin_bot/user-tier-override", json={})
    assert resp.status_code == 400
    assert "at least one field" in resp.get_json()["error"]
    assert cfg_set_calls == []
    assert audit_calls == []


def test_put_unknown_field_returns_400(app_and_calls):
    """Drive-by writers can't poison the block with future / typo'd
    keys. Plugin-side fall-through is conservative (unknown = ignore)
    but the operator gets a clear 400 to catch the bug at the UI."""
    app, cfg_set_calls, _ = app_and_calls
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/admin_bot/user-tier-override",
            json={"defaultTier": "fast", "rogueField": "x"},
        )
    assert resp.status_code == 400
    assert "rogueField" in resp.get_json()["error"]
    assert cfg_set_calls == []


def test_put_bad_default_tier_enum_returns_400(app_and_calls):
    """Operator typo or future enum value we don't know about must be
    rejected at the boundary, not silently dropped server-side."""
    app, cfg_set_calls, _ = app_and_calls
    with app.test_client() as c:
        for bad in ("turbo", "FAST", "  fast  ", "", None, 42, ["fast"]):
            resp = c.put(
                "/api/admin/config/admin_bot/user-tier-override",
                json={"defaultTier": bad},
            )
            assert resp.status_code == 400, (
                f"value {bad!r} should be rejected"
            )
    assert cfg_set_calls == []


def test_put_bad_daily_cap_returns_400(app_and_calls):
    """Range 0–100 + must be int (not bool, not float, not str)."""
    app, cfg_set_calls, _ = app_and_calls
    with app.test_client() as c:
        for bad in (-1, 101, "10", 10.5, True, False, None):
            resp = c.put(
                "/api/admin/config/admin_bot/user-tier-override",
                json={"dailyCap": bad},
            )
            assert resp.status_code == 400, (
                f"value {bad!r} should be rejected"
            )
    assert cfg_set_calls == []


def test_put_non_bool_enabled_returns_400(app_and_calls):
    """Strings like "true" / 1 must not flip the toggle. Same strictness
    as the cascade endpoint — JSON contract is bool."""
    app, cfg_set_calls, _ = app_and_calls
    with app.test_client() as c:
        for bad in ("true", "false", 1, 0, None, "yes"):
            resp = c.put(
                "/api/admin/config/admin_bot/user-tier-override",
                json={"enabled": bad},
            )
            assert resp.status_code == 400, (
                f"value {bad!r} should be rejected"
            )
    assert cfg_set_calls == []


def test_put_non_bool_allow_bot_initiated_returns_400(app_and_calls):
    app, cfg_set_calls, _ = app_and_calls
    with app.test_client() as c:
        for bad in ("true", 1, 0, None):
            resp = c.put(
                "/api/admin/config/admin_bot/user-tier-override",
                json={"allowBotInitiated": bad},
            )
            assert resp.status_code == 400, (
                f"value {bad!r} should be rejected"
            )
    assert cfg_set_calls == []


# ── Failure path ──────────────────────────────────────────────────────────


def test_put_returns_500_when_underlying_write_fails(monkeypatch, tmp_path):
    """When oc_full_config_set_with_error returns (None, err), surface
    a 500 with the structured error so the operator sees the real
    cause (not "check the server logs")."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["admin_bot"],
        "sharedDir": str(shared),
        "bots": {"admin_bot": {"role": "member"}},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    import oc_cli
    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda *a, **kw: (None, "disk full"),
    )
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/admin_bot/user-tier-override",
            json={"defaultTier": "fast"},
        )
    assert resp.status_code == 500
    body = resp.get_json()
    assert "write failed" in body["error"]
    # Structured error from oc_model.py propagates up.
    assert "disk full" in body["error"]
