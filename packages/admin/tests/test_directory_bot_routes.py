"""Tests for the bot-facing user-directory routes — /api/directory/{lookup,digest}.

Spec: internal/spec-user-directory-2026-06-22.md §5 (read tool + per-turn digest) + §10
invariants. The security-critical surface: the calling bot is bound server-side from the
unix-socket kernel peer uid, NEVER from a request field (same primitive as
google_bot_routes). These tests assert, end-to-end through the route:

  (a) identity binding   — TCP / unknown-uid → 403; a body that claims another bot is
      ignored (the peer uid wins);
  (b) cross-bot isolation — a ref that names a person in ANOTHER bot's directory resolves
      to null, never that bot's data;
  (c) one read path       — lookup/digest equal the bot_view projection of
      resolve_person / resolve_persons (admin and bot cannot diverge — invariant #1);
  (d) no profile leak     — the behavioral-profile link + audit trail never appear in a
      bot-facing payload (invariant #5).

Uses Flask's test client with manually-set request.environ (REMOTE_TRANSPORT /
REMOTE_PEER_UID) to simulate unix-socket peer credentials — same technique as
test_google_bot_routes / test_peer_auth. The bot's macOS user is the current test user,
so the real peer_auth resolver maps os.getuid() back to that bot (the binding is exercised
end-to-end, not mocked).

FAKE ids + ``*.example`` / ``example.net`` emails only (docs/PLACEHOLDER_NAMING.md).
"""
from __future__ import annotations

import json
import os
import pwd
import sys
from pathlib import Path

import pytest
from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.web import routes_bot_users as rbu  # noqa: E402
from evolve_admin.web.directory_bot_routes import (  # noqa: E402
    DIGEST_CAP,
    register_directory_bot_routes,
)
from evolve_admin.config import load_network  # noqa: E402
from evolve_admin.user_directory import bot_view  # noqa: E402
from evolve_admin.user_directory import resolver as udr  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402


_ME = pwd.getpwuid(os.getuid()).pw_name
# This bot's account is the current test user (so its uid maps back to it); the
# "other" bot is unprovisioned (skipped in the uid map — no collision).
BOT = "lex"
OTHER = "rex"


def _write_network(tmp_path: Path) -> Path:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            BOT: {"role": "member", "user": _ME},
            OTHER: {"role": "member", "user": "no_such_account_xyz"},
        },
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}, "names": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(net))
    return p


