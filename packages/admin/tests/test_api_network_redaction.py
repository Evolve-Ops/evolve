"""/api/network must not ship plaintext secrets (roadmap item 0.4).

Before this change the route returned the raw network.json via ``jsonify(net)``,
leaking ``github.pat`` (and any channel botTokens / gateway auth tokens) to any
client that could reach the loopback admin server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).parent.parent
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


# ── unit: the redactor ─────────────────────────────────────────────────────────


def test_redactor_masks_secrets_preserves_refs():
    from evolve_admin.web.server import _redact_secrets

    src = {
        "github": {"pat": "ghp_supersecret", "login": "octocat"},
        "intake": {"github": {"token_slot": "github_intake", "owner": "me"}},
        "channels": {"telegram": {"botToken": "12345:AAbbCC", "chatId": "777"}},
        "bots": {"team_bot_a": {"backupRepoUrl": "git@github.com:me/team_bot_a.git"}},
        "emptySecret": "",
    }
    out = _redact_secrets(src)

    assert out["github"]["pat"] == "[REDACTED]"
    assert out["channels"]["telegram"]["botToken"] == "[REDACTED]"
    # references and non-secrets are untouched
    assert out["github"]["login"] == "octocat"
    assert out["intake"]["github"]["token_slot"] == "github_intake"
    assert out["channels"]["telegram"]["chatId"] == "777"
    assert out["bots"]["team_bot_a"]["backupRepoUrl"] == "git@github.com:me/team_bot_a.git"
    # input not mutated; empty secret stays falsy (set-vs-unset preserved)
    assert src["github"]["pat"] == "ghp_supersecret"
    assert out["emptySecret"] == ""


# ── route: end-to-end ──────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    from evolve_admin.web.server import create_app

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a", "backupRepoUrl": "git@github.com:me/team_bot_a.git"}},
        "sharedDir": str(tmp_path / "shared"),
        "github": {"pat": "ghp_THIS_MUST_NOT_LEAK", "login": "octocat"},
        "channels": {"telegram": {"botToken": "999:SECRET_BOT_TOKEN", "chatId": "1"}},
    }
    net_file = tmp_path / "network.json"
    net_file.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    app = create_app(network_path=net_file)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_api_network_does_not_leak_secrets(client):
    resp = client.get("/api/network")
    assert resp.status_code == 200
    raw = resp.get_data(as_text=True)
    assert "ghp_THIS_MUST_NOT_LEAK" not in raw
    assert "SECRET_BOT_TOKEN" not in raw

    data = resp.get_json()
    # The 2.8 startup migration moves github.pat to the keystore and
    # scrubs the plaintext slot — the response carries no pat at all
    # (stronger than redaction). The redactor stays as the safety net
    # for the other token-bearing fields.
    assert data["github"].get("pat") in (None, "")
    assert data["channels"]["telegram"]["botToken"] == "[REDACTED]"
    # non-secret fields still present so the UI keeps working
    assert data["github"]["login"] == "octocat"
    assert data["bots"]["team_bot_a"]["backupRepoUrl"] == "git@github.com:me/team_bot_a.git"


def test_startup_migration_moved_pat_to_keystore(client, tmp_path):
    """The fixture seeded a plaintext github.pat; create_app's 2.8
    migration must have moved it into the keystore and scrubbed the file."""
    import json as _json
    net = _json.loads((tmp_path / "network.json").read_text())
    assert "ghp_THIS_MUST_NOT_LEAK" not in (tmp_path / "network.json").read_text()
    assert net.get("github", {}).get("pat") in (None, "")

    from evolve_admin.keystore import load_github_pat
    assert load_github_pat(tmp_path / "shared") == "ghp_THIS_MUST_NOT_LEAK"
