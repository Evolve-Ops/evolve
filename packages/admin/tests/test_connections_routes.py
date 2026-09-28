"""test_connections_routes.py — GET /api/connections/<bot_id>."""
from __future__ import annotations

import json

from flask import Flask

from evolve_admin import connections as conn
from evolve_admin.web.connections_routes import register_connections_routes


def _app(network_path) -> Flask:
    app = Flask(__name__)
    register_connections_routes(app, network_path)
    return app


def _write_network(tmp_path):
    p = tmp_path / "network.json"
    p.write_text(json.dumps({"sharedDir": str(tmp_path), "bots": {"lex": {"role": "member"}}}))
    return p


class TestConnectionsRoute:
    def test_unknown_bot_is_404(self, tmp_path):
        network_path = _write_network(tmp_path)
        client = _app(network_path).test_client()
        resp = client.get("/api/connections/ghost")
        assert resp.status_code == 404

    def test_known_bot_with_no_rows_returns_empty_list(self, tmp_path):
        network_path = _write_network(tmp_path)
        client = _app(network_path).test_client()
        resp = client.get("/api/connections/lex")
        assert resp.status_code == 200
        assert resp.get_json()["connections"] == []

    def test_reads_the_registry_from_the_networks_shared_dir(self, tmp_path):
        # The file is resolved from network.json's sharedDir, not a default
        # bound at import — a row written there is the row the route serves.
        network_path = _write_network(tmp_path)
        row = conn.new_connection(
            bot_id="lex", service="github", account_label="repo",
            role="own", capabilities_=["repo.push_backup"],
            credential_ref="keystore:github_pat", credential_kind="pat",
        )
        conn.save_connections({"connections": [row]}, path=tmp_path / "connections.json")
        body = _app(network_path).test_client().get("/api/connections/lex").get_json()
        assert [c["service"] for c in body["connections"]] == ["github"]

    def test_returns_health_display_text_not_a_raw_state_only(self, tmp_path):
        network_path = _write_network(tmp_path)
        conn_path = tmp_path / "connections.json"
        row = conn.new_connection(
            bot_id="lex", service="google", account_label="lex@example.com",
            role="own", jobs=["read_person_calendar"],
            capabilities_=["calendar.read"],
            credential_ref="google_integration:lex",
            credential_kind="service_account_dwd",
        )
        conn.save_connections({"connections": [row]}, path=conn_path)
        client = _app(network_path).test_client()
        resp = client.get("/api/connections/lex")
        body = resp.get_json()
        assert len(body["connections"]) == 1
        entry = body["connections"][0]
        assert entry["health_display"] == "not yet verified"
        assert entry["capabilities"] == ["calendar.read"]
        assert "credential_ref" not in entry
