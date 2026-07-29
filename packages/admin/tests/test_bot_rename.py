"""tests/test_bot_rename.py — POST /api/bots/<bot_id>/rename.

Wraps upstream `openclaw agents set-identity` so the bot identifies
with a new display name in chat (Telegram/Slack/etc.) and caches the
value in network.json so the admin UI can render it without
re-shelling on every refresh.

The tests stub out the upstream OC subprocess call (oc_command_raw)
so they exercise the route's validation + network.json mutation
without needing a real openclaw binary.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def app_with_bots(tmp_path):
    """A Flask app with the bot-config routes wired up and a
    network.json containing two bots."""
    from evolve_admin.web.server import create_app

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["evolve", "team_bot_a"],
        "bots": {
            "evolve": {"role": "primary", "user": "evolve", "tier": "full"},
            "team_bot_a": {"role": "member", "tier": "full"},
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path=network_path)
    app.config["TESTING"] = True
    app.config["_NETWORK_PATH"] = network_path
    app.config["_SHARED_DIR"] = tmp_path
    return app


def _read_network(app) -> dict:
    return json.loads(Path(app.config["_NETWORK_PATH"]).read_text())


# ── Happy path ──────────────────────────────────────────────────────────────


def test_rename_writes_display_name_and_calls_oc(app_with_bots):
    """Success path: name validates, OC subprocess succeeds, network.json
    is updated with the new display_name."""
    client = app_with_bots.test_client()

    with patch("oc_cli.oc_command_raw", return_value="ok\n") as mock_oc:
        r = client.post(
            "/api/bots/evolve/rename",
            json={"name": "Evo"},
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["display_name"] == "Evo"
    assert body["bot_id"] == "evolve"

    # OC subprocess called with the right args.
    mock_oc.assert_called_once()
    args, kwargs = mock_oc.call_args
    assert args[0] == "evolve"
    assert args[1] == ["agents", "set-identity", "--agent", "main", "--name", "Evo"]

    # Cache landed in network.json.
    net = _read_network(app_with_bots)
    assert net["bots"]["evolve"]["display_name"] == "Evo"


def test_rename_strips_whitespace(app_with_bots):
    """Trailing/leading whitespace is silently trimmed — common paste
    bug, no need to bounce the caller for it."""
    client = app_with_bots.test_client()

    with patch("oc_cli.oc_command_raw", return_value="ok\n"):
        r = client.post(
            "/api/bots/evolve/rename",
            json={"name": "  Evo  "},
        )

    assert r.status_code == 200
    assert r.get_json()["display_name"] == "Evo"


# ── Validation ──────────────────────────────────────────────────────────────


def test_empty_name_rejected(app_with_bots):
    client = app_with_bots.test_client()
    r = client.post("/api/bots/evolve/rename", json={"name": ""})
    assert r.status_code == 400
    assert "required" in r.get_json()["error"]


def test_whitespace_only_name_rejected(app_with_bots):
    """An all-whitespace name is empty after trim."""
    client = app_with_bots.test_client()
    r = client.post("/api/bots/evolve/rename", json={"name": "   "})
    assert r.status_code == 400


def test_overlong_name_rejected(app_with_bots):
    client = app_with_bots.test_client()
    r = client.post("/api/bots/evolve/rename", json={"name": "x" * 100})
    assert r.status_code == 400
    assert "too long" in r.get_json()["error"]


def test_control_chars_rejected(app_with_bots):
    """A newline or tab in the name would render badly across surfaces
    and is almost certainly an accidental paste."""
    client = app_with_bots.test_client()
    r = client.post(
        "/api/bots/evolve/rename",
        json={"name": "Evo\nNewline"},
    )
    assert r.status_code == 400
    assert "control" in r.get_json()["error"]


def test_non_string_name_rejected(app_with_bots):
    client = app_with_bots.test_client()
    r = client.post("/api/bots/evolve/rename", json={"name": 42})
    assert r.status_code == 400


def test_missing_body_rejected(app_with_bots):
    client = app_with_bots.test_client()
    r = client.post("/api/bots/evolve/rename")
    assert r.status_code == 400


# ── Bot existence ──────────────────────────────────────────────────────────


def test_unknown_bot_returns_404(app_with_bots):
    """Whitelist the bot_id against network.bots — prevents accidental
    rename of non-existent ids (and matches the pattern Bundle 3-part-2
    set for /api/primary/state/* routes)."""
    client = app_with_bots.test_client()
    r = client.post("/api/bots/nonexistent/rename", json={"name": "Anything"})
    assert r.status_code == 404


def test_oc_failure_returns_502_and_no_cache_write(app_with_bots):
    """Upstream OC failed (subprocess returned None). Do NOT cache the
    name — otherwise the UI would show a name OC didn't accept."""
    client = app_with_bots.test_client()

    with patch("oc_cli.oc_command_raw", return_value=None):
        r = client.post(
            "/api/bots/evolve/rename",
            json={"name": "Evo"},
        )

    assert r.status_code == 502
    # Cache untouched.
    net = _read_network(app_with_bots)
    assert "display_name" not in net["bots"]["evolve"]


# ── Repeated rename ──────────────────────────────────────────────────────


def test_repeated_rename_updates_cache(app_with_bots):
    """A second rename overwrites the previous display_name."""
    client = app_with_bots.test_client()

    with patch("oc_cli.oc_command_raw", return_value="ok"):
        r1 = client.post("/api/bots/evolve/rename", json={"name": "Evo"})
        r2 = client.post("/api/bots/evolve/rename", json={"name": "Evolution"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    net = _read_network(app_with_bots)
    assert net["bots"]["evolve"]["display_name"] == "Evolution"


# ── Defensive paths (caught in second-pass review) ──────────────────────────


def test_rename_repairs_none_bot_entry(tmp_path):
    """Legacy network.json sometimes has ``"bots": {"team_bot_a": null}`` —
    member listed but no entry. The 404 guard passes (key IS in dict)
    so the route reaches the cache write; the defensive repair turns
    the None into ``{"display_name": ...}`` rather than crashing
    save_network.

    Regression for an untested defensive path flagged in second-pass
    review.
    """
    from evolve_admin.web.server import create_app

    network = {
        "sharedDir": str(tmp_path),
        "primary": "evolve",
        "members": ["evolve", "legacy"],
        "bots": {
            "evolve": {"role": "primary"},
            "legacy": None,  # legacy shape, present but null
        },
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    app = create_app(network_path=network_path)
    app.config["TESTING"] = True
    client = app.test_client()

    with patch("oc_cli.oc_command_raw", return_value="ok"):
        r = client.post("/api/bots/legacy/rename", json={"name": "Legacy"})

    assert r.status_code == 200
    net = json.loads(network_path.read_text())
    assert net["bots"]["legacy"] == {"display_name": "Legacy"}


def test_save_network_failure_returns_partial_state(app_with_bots):
    """OC accepted the new name but persisting to network.json
    failed. The route must surface oc_applied=true so the UI can
    distinguish this from a clean failure and prompt the user to
    re-run. Regression for a silent-drift failure mode flagged in
    second-pass review.
    """
    client = app_with_bots.test_client()

    with patch("oc_cli.oc_command_raw", return_value="ok"), \
         patch(
             "evolve_admin.web.routes_bot_config.save_network",
             side_effect=OSError("permission denied"),
         ):
        r = client.post("/api/bots/evolve/rename", json={"name": "Evo"})

    assert r.status_code == 500
    body = r.get_json()
    assert body["oc_applied"] is True
    assert body["cache_persisted"] is False
    assert body["display_name"] == "Evo"
