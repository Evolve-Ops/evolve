"""Tests for /api/admin/engine-tier-override (GET + PUT).

The engine override knob (added 2026-06-07) lets operators pin
all engine-side (non-bot) LLM calls to a specific tier — cost-control
for background Evolve work that shouldn't burn tier1 budget.

Pairs with packages/analyzer/tests/test_models_credential_aware_defaults.py
which tests the resolver-side behavior. These tests cover the
HTTP surface: GET reads, PUT writes + validates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web.server import create_app  # noqa: E402


def _make_client(tmp_path: Path, initial_network: dict | None = None):
    """Build a Flask test client backed by a tmp network.json."""
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(initial_network or {"sharedDir": str(tmp_path)}))
    app = create_app(network_path=network_path)
    return app.test_client(), network_path


# ── GET /api/admin/engine-tier-override ─────────────────────────────────────


def test_get_returns_null_when_unset(tmp_path: Path):
    """Brand-new network.json with no cascade block — endpoint returns
    null. Confirms the read path is fail-safe on the default case."""
    client, _ = _make_client(tmp_path)
    resp = client.get("/api/admin/engine-tier-override")
    assert resp.status_code == 200
    assert resp.get_json() == {"engine_default_tier": None}


def test_get_returns_value_when_set(tmp_path: Path):
    network = {
        "sharedDir": str(tmp_path),
        "cascade": {"engine_default_tier": "tier3"},
    }
    client, _ = _make_client(tmp_path, network)
    resp = client.get("/api/admin/engine-tier-override")
    assert resp.status_code == 200
    assert resp.get_json() == {"engine_default_tier": "tier3"}


def test_get_returns_null_for_invalid_stored_value(tmp_path: Path):
    """Defensive: if someone hand-edits network.json with garbage,
    GET returns null rather than echoing the invalid value. The
    resolver would also ignore it, so the UI and behavior agree."""
    network = {
        "sharedDir": str(tmp_path),
        "cascade": {"engine_default_tier": "haiku"},
    }
    client, _ = _make_client(tmp_path, network)
    resp = client.get("/api/admin/engine-tier-override")
    assert resp.status_code == 200
    assert resp.get_json() == {"engine_default_tier": None}


# ── PUT /api/admin/engine-tier-override ─────────────────────────────────────


def test_put_sets_value_and_persists(tmp_path: Path):
    client, network_path = _make_client(tmp_path)
    resp = client.put(
        "/api/admin/engine-tier-override",
        json={"engine_default_tier": "tier3"},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"engine_default_tier": "tier3", "saved": True}
    # File state should reflect the change
    on_disk = json.loads(network_path.read_text())
    assert on_disk["cascade"]["engine_default_tier"] == "tier3"


def test_put_null_clears_existing_value(tmp_path: Path):
    network = {
        "sharedDir": str(tmp_path),
        "cascade": {"engine_default_tier": "tier3"},
    }
    client, network_path = _make_client(tmp_path, network)
    resp = client.put(
        "/api/admin/engine-tier-override",
        json={"engine_default_tier": None},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"engine_default_tier": None, "saved": True}
    on_disk = json.loads(network_path.read_text())
    # cascade block may still exist but engine_default_tier removed
    assert "engine_default_tier" not in on_disk.get("cascade", {})


def test_put_rejects_invalid_tier(tmp_path: Path):
    client, _ = _make_client(tmp_path)
    resp = client.put(
        "/api/admin/engine-tier-override",
        json={"engine_default_tier": "tier99"},
    )
    assert resp.status_code == 400
    assert "must be null or" in resp.get_json()["error"]


def test_put_rejects_non_string_tier(tmp_path: Path):
    """Numeric, dict, etc. all rejected. Defensive — UI shouldn't
    send these, but the API contract pin matters for direct callers."""
    client, _ = _make_client(tmp_path)
    for bad in [3, {"tier": "tier3"}, ["tier3"]]:
        resp = client.put(
            "/api/admin/engine-tier-override",
            json={"engine_default_tier": bad},
        )
        assert resp.status_code == 400, f"Expected 400 for {bad!r}, got {resp.status_code}"


def test_put_accepts_all_four_tier_ids(tmp_path: Path):
    """Pin: tier0 / tier1 / tier2 / tier3 all accepted by the PUT
    endpoint. The UI deliberately omits tier0 from the picker (anti-
    Goodhart), but the API accepts it so operators can set via
    direct PUT for testing / edge cases."""
    for tier in ("tier0", "tier1", "tier2", "tier3"):
        client, _ = _make_client(tmp_path)
        resp = client.put(
            "/api/admin/engine-tier-override",
            json={"engine_default_tier": tier},
        )
        assert resp.status_code == 200, f"Expected 200 for {tier}, got {resp.status_code}"


def test_put_then_get_round_trip(tmp_path: Path):
    """End-to-end: PUT a value, GET it back, confirm equality."""
    client, _ = _make_client(tmp_path)
    client.put(
        "/api/admin/engine-tier-override",
        json={"engine_default_tier": "tier2"},
    )
    resp = client.get("/api/admin/engine-tier-override")
    assert resp.get_json() == {"engine_default_tier": "tier2"}


# ── /api/models/tier-resolution — new fields ────────────────────────────────


def test_tier_resolution_includes_new_fields(tmp_path: Path):
    """The 2026-06-07 changes added ``available_providers`` and
    ``engine_default_tier`` to the response. UI consumes these."""
    client, _ = _make_client(tmp_path)
    resp = client.get("/api/models/tier-resolution")
    # 200 expected even on a network.json with no bots (falls back to
    # DEFAULT_TIERS — derived_defaults can't run without providers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "tiers" in data
    assert "available_providers" in data
    assert "engine_default_tier" in data
    # On a brand-new pod, no providers are readable and no override is set
    assert data["available_providers"] == []
    assert data["engine_default_tier"] is None
