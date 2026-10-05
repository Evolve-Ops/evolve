"""tests/test_pod_default_role_endpoint.py — PUT /api/admin/config/pod/models/default-role.

D-DT4/D-DT5 (internal/decision-default-tier-power-2026-09-23.md). The
pod-wide default conversation role writer — CLI twin is `evolve-admin
models default-role`. Writes network.json::models.defaultRole via
evolve_config._patch_network_json (splice-one-child, so a sibling
models.embedding block survives).

Coverage:
  - PUT with each valid role (fast/standard/power) writes and echoes.
  - PUT rejects `max` (pull-only) and junk values without writing.
  - models.embedding survives a defaultRole write.
  - audited as config.pod_models.default_role.set.

No real bot/user names appear; placeholders only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_EMBEDDING = {"provider": "prov_a", "model": "prov_a/embed-1", "dimensions": 256}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Flask app with a real tmp network.json carrying a models.embedding block."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["a_bot"],
        "sharedDir": str(shared),
        "bots": {"a_bot": {"role": "member"}},
        "models": {"embedding": dict(_EMBEDDING)},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network, indent=2))

    audit_calls: list[dict] = []
    monkeypatch.setattr(
        srv, "_audit_log_entry",
        lambda action, bot_id, details, oc_keys=None: audit_calls.append(
            {"action": action, "bot_id": bot_id, "details": details},
        ),
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, network_path, audit_calls


def _read_models(network_path: Path) -> dict:
    return json.loads(network_path.read_text()).get("models", {})


@pytest.mark.parametrize("role", ["fast", "standard", "power"])
def test_put_each_valid_role_writes_and_echoes(app_env, role):
    app, network_path, audit_calls = app_env
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/pod/models/default-role",
            json={"defaultRole": role},
        )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body == {"ok": True, "defaultRole": role}
    assert _read_models(network_path)["defaultRole"] == role
    assert audit_calls == [{
        "action": "config.pod_models.default_role.set",
        "bot_id": "pod",
        "details": {"defaultRole": role},
    }]


def test_put_rejects_max_pull_only(app_env):
    app, network_path, audit_calls = app_env
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/pod/models/default-role",
            json={"defaultRole": "max"},
        )
    assert resp.status_code == 400
    assert "max" in resp.get_json()["error"]
    assert "defaultRole" not in _read_models(network_path)
    assert audit_calls == []


@pytest.mark.parametrize("bad", ["auto", "junk", "", None, 42, True, ["power"]])
def test_put_rejects_unrecognized_values(app_env, bad):
    app, network_path, _ = app_env
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/pod/models/default-role",
            json={"defaultRole": bad},
        )
    assert resp.status_code == 400, f"value {bad!r} should be rejected"
    assert "defaultRole" not in _read_models(network_path)


def test_put_preserves_sibling_embedding_block(app_env):
    app, network_path, _ = app_env
    with app.test_client() as c:
        resp = c.put(
            "/api/admin/config/pod/models/default-role",
            json={"defaultRole": "standard"},
        )
    assert resp.status_code == 200
    models = _read_models(network_path)
    assert models["defaultRole"] == "standard"
    assert models["embedding"] == _EMBEDDING


def test_put_missing_field_returns_400(app_env):
    app, network_path, audit_calls = app_env
    with app.test_client() as c:
        resp = c.put("/api/admin/config/pod/models/default-role", json={})
    assert resp.status_code == 400
    assert audit_calls == []
