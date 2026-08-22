"""Tests for the bot-facing WRITE route — POST /api/directory/upsert (Phase 3b).

Spec: docs/spec-user-directory-2026-06-22.md §5 item 2 + §10 invariants. This is the
privileged, PII-writing half of the bot-side integration, so the tests are abuse-case
shaped — they assert, end-to-end through the route:

  (a) identity binding    — TCP / unknown-uid → 403; a body that names another bot is
      ignored (the peer uid wins);
  (b) cross-bot isolation — a write always lands in the CALLER's own directory; a ref
      that names another bot's person never resolves and never writes there;
  (c) identity, never authority (invariant #2) — a body carrying membership/role/etc. is
      rejected; the write can only ever touch directory-owned fields;
  (d) provenance never forged / downgraded (invariant #3) — writes stamp bot-asserted; a
      bot can't claim operator-verified, and can't clobber an existing operator-verified
      email;
  (e) create-or-update    — an existing person is updated (no membership minted); a new
      contact is minted WITHOUT membership; a name-only ref with nothing to key on is a
      clean 422;
  (f) hot-path hygiene     — validation faults are clean 4xx, never a 500; the store-growth
      cap bounds runaway creation.

Harness mirrors test_directory_bot_routes.py: Flask test client with manually-set
request.environ (REMOTE_TRANSPORT / REMOTE_PEER_UID) to simulate the unix-socket peer
credential; the bot's macOS user is the current test user so the real peer_auth resolver
binds os.getuid() back to it (the binding is exercised, not mocked).

FAKE ids + ``*.example`` / ``example.net`` / ``*.test`` emails only (docs/PLACEHOLDER_NAMING.md).
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
from evolve_admin.web import directory_bot_routes as dbr  # noqa: E402
from evolve_admin.web.directory_bot_routes import (  # noqa: E402
    register_directory_bot_routes,
)
from evolve_admin.config import load_network  # noqa: E402
from evolve_admin.user_directory import resolver as udr  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402


_ME = pwd.getpwuid(os.getuid()).pw_name
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
    monkeypatch.setattr(rbu, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    a = Flask(__name__)
    register_directory_bot_routes(a, network_path)
    a.config["TESTING"] = True
    a._evolve_network_path = network_path
    return a


def _unix_env(uid: int | None = None) -> dict:
    return {"REMOTE_TRANSPORT": "unix-socket",
            "REMOTE_PEER_UID": os.getuid() if uid is None else uid}


def _post(c, body, **env):
    return c.post("/api/directory/upsert", json=body,
                  environ_overrides=_unix_env(**env))


def _write_allowfrom(tmp_path: Path, bot: str, channel: str, ids: list[str]):
    creds = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / f"{channel}-default-allowFrom.json").write_text(
        json.dumps({"allowFrom": ids}))


def _write_overlay(tmp_path: Path, bot: str, overlay: dict):
    rosters = tmp_path / "shared" / "rosters"
    rosters.mkdir(parents=True, exist_ok=True)
    (rosters / f"{bot}.json").write_text(json.dumps(overlay))


# ── (a) identity binding ──────────────────────────────────────────────────────


def test_upsert_403_for_tcp(app):
    with app.test_client() as c:
        resp = c.post("/api/directory/upsert",
                      json={"ref": "x", "names": {"display": "X"}},
                      environ_overrides={"REMOTE_PEER_UID": os.getuid()})  # no transport
    assert resp.status_code == 403


def test_upsert_403_for_unknown_uid(app):
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net", "names": {"display": "Dana"}},
                     uid=4242424)
    assert resp.status_code == 403


def test_upsert_requires_ref(app):
    with app.test_client() as c:
        resp = _post(c, {"names": {"display": "X"}})
    assert resp.status_code == 400


def test_upsert_nothing_to_write_is_400(app):
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net"})
    assert resp.status_code == 400


# ── (b) cross-bot isolation ────────────────────────────────────────────────────


def test_upsert_ignores_body_bot_and_writes_to_caller(app, tmp_path):
    """A body claiming bot=rex is ignored — the write lands in LEX's directory (peer uid),
    never rex's."""
    shared = _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net", "bot": OTHER,
                         "emails": [{"addr": "dana@example.net", "rank": "primary"}]})
    assert resp.status_code == 200, resp.get_json()
    # Wrote to LEX, not REX.
    assert uds.directory_path(shared, BOT).exists()
    assert not uds.directory_path(shared, OTHER).exists()


