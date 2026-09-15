"""tests/test_backup_verdict_roundtrip.py — the two halves, wired together.

``test_backup_bot_routes`` pins the server half and
``test_backup_pat_unreadable`` pins the bot half, each against a stub of the
other. This joins them: a REAL Flask app serving the real route, driven by the
real client transport in ``backup_visibility.visibility_via_daemon``, with only
the unix socket itself replaced by Flask's test client.

What it proves that neither half can alone: the client's parsing of
``{visibility, reason, repo_url}`` matches what the route actually emits, and
the ``repo_url`` cross-check fires on a real response body rather than a
hand-written dict. A field rename on either side breaks this test.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

from flask import Flask

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

import backup_visibility as bv
from evolve_admin.keystore import SecretState
from evolve_admin.web.backup_bot_routes import register_backup_bot_routes

_ME = pwd.getpwuid(os.getuid()).pw_name
_MY_URL = "git@github.com:cjalden/lex-workspace.git"


def _network(tmp_path: Path) -> Path:
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"lex": {"role": "member", "user": _ME,
                         "backupRepoUrl": _MY_URL}},
    }))
    return p


def _poster_for(app: Flask):
    """A ``post_json``-shaped callable backed by Flask's test client.

    Stands in for the unix-socket transport: same (path, body) in, same
    ``(status, parsed_json)`` out.
    """
    client = app.test_client()

    def post_json(path, body, timeout=None):
        r = client.post(path, json=body, environ_base={
            "REMOTE_TRANSPORT": "unix-socket", "REMOTE_PEER_UID": os.getuid(),
        })
        return r.status_code, r.get_json()

    return post_json


def _app(tmp_path):
    app = Flask(__name__)
    register_backup_bot_routes(app, _network(tmp_path))
    return app


def test_private_verdict_round_trips(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.keystore.load_github_pat_state",
        lambda shared: (SecretState.PRESENT, "ghp_fake"),
    )
    monkeypatch.setattr(
        "backup_visibility.check_repo_visibility", lambda u, pat=None: "private")

    answer = bv.visibility_via_daemon(_MY_URL, _poster=_poster_for(app))
    assert answer is not None, "a served endpoint must not read as 'unavailable'"
    verdict, reason = answer
    assert verdict == "private"
    assert reason == "checked"


def test_public_verdict_round_trips(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.keystore.load_github_pat_state",
        lambda shared: (SecretState.PRESENT, "ghp_fake"),
    )
    monkeypatch.setattr(
        "backup_visibility.check_repo_visibility", lambda u, pat=None: "public")
    verdict, _reason = bv.visibility_via_daemon(_MY_URL, _poster=_poster_for(app))
    assert verdict == "public"


def test_client_refuses_a_verdict_about_a_different_repo(tmp_path, monkeypatch):
    """The cross-check, against a real response body.

    The route answers about the CALLER's configured repo. If the bot is about
    to push somewhere else, that verdict does not cover it and must not be
    borrowed — fail closed instead.
    """
    app = _app(tmp_path)
    monkeypatch.setattr(
        "evolve_admin.keystore.load_github_pat_state",
        lambda shared: (SecretState.PRESENT, "ghp_fake"),
    )
    monkeypatch.setattr(
        "backup_visibility.check_repo_visibility", lambda u, pat=None: "private")

    verdict, reason = bv.visibility_via_daemon(
        "git@github.com:cjalden/a-different-repo.git", _poster=_poster_for(app),
    )
    assert verdict == "unknown"
    assert reason in (
        "daemon-checked-a-different-repo",
        # The route also refuses outright when the body URL disagrees; either
        # refusal is correct, and both are fail-closed.
        "requested-url-is-not-this-bot-s-configured-backup-repo",
    )


def test_unreachable_daemon_reads_as_unavailable_not_as_a_verdict(tmp_path):
    """``None`` (fall back) must be distinguishable from ``unknown`` (answered).

    backup.py branches on exactly this: ``None`` means "no verdict route
    exists", which with an unreadable PAT is the failure that names the socket.
    """
    def dead(path, body, timeout=None):
        raise OSError("connect: no such file or directory")

    assert bv.visibility_via_daemon(_MY_URL, _poster=dead) is None


def test_403_is_an_answer_and_fails_closed(tmp_path):
    """A daemon that refuses the peer answered; we must not fall back to a
    direct-PAT path we already know we cannot take."""
    def refuse(path, body, timeout=None):
        return 403, {"error": "caller not a recognized bot"}

    verdict, reason = bv.visibility_via_daemon(_MY_URL, _poster=refuse)
    assert verdict == "unknown"
    assert "403" in reason


def test_404_means_the_daemon_predates_this_endpoint(tmp_path):
    """A partially-rolled-out fleet must keep using the direct-PAT path."""
    def old(path, body, timeout=None):
        return 404, None

    assert bv.visibility_via_daemon(_MY_URL, _poster=old) is None
