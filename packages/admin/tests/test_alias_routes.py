"""tests/test_alias_routes.py — Standalone alias editor endpoint tests.

Covers ``/api/bot/<bot_id>/alias`` GET + PUT registered by
``evolve_admin.web.routes_alias.register_routes``.

Focus areas:

  * GET returns the existing alias when present and the suggested
    primary_user.name when absent.
  * PUT writes only the ``correspondence`` block — ``google_integration``
    is byte-identical before/after.
  * Validation: bad disclosure rejected; ``none`` requires a reason;
    empty name clears the block.
  * Multi-user flag round-trips so the frontend can surface the
    "alias will rotate per initiating user" hint (deliverable C
    follow-up).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from flask import Flask  # noqa: E402

from evolve_admin.web.routes_alias import (  # noqa: E402
    _validate_alias_payload,
    register_routes,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def network_path(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "primary": "evo",
        "bots": {
            # Single-user bot with no alias yet — exercises the
            # "suggested_name from primary_user.name" path.
            "lex": {
                "role": "member",
                "port": 19010,
                "display_name": "Lex",
                "primary_user": {
                    "name": "Sam",
                    "email_address": "sam@example.com",
                    "pod_user": "sam",
                },
                "google_integration": {
                    "mode": "service_account_dwd",
                    "subject": "lex@example-corp.com",
                    "workspace_domain": "example-corp.com",
                    "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                    "service_account_secret_ref": "google-sa-example-corp",
                },
            },
            # Bot with an existing alias — exercises the round-trip
            # update path and the never-touch-google_integration
            # invariant.
            "personal-bot": {
                "role": "member",
                "port": 19011,
                "display_name": "Jane",
                "primary_user": {"name": "Sam"},
                "correspondence": {
                    "name": "Jane",
                    "email_address": "personal-bot@example.com",
                    "disclosure": "soft",
                },
                "google_integration": {
                    "mode": "service_account_dwd",
                    "subject": "personal-bot@example-corp.com",
                    "workspace_domain": "example-corp.com",
                    "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                    "service_account_secret_ref": "google-sa-example-corp",
                },
            },
            # Multi-user bot — surfaces the multi_user flag so the
            # frontend can show the "alias rotates per initiating user"
            # hint.
            "team-bot-a": {
                "role": "member",
                "port": 19012,
                "display_name": "Team-Bot-A",
                "multiUser": True,
                "primary_user": {"name": "Sam"},
            },
        },
        "members": ["lex", "personal-bot", "team-bot-a"],
    }))
    return p


@pytest.fixture
def app(network_path: Path) -> Flask:
    flask_app = Flask(__name__)
    register_routes(flask_app, network_path)
    return flask_app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


# ── Validation unit tests ───────────────────────────────────────────────────


def test_validate_empty_name_clears_block():
    """An empty name signals "clear the alias entirely" — return empty cleaned."""
    errors, cleaned = _validate_alias_payload({"name": ""})
    assert errors == []
    assert cleaned == {}


def test_validate_missing_name_clears_block():
    """No name field at all is also a clear-block signal."""
    errors, cleaned = _validate_alias_payload({})
    assert errors == []
    assert cleaned == {}


def test_validate_happy_path_with_disclosure_default():
    errors, cleaned = _validate_alias_payload({"name": "Sam Riley"})
    assert errors == []
    assert cleaned["name"] == "Sam Riley"
    assert cleaned["disclosure"] == "soft"  # default


def test_validate_rejects_bogus_disclosure():
    errors, _ = _validate_alias_payload({
        "name": "Sam", "disclosure": "yikes",
    })
    assert any("disclosure must be one of" in e for e in errors)


def test_validate_none_disclosure_requires_reason():
    errors, _ = _validate_alias_payload({
        "name": "Sam", "disclosure": "none",
    })
    assert any("disclosure_override_reason" in e for e in errors)


def test_validate_none_disclosure_with_reason_accepted():
    errors, cleaned = _validate_alias_payload({
        "name": "Sam", "disclosure": "none",
        "disclosure_override_reason": "long-standing vendor relationships",
    })
    assert errors == []
    assert cleaned["disclosure"] == "none"
    assert cleaned["disclosure_override_reason"] == (
        "long-standing vendor relationships"
    )


def test_validate_bad_email_rejected():
    errors, _ = _validate_alias_payload({
        "name": "Sam", "email_address": "not-an-email",
    })
    assert any("email_address" in e for e in errors)


# ── GET /api/bot/<bot_id>/alias ─────────────────────────────────────────────


def test_get_unknown_bot_404(client):
    r = client.get("/api/bot/nope/alias")
    assert r.status_code == 404
    assert "not found" in r.get_json()["error"]


def test_get_no_alias_suggests_primary_user_name(client):
    """lex has no correspondence block — suggested_name = primary_user.name."""
    r = client.get("/api/bot/lex/alias")
    assert r.status_code == 200
    body = r.get_json()
    assert body["alias"] is None
    assert body["suggested_name"] == "Sam"
    assert body["primary_user"]["name"] == "Sam"
    assert body["has_workspace_mailbox"] is True
    assert body["workspace_mailbox"] == "lex@example-corp.com"
    assert body["multi_user"] is False


def test_get_existing_alias_round_trips(client):
    r = client.get("/api/bot/personal-bot/alias")
    body = r.get_json()
    assert body["alias"]["name"] == "Jane"
    assert body["alias"]["disclosure"] == "soft"
    assert body["alias"]["email_address"] == "personal-bot@example.com"


def test_get_multi_user_flag(client):
    r = client.get("/api/bot/team-bot-a/alias")
    body = r.get_json()
    assert body["multi_user"] is True
    # team-bot-a has no google_integration yet
    assert body["has_workspace_mailbox"] is False


# ── PUT /api/bot/<bot_id>/alias ─────────────────────────────────────────────


def test_put_unknown_bot_404(client):
    r = client.put("/api/bot/nope/alias", json={"name": "X"})
    assert r.status_code == 404


def test_put_writes_alias_and_persists(client, network_path: Path):
    r = client.put("/api/bot/lex/alias", json={
        "name": "Sam",  # alias = the user's own name (the natural default)
        "disclosure": "soft",
    })
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["alias"]["name"] == "Sam"
    assert body["alias"]["disclosure"] == "soft"

    # Reload from disk to verify persistence
    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["lex"]["correspondence"]["name"] == "Sam"


def test_put_does_not_clobber_google_integration(
    client, network_path: Path,
):
    """PUT on the alias block must leave google_integration byte-identical.

    Regression guard for the "wizard owns all per-bot Google config"
    coupling — the whole point of the standalone editor is that an
    operator renaming the alias shouldn't have to re-walk SA upload,
    scope picker, and pre-flight.
    """
    gi_before = json.loads(network_path.read_text())["bots"]["personal-bot"][
        "google_integration"
    ]
    r = client.put("/api/bot/personal-bot/alias", json={
        "name": "Janet",  # rename from Jane → Janet
        "disclosure": "soft",
    })
    assert r.status_code == 200, r.get_json()

    on_disk = json.loads(network_path.read_text())
    assert on_disk["bots"]["personal-bot"]["correspondence"]["name"] == "Janet"
    # The whole google_integration block is byte-identical.
    assert on_disk["bots"]["personal-bot"]["google_integration"] == gi_before


def test_put_empty_name_clears_block(client, network_path: Path):
    """Sending name='' deletes the correspondence block entirely."""
    r = client.put("/api/bot/personal-bot/alias", json={"name": ""})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["cleared"] is True
    assert body["alias"] is None

    on_disk = json.loads(network_path.read_text())
    assert "correspondence" not in on_disk["bots"]["personal-bot"]
    # And google_integration is still there.
    assert "google_integration" in on_disk["bots"]["personal-bot"]


def test_put_validation_errors_block_save(client, network_path: Path):
    """Bad disclosure -> 400, no write."""
    before = network_path.read_text()
    r = client.put("/api/bot/lex/alias", json={
        "name": "Sam", "disclosure": "yikes",
    })
    assert r.status_code == 400
    assert "errors" in r.get_json()
    # File unchanged
    assert network_path.read_text() == before


def test_put_none_disclosure_without_reason_blocked(
    client, network_path: Path,
):
    before = network_path.read_text()
    r = client.put("/api/bot/lex/alias", json={
        "name": "Sam", "disclosure": "none",
    })
    assert r.status_code == 400
    body = r.get_json()
    assert any("disclosure_override_reason" in e for e in body["errors"])
    assert network_path.read_text() == before


def test_put_idempotent_clear(client, network_path: Path):
    """Clearing an already-absent block returns ok without touching disk."""
    # lex has no correspondence block — clear-on-absent should succeed.
    before = network_path.read_text()
    r = client.put("/api/bot/lex/alias", json={"name": ""})
    assert r.status_code == 200
    body = r.get_json()
    assert body["cleared"] is False  # nothing was actually removed
    # File unchanged — no spurious rewrite
    assert network_path.read_text() == before