def test_upsert_cannot_reach_another_bots_person(app, tmp_path):
    """A ref that names a person who exists ONLY in another bot's directory does not
    resolve for the caller — so it either 422s (no key) or mints in the CALLER's own
    store; rex's directory is never written."""
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, OTHER, "telegram", "70000050", by=OTHER,
                     provenance="bot-asserted", names={"display": "Their Person"})
    before = json.loads(uds.directory_path(shared, OTHER).read_text())
    with app.test_client() as c:
        # By the other bot's stable id (a bare token) → no match in lex, not an email →
        # not keyable → clean 422; rex's store is untouched.
        resp = _post(c, {"ref": "70000050", "contact": {"org": "Acme"}})
    assert resp.status_code == 422
    after = json.loads(uds.directory_path(shared, OTHER).read_text())
    assert before == after  # rex's directory never mutated


# ── (c) identity, never authority (invariant #2) ───────────────────────────────


@pytest.mark.parametrize("authority", [
    {"membership": {"admitted": True}},
    {"role": "admin"},
    {"roles": ["admin"]},
    {"capabilities": ["bot.roster.mutate"]},
    {"admitted": True},
    {"admission": "approved"},
    {"profile_ref": ".openclaw/profiles/x.md"},
])
def test_upsert_rejects_authority_keys(app, authority):
    body = {"ref": "dana@example.net",
            "emails": [{"addr": "dana@example.net", "rank": "primary"}], **authority}
    with app.test_client() as c:
        resp = _post(c, body)
    assert resp.status_code == 400, authority
    assert "cannot set" in resp.get_json()["error"]


def test_upsert_create_mints_contact_without_membership(app, tmp_path):
    """The address-book case: a brand-new contact is a Person WITHOUT membership — no
    role, never admitted (membership comes from the roster join, and a minted contact is
    not in the roster)."""
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net", "names": {"display": "Dana Lopez"},
                         "emails": [{"addr": "dana@example.net", "rank": "primary"}]})
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] and body["created"]
    assert body["person"]["membership"] is None  # ← pure contact, no authority
    # And the store agrees, resolved through the one read path.
    net = load_network(app._evolve_network_path)
    person = udr.resolve_person(net, BOT, "dana@example.net")
    assert person is not None and person.membership is None


def test_upsert_does_not_grant_membership_to_existing_user(app, tmp_path):
    """Writing contact data onto an admitted user touches only directory fields — the
    membership/role is whatever the roster says, never altered by the write."""
    _shared(tmp_path)
    _write_allowfrom(tmp_path, BOT, "telegram", ["999"])  # 999 is a pod admin
    with app.test_client() as c:
        resp = _post(c, {"ref": "999",
                         "emails": [{"addr": "admin@example.net", "rank": "primary"}]})
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"]
    assert body["created"] is False
    # Role is still admin (from the roster), unchanged by the bot's contact write.
    assert body["person"]["membership"]["role"] == "admin"
    assert body["person"]["emails"][0]["addr"] == "admin@example.net"
    assert body["person"]["emails"][0]["provenance"] == "bot-asserted"


# ── (d) provenance never forged / downgraded (invariant #3) ────────────────────


def test_upsert_stamps_bot_asserted_and_ignores_forged_provenance(app, tmp_path):
    """The route hardcodes bot-asserted; a body trying to forge operator-verified on a row
    is overridden, and ``verified`` is forced False (a bot can't self-certify)."""
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {
            "ref": "dana@example.net",
            "emails": [{"addr": "dana@example.net", "rank": "primary",
                        "provenance": "operator-verified", "verified": True}]})
    email = resp.get_json()["person"]["emails"][0]
    assert email["provenance"] == "bot-asserted"  # forgery overridden
    assert email["verified"] is False             # bot cannot self-verify