def _shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def app(tmp_path, monkeypatch):
    network_path = _write_network(tmp_path)
    # Keep any roster allowFrom reads inside tmp (so resolve_persons doesn't touch the
    # current user's real ~/.openclaw). Empty creds ⇒ directory entries resolve as
    # contacts unless we write an allowFrom file.
    monkeypatch.setattr(rbu, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    a = Flask(__name__)
    register_directory_bot_routes(a, network_path)
    a.config["TESTING"] = True
    a._evolve_network_path = network_path  # stash for tests that re-resolve
    return a


def _unix_env(uid: int | None = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


def _seed_contact(shared: Path, bot: str, platform: str, stable_id: str, **fields):
    uds.upsert_entry(shared, bot, platform, stable_id,
                     by=bot, provenance="bot-asserted", **fields)


def _write_allowfrom(tmp_path: Path, bot: str, channel: str, ids: list[str]):
    creds = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    # allowFrom file shape is {"allowFrom": [...]} (see roster_resolver reader).
    (creds / f"{channel}-default-allowFrom.json").write_text(
        json.dumps({"allowFrom": ids}))


def _write_overlay(tmp_path: Path, bot: str, overlay: dict):
    rosters = tmp_path / "shared" / "rosters"
    rosters.mkdir(parents=True, exist_ok=True)
    (rosters / f"{bot}.json").write_text(json.dumps(overlay))


# ── (a) identity binding ──────────────────────────────────────────────────────


def test_lookup_403_for_tcp(app):
    with app.test_client() as c:
        resp = c.post("/api/directory/lookup", json={"ref": "x"},
                      environ_overrides={"REMOTE_PEER_UID": os.getuid()})  # no transport
    assert resp.status_code == 403


def test_lookup_403_for_unknown_uid(app):
    with app.test_client() as c:
        resp = c.post("/api/directory/lookup", json={"ref": "x"},
                      environ_overrides=_unix_env(uid=4242424))
    assert resp.status_code == 403


def test_digest_403_for_tcp(app):
    with app.test_client() as c:
        resp = c.get("/api/directory/digest",
                     environ_overrides={"REMOTE_PEER_UID": os.getuid()})
    assert resp.status_code == 403


def test_lookup_missing_ref_400(app):
    with app.test_client() as c:
        resp = c.post("/api/directory/lookup", json={}, environ_overrides=_unix_env())
    assert resp.status_code == 400


def test_lookup_ignores_body_bot(app, tmp_path):
    """A body that claims bot=rex is ignored — the lookup runs against LEX (peer uid),
    so rex's contact does NOT resolve."""
    shared = _shared(tmp_path)
    _seed_contact(shared, OTHER, "email", "evil@acme.example",
                  emails=[{"addr": "evil@acme.example", "rank": "primary"}])
    with app.test_client() as c:
        resp = c.post("/api/directory/lookup",
                      json={"ref": "evil@acme.example", "bot": OTHER},
                      environ_overrides=_unix_env())
    assert resp.status_code == 200
    assert resp.get_json()["person"] is None  # rex's data never reachable as lex


# ── (b) cross-bot isolation ────────────────────────────────────────────────────


def test_lookup_cross_bot_returns_null(app, tmp_path):
    """A contact that exists ONLY in another bot's directory resolves to null for the
    caller — the lookup is scoped to the caller's own directory by construction."""
    shared = _shared(tmp_path)
    _seed_contact(shared, OTHER, "email", "dana@acme.example",
                  emails=[{"addr": "dana@acme.example", "rank": "primary"}],
                  names={"display": "Dana Lopez"})
    with app.test_client() as c:
        for ref in ("dana@acme.example", "Dana Lopez"):
            resp = c.post("/api/directory/lookup", json={"ref": ref},
                          environ_overrides=_unix_env())
            assert resp.status_code == 200
            assert resp.get_json()["person"] is None, ref


def test_digest_is_per_bot(app, tmp_path):
    """Lex's digest contains lex's contacts and NOT rex's."""
    shared = _shared(tmp_path)
    _seed_contact(shared, BOT, "email", "mine@acme.example",
                  emails=[{"addr": "mine@acme.example", "rank": "primary"}],
                  names={"display": "Mine Contact"})
    _seed_contact(shared, OTHER, "email", "theirs@acme.example",
                  emails=[{"addr": "theirs@acme.example", "rank": "primary"}],
                  names={"display": "Theirs Contact"})
    with app.test_client() as c:
        body = c.get("/api/directory/digest", environ_overrides=_unix_env()).get_json()
    displays = [r["display"] for r in body["persons"]]
    assert "Mine Contact" in displays
    assert "Theirs Contact" not in displays


# ── lookup resolution: by handle / name / email / id; unknown → null ───────────


def test_lookup_by_email_name_handle(app, tmp_path):
    shared = _shared(tmp_path)
    _seed_contact(
        shared, BOT, "telegram", "70000001",
        emails=[{"addr": "dana@example.net", "rank": "primary"}],
        names={"display": "Dana Lopez"},
        identities=[{"platform": "telegram", "id": "70000001", "handle": "@dana"}])
    with app.test_client() as c:
        for ref in ("dana@example.net", "Dana Lopez", "@dana"):
            resp = c.post("/api/directory/lookup", json={"ref": ref},
                          environ_overrides=_unix_env())
            assert resp.status_code == 200, ref
            person = resp.get_json()["person"]
            assert person is not None, ref
            assert person["names"]["display"] == "Dana Lopez", ref


def test_lookup_unknown_ref_returns_null(app):
    with app.test_client() as c:
        resp = c.post("/api/directory/lookup", json={"ref": "@nobody"},
                      environ_overrides=_unix_env())
    assert resp.status_code == 200
    assert resp.get_json()["person"] is None


# ── (c) one read path: lookup/digest == bot_view of resolve_* ──────────────────


def test_lookup_coheres_with_resolve_person(app, tmp_path):
    shared = _shared(tmp_path)
    _seed_contact(
        shared, BOT, "email", "dana@acme.example",
        emails=[{"addr": "dana@acme.example", "rank": "primary"}],
        names={"display": "Dana Lopez"})
    net = load_network(app._evolve_network_path)
    expected = bot_view.person_to_bot_dict(
        udr.resolve_person(net, BOT, "dana@acme.example"))
    with app.test_client() as c:
        got = c.post("/api/directory/lookup", json={"ref": "dana@acme.example"},
                     environ_overrides=_unix_env()).get_json()["person"]
    assert got == expected


def test_digest_coheres_with_resolve_persons(app, tmp_path):
    shared = _shared(tmp_path)
    for i in range(3):
        _seed_contact(
            shared, BOT, "email", f"c{i}@acme.example",
            emails=[{"addr": f"c{i}@acme.example", "rank": "primary"}],
            names={"display": f"Contact {i}"})
    net = load_network(app._evolve_network_path)
    expected = bot_view.build_digest(
        udr.resolve_persons(net, BOT), cap=DIGEST_CAP)
    with app.test_client() as c:
        got = c.get("/api/directory/digest", environ_overrides=_unix_env()).get_json()
    assert got == expected
    assert got["cap"] == DIGEST_CAP
    assert got["total"] == 3


# ── (d) no profile leak (invariant #5) ─────────────────────────────────────────


def test_lookup_never_exposes_profile_ref_or_audit(app, tmp_path):
    """Even when the stored entry carries a profile_ref + audit trail, the bot-facing
    payload omits both — the behavioral profile is Phase-4-gated and audit holds operator
    PII history."""
    shared = _shared(tmp_path)
    _seed_contact(
        shared, BOT, "email", "dana@acme.example",
        emails=[{"addr": "dana@acme.example", "rank": "primary"}],
        names={"display": "Dana Lopez"},
        profile_ref=".openclaw/profiles/dana.md")
    # Sanity: the stored entry really does carry profile_ref + audit.
    entry = uds.get_entry(uds.load_directory(shared, BOT), "email", "dana@acme.example")
    assert entry["profile_ref"] == ".openclaw/profiles/dana.md"
    assert entry.get("audit")
    with app.test_client() as c:
        person = c.post("/api/directory/lookup", json={"ref": "dana@acme.example"},
                        environ_overrides=_unix_env()).get_json()["person"]
    assert "profile_ref" not in person
    assert "audit" not in person


# ── admitted-user path (membership/role end-to-end through the route) ──────────


def test_digest_includes_admitted_role(app, tmp_path):
    """An admitted user (in allowFrom) shows up with a real membership role; a pod admin
    (telegram 999) resolves to role=admin."""
    shared = _shared(tmp_path)
    _write_allowfrom(tmp_path, BOT, "telegram", ["999"])  # 999 is a pod admin
    with app.test_client() as c:
        body = c.get("/api/directory/digest", environ_overrides=_unix_env()).get_json()
    admin_rows = [r for r in body["persons"] if r["identity"] == "telegram:999"]
    assert admin_rows, body
    assert admin_rows[0]["role"] == "admin"


# ── blocked users are never advertised — lookup AND digest agree ───────────────


def test_lookup_suppresses_blocked_user(app, tmp_path):
    """A blocked user (in allowFrom but in the overlay block index — the out-of-sync
    window) resolves with role=blocked; the lookup route suppresses them to null so it
    can't expose a blocked person's identities/emails. Parity with the digest filter."""
    shared = _shared(tmp_path)
    _write_allowfrom(tmp_path, BOT, "telegram", ["70000099"])
    _write_overlay(tmp_path, BOT, {"blocked": {"telegram:70000099": {"by": "op"}}})
    # Give them a directory email so a leak would be observable if not suppressed.
    _seed_contact(shared, BOT, "telegram", "70000099",
                  emails=[{"addr": "blocked@acme.example", "rank": "primary"}],
                  names={"display": "Blocked User"})
    with app.test_client() as c:
        for ref in ("Blocked User", "blocked@acme.example", "70000099"):
            resp = c.post("/api/directory/lookup", json={"ref": ref},
                          environ_overrides=_unix_env())
            assert resp.status_code == 200, ref
            assert resp.get_json()["person"] is None, ref  # never the blocked person
        # …and the digest excludes them too (the two read paths agree).
        digest = c.get("/api/directory/digest",
                       environ_overrides=_unix_env()).get_json()
    assert all(r["identity"] != "telegram:70000099" for r in digest["persons"])
