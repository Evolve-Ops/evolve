"""tests/test_backup_bot_routes.py — the verdict endpoint gives a verdict, never a token.

``/api/backup-bot/visibility`` exists so a bot-user backup daemon can learn
whether its backup repo is private WITHOUT reading the pod GitHub PAT. The whole
security value of the change rests on one property: the PAT cannot leave this
endpoint, by any argument, on any branch.

These tests walk every reachable branch and assert a token-shaped string never
appears in the response, then pin the two identity bindings (which bot is
asking, and which repo gets checked) that stop the endpoint becoming a
general-purpose GitHub oracle for a compromised bot.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import sys
from pathlib import Path

from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.keystore import SecretState
from evolve_admin.web.backup_bot_routes import register_backup_bot_routes

_ME = pwd.getpwuid(os.getuid()).pw_name
_MY_URL = "git@github.com:cjalden/lex-workspace.git"
_OTHER_URL = "git@github.com:cjalden/someone-elses-private-repo.git"

# The PAT shapes GitHub issues. If any of these turns up in a response body the
# endpoint has failed at its one job.
_TOKEN_RE = re.compile(r"gh[pousr]_[A-Za-z0-9]{4,}|github_pat_[A-Za-z0-9_]{4,}")
# ASSEMBLED AT RUNTIME, deliberately. The fixture has to be full length to be a
# fair stand-in for a real PAT, but a literal `ghp_` + 36 chars in a source file
# is exactly what the repo's gitleaks scan exists to catch — and it cannot tell
# a test fixture from the real thing, which is the correct behaviour. Building
# it from parts keeps the scan honest without an allowlist entry; an allowlist
# is a permanent hole opened for a temporary convenience.
_FAKE_PAT = "ghp_" + "N0tARealToken" + "0" * 23


def _write_network(tmp_path: Path, *, url: str | None = _MY_URL) -> Path:
    lex = {"role": "member", "user": _ME}
    if url:
        lex["backupRepoUrl"] = url
    net = {
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "lex": lex,
            "rex": {"role": "member", "user": "no_such_account_xyz",
                    "backupRepoUrl": _OTHER_URL},
        },
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def _app(network_path: Path) -> Flask:
    app = Flask(__name__)
    register_backup_bot_routes(app, network_path)
    return app


def _unix_env(uid: int | None = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


def _post(app, body=None, env=None):
    return app.test_client().post(
        "/api/backup-bot/visibility",
        json=body if body is not None else {},
        environ_base=env if env is not None else _unix_env(),
    )


def _stub_pat(monkeypatch, state=SecretState.PRESENT, value=_FAKE_PAT):
    monkeypatch.setattr(
        "evolve_admin.keystore.load_github_pat_state", lambda shared: (state, value),
    )


# ── The load-bearing property: no token, on any branch ──────────────────────

def test_no_branch_of_the_endpoint_returns_a_token_shaped_string(tmp_path, monkeypatch):
    """Sweep every reachable outcome; none may echo a PAT.

    Includes the branch where the lookup itself raises — the tempting place to
    return ``str(exc)``, which can quote a remote URL carrying an embedded
    credential (the 2026-07-28 incident shape).
    """
    network_path = _write_network(tmp_path)
    app = _app(network_path)

    def _boom(*a, **k):
        raise RuntimeError(f"connection failed using token {_FAKE_PAT}")

    cases = [
        ("private verdict", SecretState.PRESENT, lambda u, pat=None: "private", {}),
        ("public verdict", SecretState.PRESENT, lambda u, pat=None: "public", {}),
        ("unknown verdict", SecretState.PRESENT, lambda u, pat=None: "unknown", {}),
        ("lookup raises with a token in the message",
         SecretState.PRESENT, _boom, {}),
        ("pat absent", SecretState.ABSENT, lambda u, pat=None: "private", {}),
        ("pat unreadable", SecretState.UNREADABLE, lambda u, pat=None: "private", {}),
        ("url mismatch", SecretState.PRESENT, lambda u, pat=None: "private",
         {"repoUrl": _OTHER_URL}),
    ]
    for label, state, checker, body in cases:
        _stub_pat(monkeypatch, state, _FAKE_PAT if state is SecretState.PRESENT else None)
        monkeypatch.setattr("backup_visibility.check_repo_visibility", checker)
        r = _post(app, body)
        raw = r.get_data(as_text=True)
        assert not _TOKEN_RE.search(raw), (
            f"[{label}] a token-shaped string reached the response body — the "
            f"endpoint's entire purpose is that the PAT never leaves it: {raw}"
        )
        assert _FAKE_PAT not in raw, f"[{label}] the PAT itself leaked: {raw}"
        # Not a vacuous pass: every case must actually reach a verdict body.
        assert set(r.get_json()) == {"visibility", "reason", "repo_url"}, (
            f"[{label}] did not reach the verdict path at all: {raw}"
        )

    # And specifically: the raising lookup must be caught and reported as a
    # reason code, NOT as str(exc) — which held a token in this fixture.
    _stub_pat(monkeypatch)
    monkeypatch.setattr("backup_visibility.check_repo_visibility", _boom)
    body = _post(app).get_json()
    assert body["reason"] == "github-lookup-failed"
    assert body["visibility"] == "unknown"


def test_response_keys_are_exactly_the_verdict_triple(tmp_path, monkeypatch):
    """No spare keys — a response that grows fields grows a leak surface."""
    app = _app(_write_network(tmp_path))
    _stub_pat(monkeypatch)
    monkeypatch.setattr(
        "backup_visibility.check_repo_visibility", lambda u, pat=None: "private")
    r = _post(app)
    assert r.status_code == 200
    assert set(r.get_json()) == {"visibility", "reason", "repo_url"}


# ── Identity binding: which bot is asking ───────────────────────────────────

def test_tcp_caller_is_refused(tmp_path, monkeypatch):
    """A browser session must never spend the pod PAT."""
    app = _app(_write_network(tmp_path))
    _stub_pat(monkeypatch)
    r = _post(app, env={"REMOTE_TRANSPORT": "tcp"})
    assert r.status_code == 403


def test_unknown_peer_uid_is_refused(tmp_path, monkeypatch):
    app = _app(_write_network(tmp_path))
    _stub_pat(monkeypatch)
    r = _post(app, env=_unix_env(uid=999999))
    assert r.status_code == 403


# ── Identity binding: which repo gets checked ───────────────────────────────

def test_body_url_cannot_redirect_the_check_to_another_repo(tmp_path, monkeypatch):
    """The oracle guard.

    A compromised bot must not be able to spend the pod's PAT enumerating the
    visibility of repos it has nothing to do with. The endpoint checks the
    CALLER's configured backupRepoUrl and nothing else; a mismatched body URL
    is reported, never followed.
    """
    app = _app(_write_network(tmp_path))
    _stub_pat(monkeypatch)

    checked: list[str] = []

    def spy(url, pat=None):
        checked.append(url)
        return "private"

    monkeypatch.setattr("backup_visibility.check_repo_visibility", spy)
    r = _post(app, {"repoUrl": _OTHER_URL})

    assert checked == [], (
        f"the endpoint performed a GitHub lookup against a caller-supplied "
        f"URL ({checked}) — that is a private-repo existence oracle backed by "
        f"the pod's PAT"
    )
    body = r.get_json()
    assert body["visibility"] == "unknown"
    assert body["reason"] == "requested-url-is-not-this-bot-s-configured-backup-repo"
    assert body["repo_url"] == _MY_URL


def test_matching_body_url_is_checked_normally(tmp_path, monkeypatch):
    app = _app(_write_network(tmp_path))
    _stub_pat(monkeypatch)
    monkeypatch.setattr(
        "backup_visibility.check_repo_visibility", lambda u, pat=None: "private")
    body = _post(app, {"repoUrl": _MY_URL}).get_json()
    assert body["visibility"] == "private"
    assert body["repo_url"] == _MY_URL


def test_bot_with_no_backup_url_gets_unknown(tmp_path, monkeypatch):
    app = _app(_write_network(tmp_path, url=None))
    _stub_pat(monkeypatch)
    body = _post(app).get_json()
    assert body["visibility"] == "unknown"
    assert body["reason"] == "no-backup-url-configured"


# ── PAT state is reported as a reason, never as a value ─────────────────────

def test_absent_and_unreadable_pat_are_distinguishable_to_the_caller(tmp_path, monkeypatch):
    """The two states need different operator remediations; keep them distinct.

    Collapsing them here would re-create the original defect one layer up.
    """
    app = _app(_write_network(tmp_path))

    _stub_pat(monkeypatch, SecretState.ABSENT, None)
    absent = _post(app).get_json()

    _stub_pat(monkeypatch, SecretState.UNREADABLE, None)
    unreadable = _post(app).get_json()

    assert absent["reason"] == "no-pat-stored"
    assert unreadable["reason"] == "pat-unreadable-by-admin-daemon"
    assert absent["reason"] != unreadable["reason"]
    assert absent["visibility"] == unreadable["visibility"] == "unknown"


# ── Registration ────────────────────────────────────────────────────────────

def test_route_is_in_the_peer_bot_exempt_list():
    """Without the exemption the device-auth gate 401s the bot before the route.

    The endpoint would be unreachable from the exact caller it exists for, and
    the backup daemon would fall through to 'failed' every night.
    """
    from evolve_admin.web.peer_auth import _PEER_BOT_ROUTE_EXACT
    assert "/api/backup-bot/visibility" in _PEER_BOT_ROUTE_EXACT