def test_upsert_bot_cannot_clobber_operator_verified_email(app, tmp_path):
    """The operator sets a verified primary (Phase-2 provenance); a later bot upsert that
    tries to replace it leaves the operator row intact and is demoted to secondary."""
    shared = _shared(tmp_path)
    uds.upsert_entry(
        shared, BOT, "email", "dana@example.net", by="operator@test",
        provenance="operator-verified",
        emails=[{"addr": "dana@example.net", "rank": "primary", "verified": True}])
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net",
                         "emails": [{"addr": "dana@bot.example", "rank": "primary"}]})
    emails = {e["addr"]: e for e in resp.get_json()["person"]["emails"]}
    assert emails["dana@example.net"]["provenance"] == "operator-verified"
    assert emails["dana@example.net"]["rank"] == "primary"      # operator primary held
    assert emails["dana@bot.example"]["rank"] == "secondary"    # bot demoted


# ── (e) create-or-update scope ─────────────────────────────────────────────────


def test_upsert_update_existing_contact(app, tmp_path):
    """A second upsert to a resolvable contact UPDATES it (created=False), augmenting the
    contact map rather than wiping unrelated keys."""
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, BOT, "email", "dana@example.net", by=BOT,
                     provenance="bot-asserted", names={"display": "Dana Lopez"},
                     contact={"org": "Acme", "phone": "+100"})
    with app.test_client() as c:
        resp = _post(c, {"ref": "Dana Lopez", "contact": {"org": "NewCo"}})
    body = resp.get_json()
    assert resp.status_code == 200 and body["created"] is False
    # org updated, phone preserved (shallow-merge, not whole-replace).
    assert body["person"]["contact"] == {"org": "NewCo", "phone": "+100"}


def test_upsert_create_by_explicit_identity_no_email(app, tmp_path):
    """A contact keyed on a messaging identity (no email) — still membership-free."""
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "Bob",
                         "identities": [{"platform": "telegram", "id": "70000077"}],
                         "names": {"display": "Bob Stone"}})
    body = resp.get_json()
    assert resp.status_code == 200 and body["created"]
    assert body["person"]["membership"] is None
    assert any(i["id"] == "70000077" for i in body["person"]["identities"])


def test_upsert_name_only_uncreatable_is_422(app, tmp_path):
    """A name with nothing to key on can't mint a contact — a clean 422, not a 500."""
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "Just A Name", "contact": {"org": "Acme"}})
    assert resp.status_code == 422
    assert not resp.get_json()["ok"]


# ── (e2) dedup: a write never mints a duplicate row for an existing person ──────


def test_upsert_by_stored_identity_updates_not_duplicates(app, tmp_path):
    """A person keyed on their email but holding a stored slack identity is found by a
    bare-id / identity write — it UPDATES them, never mints a second row (invariant #1)."""
    shared = _shared(tmp_path)
    # Person keyed on email, carrying a slack identity row.
    uds.upsert_entry(shared, BOT, "email", "dana@example.net", by=BOT,
                     provenance="bot-asserted", names={"display": "Dana Lopez"},
                     identities=[{"platform": "slack", "id": "U0FAKEDANA"}])
    with app.test_client() as c:
        # A new write that refs by a fresh name but supplies the SAME slack identity.
        resp = _post(c, {"ref": "D.",
                         "identities": [{"platform": "slack", "id": "U0FAKEDANA"}],
                         "contact": {"org": "Acme"}})
    body = resp.get_json()
    assert resp.status_code == 200 and body["created"] is False  # update, not create
    # Exactly one Person in the store (no duplicate row minted).
    directory = uds.load_directory(shared, BOT)
    assert len(directory["persons"]) == 1


def test_upsert_email_in_body_attaches_to_existing_owner(app, tmp_path):
    """A ref that misses by name while the body's email already belongs to a Person
    updates that Person rather than minting a duplicate keyed on the email."""
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, BOT, "telegram", "70000200", by=BOT,
                     provenance="bot-asserted", names={"display": "Existing"},
                     emails=[{"addr": "known@example.net", "rank": "primary"}])
    with app.test_client() as c:
        resp = _post(c, {"ref": "Some New Name",
                         "emails": [{"addr": "known@example.net", "rank": "secondary"}],
                         "contact": {"phone": "+100"}})
    body = resp.get_json()
    assert resp.status_code == 200 and body["created"] is False
    directory = uds.load_directory(shared, BOT)
    assert len(directory["persons"]) == 1  # attached to the existing owner


