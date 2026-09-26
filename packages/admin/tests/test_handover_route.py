"""End-to-end tests for the handover Flask routes (V2.4-5).

Mounts only the handover blueprint on a tmp_path network.json so the
test doesn't pull in the full admin server stack.

Covers:
  • GET /handover/<token>            — friendly onboarding page renders
  • GET /handover/<bad>              — friendly "not recognized" page
  • GET /handover/<expired>          — friendly "no longer active" page
  • POST /handover/<token>/onboard   — applies prefs + marks claimed
  • Double POST                      — second attempt is rejected
  • POST /api/handover/generate      — admin API mints / fetches / rotates
  • GET  /api/handover/list          — lists existing tokens
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
for p in (str(_ADMIN), str(_ANALYZER)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _seed(tmp_path: Path) -> Path:
    network = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": ["diana_personal"],
        "bots": {
            "diana_personal": {
                "role": "member",
                "port": 19002,
                "user": "diana",
                "display_name": "Diana's Assistant",
            },
        },
        "pod": {"operator_name": "Maria"},
    }
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "network.json"
    p.write_text(json.dumps(network))
    return p


@pytest.fixture
def app_and_paths(tmp_path):
    from evolve_admin.web.handover_routes import register_handover_routes
    network_path = _seed(tmp_path)
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_handover_routes(app, network_path)
    return app, tmp_path, network_path


def _mint_token(tmp_path, **overrides) -> dict:
    from evolve_admin.handover import create_token
    shared = tmp_path / "shared"
    rec, _ = create_token(
        shared,
        bot_id=overrides.pop("bot_id", "diana_personal"),
        audience=overrides.pop("audience", "personal_bot_user"),
        message=overrides.pop("message", ""),
        expires_in_days=overrides.pop("expires_in_days", 7),
        rotate=True,
    )
    return rec


# ── GET /handover/<token> ─────────────────────────────────────────────────────


def test_get_valid_token_renders_onboarding_page(app_and_paths):
    app, tmp_path, _ = app_and_paths
    rec = _mint_token(tmp_path, message="Hi Diana — your assistant is ready.")
    with app.test_client() as c:
        r = c.get(f"/handover/{rec['token']}")
        assert r.status_code == 200
        html = r.data.decode()
        # Plex-test: no jargon
        assert "Charter" not in html
        assert "Generator" not in html
        assert "RSI" not in html
        assert "openclaw" not in html.lower()
        # Custom greeting surfaces
        assert "Hi Diana" in html
        # Display name surfaces (not the raw bot_id)
        assert "Diana&#39;s Assistant" in html or "Diana" in html
        # Voice presets are offered
        assert "Concierge" in html
        assert "Casual" in html
        # Safety affordance is mentioned
        assert "pause" in html.lower()


def test_get_unknown_token_shows_friendly_page(app_and_paths):
    app, _, _ = app_and_paths
    with app.test_client() as c:
        # Looks like a token (32 hex chars) but unknown
        r = c.get("/handover/" + "a" * 32)
        assert r.status_code == 200
        html = r.data.decode()
        assert "don&#39;t recognize" in html or "don&#x27;t recognize" in html or "don't recognize" in html
        # Operator name is surfaced from pod.operator_name
        assert "Maria" in html


def test_get_malformed_token_shows_friendly_page(app_and_paths):
    app, _, _ = app_and_paths
    with app.test_client() as c:
        r = c.get("/handover/not-a-real-token")
        assert r.status_code == 200
        html = r.data.decode()
        # No traceback
        assert "Traceback" not in html
        # Token isn't echoed
        assert "not-a-real-token" not in html


def test_get_expired_token_shows_friendly_page(app_and_paths):
    app, tmp_path, _ = app_and_paths
    rec = _mint_token(tmp_path)
    # Hand-edit to look expired
    token_path = tmp_path / "shared" / "handover-tokens" / f"{rec['token']}.json"
    payload = json.loads(token_path.read_text())
    payload["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    token_path.write_text(json.dumps(payload))

    with app.test_client() as c:
        r = c.get(f"/handover/{rec['token']}")
        assert r.status_code == 200
        html = r.data.decode()
        assert "no longer active" in html.lower() or "expired" in html.lower()
        # Token not echoed
        assert rec["token"] not in html


# ── POST /handover/<token>/onboard ────────────────────────────────────────────


def test_post_applies_prefs_and_marks_claimed(app_and_paths):
    app, tmp_path, _ = app_and_paths
    rec = _mint_token(tmp_path)
    with app.test_client() as c:
        r = c.post(
            f"/handover/{rec['token']}/onboard",
            json={
                "preferred_name": "Diana",
                "voice": "Concierge",
                "notes": "Two-home household, two adult kids.",
            },
        )
        assert r.status_code == 200, r.data
        data = r.get_json()
        assert data["ok"] is True
        assert "success_html" in data
        assert "Diana" in data["success_html"]
    # Token is now claimed
    token_path = tmp_path / "shared" / "handover-tokens" / f"{rec['token']}.json"
    stored = json.loads(token_path.read_text())
    assert stored["claimed_at"] is not None
    assert stored["preferences"]["preferred_name"] == "Diana"
    assert stored["preferences"]["voice"] == "Concierge"


def test_post_double_claim_rejected(app_and_paths):
    app, tmp_path, _ = app_and_paths
    rec = _mint_token(tmp_path)
    with app.test_client() as c:
        r1 = c.post(
            f"/handover/{rec['token']}/onboard",
            json={"preferred_name": "Diana", "voice": "Concierge"},
        )
        assert r1.status_code == 200
        r2 = c.post(
            f"/handover/{rec['token']}/onboard",
            json={"preferred_name": "Different person", "voice": "Casual"},
        )
        assert r2.status_code == 409
        data = r2.get_json()
        assert data["ok"] is False
    # First-claim preferences preserved
    token_path = tmp_path / "shared" / "handover-tokens" / f"{rec['token']}.json"
    stored = json.loads(token_path.read_text())
    assert stored["preferences"]["preferred_name"] == "Diana"


def test_post_drops_disallowed_fields(app_and_paths):
    """The preference allowlist must drop unknown fields — keeps the
    write surface narrow per the spec."""
    app, tmp_path, _ = app_and_paths
    rec = _mint_token(tmp_path)
    with app.test_client() as c:
        r = c.post(
            f"/handover/{rec['token']}/onboard",
            json={
                "preferred_name": "Diana",
                "voice": "Concierge",
                # Sneaky extra fields — should not appear in stored prefs.
                "is_admin": True,
                "openclaw_path": "/etc/passwd",
                "channels": ["slack"],
            },
        )
        assert r.status_code == 200
    stored = json.loads(
        (tmp_path / "shared" / "handover-tokens" / f"{rec['token']}.json").read_text()
    )
    prefs = stored["preferences"]
    assert "is_admin" not in prefs
    assert "openclaw_path" not in prefs
    assert "channels" not in prefs


def test_post_unknown_token_returns_404(app_and_paths):
    app, _, _ = app_and_paths
    with app.test_client() as c:
        r = c.post("/handover/" + "a" * 32 + "/onboard",
                   json={"preferred_name": "x", "voice": "Casual"})
        assert r.status_code == 404


# ── Admin API ─────────────────────────────────────────────────────────────────


def test_api_generate_creates_and_fetches_token(app_and_paths):
    app, tmp_path, _ = app_and_paths
    with app.test_client() as c:
        r1 = c.post("/api/handover/generate",
                    json={"bot_id": "diana_personal", "message": "Hi Diana"})
        assert r1.status_code == 200
        d1 = r1.get_json()
        assert d1["ok"] is True
        assert d1["created"] is True
        assert d1["bot_id"] == "diana_personal"
        token1 = d1["token"]
        assert d1["url"].endswith(f"/handover/{token1}")

        # Second call → existing token, not a fresh one
        r2 = c.post("/api/handover/generate", json={"bot_id": "diana_personal"})
        assert r2.status_code == 200
        d2 = r2.get_json()
        assert d2["token"] == token1
        assert d2["created"] is False


def test_api_generate_rotate_replaces(app_and_paths):
    app, tmp_path, _ = app_and_paths
    with app.test_client() as c:
        r1 = c.post("/api/handover/generate", json={"bot_id": "diana_personal"})
        assert r1.status_code == 200
        token1 = r1.get_json()["token"]
        r2 = c.post("/api/handover/generate",
                    json={"bot_id": "diana_personal", "rotate": True})
        assert r2.status_code == 200
        token2 = r2.get_json()["token"]
        assert token2 != token1


def test_api_generate_rejects_unknown_bot(app_and_paths):
    app, _, _ = app_and_paths
    with app.test_client() as c:
        r = c.post("/api/handover/generate", json={"bot_id": "ghost"})
        assert r.status_code == 404


def test_api_list_returns_tokens(app_and_paths):
    app, tmp_path, _ = app_and_paths
    _mint_token(tmp_path)
    with app.test_client() as c:
        r = c.get("/api/handover/list")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert len(data["tokens"]) == 1
        assert data["tokens"][0]["bot_id"] == "diana_personal"
        assert data["tokens"][0]["usable"] is True
