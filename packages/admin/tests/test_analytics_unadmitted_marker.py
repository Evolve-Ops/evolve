"""Pod-wide "not admitted" marker on the Usage By-User report.

A user is ``unadmitted`` iff they are admitted to NO bot anywhere — not in any
bot's allowFrom, not a pod admin, not a bot's primary owner. The marker is
pod-wide because the Usage report is pod-wide while admission is per-bot, so a
user admitted to even one bot must clear it everywhere.

We stub load_turns and patch the allowFrom reader so the admission index is
deterministic and touches no real files. Placeholder ids/names only.
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

from evolve_admin.web.server import _register_analytics_routes  # noqa: E402


def _turn(*, user_id, channel="C0FAKECHAN", instance="bot_one",
          source="human", ts="2026-06-12T10:00:00Z", session_id="s1"):
    return {
        "ts": ts, "instance": instance, "model": "provx/used-lo",
        "provider": "provx", "source": source, "channel": channel,
        "user_id": user_id, "cost": 0.5, "input_tokens": 5,
        "output_tokens": 200, "cache_read_tokens": 10000,
        "cache_write_tokens": 0, "session_id": session_id, "auth_mode": "api_key",
    }


# Admitted-somewhere (must NOT be marked):
#   U0ADMIN001 — pod admin           U0OWNER01 — bot_one primary owner
#   U0ALLOW001 — bot_one allowFrom   U0ALLOW002 — bot_two allowFrom
# Admitted nowhere (MUST be marked): U0OUTSIDE1 (slack), 5550001 (telegram)
_POOLED = [
    _turn(user_id="U0ADMIN001", session_id="a1"),
    _turn(user_id="U0OWNER01", session_id="a2"),
    _turn(user_id="U0ALLOW001", session_id="a3"),
    _turn(user_id="U0ALLOW002", instance="bot_two", session_id="a4"),
    _turn(user_id="U0OUTSIDE1", session_id="a5"),
    _turn(user_id="5550001", channel="5550001", session_id="a6"),
]

# Fake per-bot allowFrom membership, keyed (bot_id, channel) → list | None.
# Telegram is readable (present, with one admitted chat) so a telegram user
# NOT in it can be confidently marked — the marker gates per-platform on a real
# allowFrom read.
_ALLOW = {
    ("bot_one", "slack"): ["U0ALLOW001"],
    ("bot_two", "slack"): ["U0ALLOW002"],
    ("bot_one", "telegram"): ["5551234"],
}


@pytest.fixture
def app(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "bot_one",
        "bots": {
            "bot_one": {
                "user": "bot_one",
                "primary_user": {"external_ids": {"slack": "U0OWNER01"}},
            },
            "bot_two": {"user": "bot_two"},
        },
        "pod": {"admins": {"external_ids": {"slack": ["U0ADMIN001"]}}},
    }))

    import usage_analytics as ua
    monkeypatch.setattr(ua, "load_turns", lambda bot_id, **kw: list(_POOLED))

    # Deterministic allowFrom — patched on roster_resolver because
    # _pod_admitted_index imports the reader from there at call time.
    import evolve_admin.roster_resolver as rr

    def _fake_reader(network, bot_id):
        return lambda channel: _ALLOW.get((bot_id, channel))

    monkeypatch.setattr(rr, "_default_allowfrom_reader", _fake_reader)

    # Cache-only render + no real warm work.
    import evolve_admin.evo.name_resolver as nr
    import evolve_admin.evo.conversation_resolver as cr
    import evolve_admin.evo.user_resolver as ur

    def _boom(*a, **k):
        raise AssertionError("render must be cache-only")

    monkeypatch.setattr(nr, "_http_get_json", _boom)
    monkeypatch.setattr(cr, "warm_conversation_cache", lambda *a, **k: False)
    monkeypatch.setattr(ur, "warm_user_cache", lambda *a, **k: False)

    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def _by_user(app):
    with app.test_client() as c:
        resp = c.get("/api/analytics/usage?days=7")
    assert resp.status_code == 200
    return {r["user_id"]: r for r in resp.get_json()["by_user"]}


def test_admitted_users_are_not_marked(app):
    rows = _by_user(app)
    for uid in ("U0ADMIN001", "U0OWNER01", "U0ALLOW001", "U0ALLOW002"):
        assert rows[uid]["unadmitted"] is False, uid


def test_unadmitted_users_are_marked(app):
    rows = _by_user(app)
    assert rows["U0OUTSIDE1"]["unadmitted"] is True
    assert rows["U0OUTSIDE1"]["platform"] == "slack"


def test_unadmitted_marker_is_pod_wide_across_platforms(app):
    """A Telegram user admitted to no bot is marked too — the index spans all
    roster channels, not just Slack."""
    rows = _by_user(app)
    tg = rows["5550001"]
    assert tg["platform"] == "telegram"
    assert tg["unadmitted"] is True


def test_marker_suppressed_when_allowfrom_unreadable_despite_admins(
        tmp_path, monkeypatch):
    """Regression for the partial-readability false positive: pod admins ARE
    configured (network.json always readable) but every allowFrom read fails
    (e.g. a dormant sudo grant). The marker must be SUPPRESSED for plain
    participants on that platform — pod-admin presence alone must not enable
    marking, or every allowFrom-admitted user would be falsely flagged."""
    shared = tmp_path / "shared"
    shared.mkdir()
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "sharedDir": str(shared),
        "primary": "bot_one",
        "bots": {"bot_one": {"user": "bot_one"}},
        "pod": {"admins": {"external_ids": {"slack": ["U0ADMIN001"]}}},
    }))

    import usage_analytics as ua
    monkeypatch.setattr(ua, "load_turns", lambda bot_id, **kw: [
        _turn(user_id="U0ADMIN001", session_id="b1"),
        _turn(user_id="U0PLAIN01", session_id="b2"),
    ])

    import evolve_admin.roster_resolver as rr
    # Every allowFrom read fails (None) — the dormant-grant scenario.
    monkeypatch.setattr(
        rr, "_default_allowfrom_reader",
        lambda network, bot_id: (lambda channel: None))

    import evolve_admin.evo.name_resolver as nr
    import evolve_admin.evo.conversation_resolver as cr
    import evolve_admin.evo.user_resolver as ur
    monkeypatch.setattr(
        nr, "_http_get_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache-only")))
    monkeypatch.setattr(cr, "warm_conversation_cache", lambda *a, **k: False)
    monkeypatch.setattr(ur, "warm_user_cache", lambda *a, **k: False)

    a = Flask(__name__)
    _register_analytics_routes(a, network_path)
    a.config["TESTING"] = True
    rows = _by_user(a)
    # slack allowFrom unreadable → slack not markable → NO false positive,
    # even though U0PLAIN01 is admitted nowhere we can see.
    assert rows["U0PLAIN01"]["unadmitted"] is False
    assert rows["U0ADMIN001"]["unadmitted"] is False