# ── (f) hot-path hygiene: clean errors, growth + size caps ─────────────────────


def test_upsert_too_many_emails_is_400(app, tmp_path):
    _shared(tmp_path)
    emails = [{"addr": f"a{i}@example.net"} for i in range(dbr.MAX_EMAILS_PER_PERSON + 1)]
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net", "emails": emails})
    assert resp.status_code == 400


def test_upsert_giant_contact_value_is_400(app, tmp_path):
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net",
                         "contact": {"notes": "x" * (dbr.MAX_FIELD_LEN + 1)}})
    assert resp.status_code == 400


def test_upsert_too_many_identities_is_400(app, tmp_path):
    _shared(tmp_path)
    idents = [{"platform": "slack", "id": f"U{i}"}
              for i in range(dbr.MAX_IDENTITIES_PER_PERSON + 1)]
    with app.test_client() as c:
        resp = _post(c, {"ref": "Bob", "identities": idents})
    assert resp.status_code == 400


def test_upsert_malformed_email_is_400_not_500(app, tmp_path):
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net",
                         "emails": [{"addr": "not-an-email"}]})
    assert resp.status_code == 400


def test_upsert_two_primaries_is_400(app, tmp_path):
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net", "emails": [
            {"addr": "a@example.net", "rank": "primary"},
            {"addr": "b@example.net", "rank": "primary"}]})
    assert resp.status_code == 400


def test_upsert_nested_contact_value_is_400(app, tmp_path):
    _shared(tmp_path)
    with app.test_client() as c:
        resp = _post(c, {"ref": "dana@example.net",
                         "contact": {"org": {"nested": "blob"}}})
    assert resp.status_code == 400


def test_upsert_growth_cap_refuses_new_contact(app, tmp_path, monkeypatch):
    """Past the per-bot person cap, a CREATE is refused (429) — but an UPDATE to an
    existing person still succeeds."""
    shared = _shared(tmp_path)
    monkeypatch.setattr(dbr, "MAX_DIRECTORY_PERSONS", 1)
    # One existing contact fills the cap.
    uds.upsert_entry(shared, BOT, "email", "first@example.net", by=BOT,
                     provenance="bot-asserted", names={"display": "First"})
    with app.test_client() as c:
        # A NEW contact is refused …
        new = _post(c, {"ref": "second@example.net",
                        "emails": [{"addr": "second@example.net", "rank": "primary"}]})
        assert new.status_code == 429
        # … but updating the EXISTING one is still fine.
        upd = _post(c, {"ref": "first@example.net", "contact": {"org": "Acme"}})
        assert upd.status_code == 200 and upd.get_json()["created"] is False


def test_upsert_blocked_person_is_noop(app, tmp_path):
    """A blocked person is invisible to the bot for WRITE too: no-op, advertise nothing,
    write nothing (parity with the lookup's blocked suppression)."""
    shared = _shared(tmp_path)
    _write_allowfrom(tmp_path, BOT, "telegram", ["70000099"])
    _write_overlay(tmp_path, BOT, {"blocked": {"telegram:70000099": {"by": "op"}}})
    with app.test_client() as c:
        resp = _post(c, {"ref": "70000099",
                         "emails": [{"addr": "blocked@example.net", "rank": "primary"}]})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["person"] is None and body["created"] is False
    # Nothing was written for the blocked identity.
    entry = uds.get_entry(uds.load_directory(shared, BOT), "telegram", "70000099")
    assert entry is None or not entry.get("emails")


# ── one read path: the write is immediately visible via resolve_person ─────────


def test_upsert_write_is_visible_through_resolve_person(app, tmp_path):
    _shared(tmp_path)
    with app.test_client() as c:
        _post(c, {"ref": "ivy@example.net", "names": {"display": "Ivy Brooks"},
                  "emails": [{"addr": "ivy@example.net", "rank": "primary"}]})
    net = load_network(app._evolve_network_path)
    person = udr.resolve_person(net, BOT, "Ivy Brooks")
    assert person is not None
    assert person.primary_email.addr == "ivy@example.net"
    assert person.primary_email.provenance == "bot-asserted"
