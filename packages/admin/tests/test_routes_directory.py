"""End-to-end tests for the per-bot directory write routes (Phase 2).

Covers ``routes_directory`` (spec-user-directory-2026-06-22 §6 + §10 invariants):

  GET  /api/admin/bots/<bot>/directory
  POST /api/admin/bots/<bot>/directory/<platform>/<stable_id>/email
  POST /api/admin/bots/<bot>/directory/<platform>/<stable_id>/contact

The load-bearing invariants under test:

  * **Email CRUD + single-primary.** add / update / delete / set_primary /
    set_verified, with at most one ``rank:"primary"`` maintained atomically.
  * **Operator writes stamp ``operator-verified``** (invariant #3) — the route
    hardcodes the provenance; it is never read from the request body.
  * **Contact attribute edits** round-trip through the open ``contact`` map.
  * **Membership is untouchable.** No directory route (and no helper it calls)
    can add admission / roles — structurally (``upsert_entry`` has no membership
    param) and observably (no allowFrom / roster overlay file is ever written).
  * **Admit runs the EXISTING flow.** The UI's Admit posts to ``/users/approve``,
    not a directory route — there is no membership-granting directory route.
  * **Forgery guard.** A gateway-attested non-privileged requester is 403'd, so
    ``operator-verified`` can't be forged off the trusted admin-UI path.

FAKE ids + ``*.example`` / ``example.net`` emails only (docs/PLACEHOLDER_NAMING.md).
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

from evolve_admin.web import routes_directory as rd  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402


BOT = "team_bot_a"
# A pure address-book contact: keyed on an email identity, never in any roster.
CONTACT_PLATFORM = "email"
CONTACT_ID = "dana@acme.example"
# An admittable contact: carries a telegram identity but isn't in allowFrom.
TG_PLATFORM = "telegram"
TG_ID = "70000001"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _seed_network(tmp_path: Path) -> Path:
    base = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "members": [BOT],
        "bots": {BOT: {"role": "member", "port": 19002, "multiUser": True}},
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}, "names": {}}},
    }
    p = tmp_path / "network.json"
    p.write_text(json.dumps(base, indent=2))
    return p


@pytest.fixture
def shared(tmp_path) -> Path:
    d = tmp_path / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def app(tmp_path, monkeypatch):
    network_path = _seed_network(tmp_path)
    # Keep any roster allowFrom reads inside tmp (empty creds → empty roster →
    # every directory identity resolves as a contact unless we say otherwise).
    monkeypatch.setattr(rbu, "bot_home", lambda bot, net: tmp_path / "Users" / bot)
    a = Flask(__name__)
    rd.register_routes(a, network_path)
    a.config["TESTING"] = True
    return a


def _seed_contact(shared: Path, *, platform=CONTACT_PLATFORM, stable_id=CONTACT_ID,
                  emails=None, contact=None, provenance="bot-asserted") -> None:
    """Seed a directory entry (a contact) via the Phase-1 store."""
    uds.upsert_entry(
        shared, BOT, platform, stable_id,
        by="team_bot_a", provenance=provenance,
        emails=emails, contact=contact)


def _emails(shared: Path, platform=CONTACT_PLATFORM, stable_id=CONTACT_ID) -> list[dict]:
    directory = uds.load_directory(shared, BOT)
    entry = uds.get_entry(directory, platform, stable_id) or {}
    return entry.get("emails") or []


def _email_url(platform=CONTACT_PLATFORM, stable_id=CONTACT_ID) -> str:
    return f"/api/admin/bots/{BOT}/directory/{platform}/{stable_id}/email"


def _contact_url(platform=CONTACT_PLATFORM, stable_id=CONTACT_ID) -> str:
    return f"/api/admin/bots/{BOT}/directory/{platform}/{stable_id}/contact"


# ── GET ───────────────────────────────────────────────────────────────────────


def test_get_unknown_bot_404s(app):
    with app.test_client() as c:
        assert c.get("/api/admin/bots/nope/directory").status_code == 404


def test_get_lists_contacts(app, shared):
    _seed_contact(shared, emails=[{"addr": "dana@example.net", "rank": "primary"}])
    with app.test_client() as c:
        data = c.get(f"/api/admin/bots/{BOT}/directory").get_json()
    assert data["bot_id"] == BOT
    persons = data["persons"]
    assert len(persons) == 1
    p = persons[0]
    assert p["membership"] is None  # a contact, not an admitted user
    assert p["emails"][0]["addr"] == "dana@example.net"


# ── Email CRUD + single-primary ───────────────────────────────────────────────


def test_email_add(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        r = c.post(_email_url(), json={"op": "add", "addr": "dana@example.net"})
    assert r.status_code == 200
    addrs = {e["addr"] for e in _emails(shared)}
    assert addrs == {"dana@example.net"}


def test_email_add_primary_demotes_old_primary_atomically(app, shared):
    _seed_contact(shared, emails=[{"addr": "first@example.net", "rank": "primary"}])
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "add", "addr": "second@example.net", "rank": "primary"})
    emails = _emails(shared)
    primaries = [e for e in emails if e["rank"] == "primary"]
    assert len(primaries) == 1
    assert primaries[0]["addr"] == "second@example.net"  # newest primary wins
    assert {e["addr"] for e in emails} == {"first@example.net", "second@example.net"}


def test_email_set_primary_flips_atomically(app, shared):
    _seed_contact(shared, emails=[
        {"addr": "a@example.net", "rank": "primary"},
        {"addr": "b@acme.example", "rank": "secondary"}])
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "set_primary", "addr": "b@acme.example"})
    emails = _emails(shared)
    primaries = [e for e in emails if e["rank"] == "primary"]
    assert [e["addr"] for e in primaries] == ["b@acme.example"]


def test_email_update_rename_and_promote(app, shared):
    _seed_contact(shared, emails=[
        {"addr": "old@example.net", "rank": "secondary"},
        {"addr": "keep@acme.example", "rank": "primary"}])
    with app.test_client() as c:
        c.post(_email_url(), json={
            "op": "update", "addr": "old@example.net",
            "new_addr": "new@example.net", "rank": "primary"})
    emails = _emails(shared)
    addrs = {e["addr"] for e in emails}
    assert "new@example.net" in addrs and "old@example.net" not in addrs
    primaries = [e for e in emails if e["rank"] == "primary"]
    assert [e["addr"] for e in primaries] == ["new@example.net"]  # promote demoted the other


def test_email_set_verified(app, shared):
    _seed_contact(shared, emails=[{"addr": "a@example.net", "rank": "primary"}])
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "set_verified", "addr": "a@example.net", "verified": True})
    assert _emails(shared)[0]["verified"] is True


def test_email_delete(app, shared):
    _seed_contact(shared, emails=[
        {"addr": "a@example.net", "rank": "primary"},
        {"addr": "b@acme.example", "rank": "secondary"}])
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "delete", "addr": "a@example.net"})
    assert {e["addr"] for e in _emails(shared)} == {"b@acme.example"}


def test_email_bad_op_400s(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        assert c.post(_email_url(), json={"op": "frobnicate"}).status_code == 400


def test_email_bad_address_400s(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        assert c.post(_email_url(), json={"op": "add", "addr": "not-an-email"}).status_code == 400


def test_email_add_first_to_admitted_user_with_no_entry(app, shared):
    """Operator adds the FIRST email to an identity that has no directory entry
    yet — the route mints the entry on write (current emails treated as [])."""
    with app.test_client() as c:
        r = c.post(_email_url(TG_PLATFORM, TG_ID),
                   json={"op": "add", "addr": "tg@example.net", "rank": "primary"})
    assert r.status_code == 200
    assert {e["addr"] for e in _emails(shared, TG_PLATFORM, TG_ID)} == {"tg@example.net"}


# ── Provenance: operator writes are operator-verified ─────────────────────────


def test_operator_email_write_stamps_operator_verified(app, shared):
    """A bot-asserted email, once the operator edits the record, is stamped
    operator-verified (the route hardcodes provenance; never reads the body)."""
    _seed_contact(shared, emails=[{"addr": "botclaim@example.net", "rank": "primary"}],
                  provenance="bot-asserted")
    assert _emails(shared)[0]["provenance"] == "bot-asserted"  # precondition
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "set_verified", "addr": "botclaim@example.net",
                                   "verified": True})
    assert _emails(shared)[0]["provenance"] == "operator-verified"


def test_operator_cannot_forge_weaker_provenance_via_body(app, shared):
    """Even if a caller puts ``provenance`` in the body, the stored row is
    operator-verified — the route never forwards a body provenance."""
    _seed_contact(shared)
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "add", "addr": "x@example.net",
                                   "provenance": "channel-captured"})
    assert _emails(shared)[0]["provenance"] == "operator-verified"


# ── Contact attribute edits ───────────────────────────────────────────────────


def test_contact_edit_round_trips(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        r = c.post(_contact_url(), json={"contact": {"org": "Acme", "phone": "+10000000"}})
    assert r.status_code == 200
    directory = uds.load_directory(shared, BOT)
    entry = uds.get_entry(directory, CONTACT_PLATFORM, CONTACT_ID)
    assert entry["contact"] == {"org": "Acme", "phone": "+10000000"}
    # Stamped operator-verified.
    contact_audit = [a for a in entry["audit"] if a["field"] == "contact"]
    assert contact_audit and contact_audit[-1]["source"] == "operator-verified"


def test_contact_rejects_nested_blob(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        r = c.post(_contact_url(), json={"contact": {"x": {"nested": 1}}})
    assert r.status_code == 400


def test_contact_missing_object_400s(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        assert c.post(_contact_url(), json={"not_contact": 1}).status_code == 400


# ── Membership is UNTOUCHABLE (invariant #2) ──────────────────────────────────


def test_upsert_entry_has_no_membership_parameter():
    """Structural proof: the ONLY write helper these routes call cannot touch
    membership — there is no keyword for it (TypeError, not a runtime guard)."""
    with pytest.raises(TypeError):
        uds.upsert_entry(
            "/tmp/nope", BOT, "slack", "U0FAKE",  # noqa: S108 — never written (raises first)
            by="op", provenance="operator-verified",
            membership={"admitted": True, "role": "admin"})  # type: ignore[call-arg]


def test_email_write_creates_no_allowfrom_or_roster(app, shared, tmp_path):
    """Observable proof: editing emails never writes an allowFrom (admission) or
    a roster overlay (roles) file — the directory write path has no authority."""
    _seed_contact(shared)
    with app.test_client() as c:
        c.post(_email_url(), json={"op": "add", "addr": "x@example.net"})
        c.post(_contact_url(), json={"contact": {"org": "Acme"}})
    # No OC allowFrom credential files anywhere under the bot home.
    assert not list((tmp_path / "Users").rglob("*allowFrom*.json")) \
        if (tmp_path / "Users").exists() else True
    # No roster overlay file (roles/admission live there) was created.
    assert not (shared / "rosters" / f"{BOT}.json").exists()


def test_directory_routes_reach_no_admission_or_role_mutators():
    """The module references none of the admission/role mutation primitives — so
    there is no code path from a directory route to membership."""
    src = Path(rd.__file__).read_text()
    # Actual mutator identifiers (not prose) — any of these in the module would
    # mean a code path from a directory route to admission/roles.
    forbidden = [
        "block_identity", "set_identity_role", "set_identity_engagement",
        "mutate_overlay", "safe_write_bot_config", "allowFrom",
        "roster_overlay", "_write_bot_json",
    ]
    leaked = [tok for tok in forbidden if tok in src]
    assert not leaked, f"directory routes must not reach admission/role mutators: {leaked}"


def test_admit_in_ui_uses_existing_approve_route_not_directory():
    """The UI's Admit action posts to the EXISTING /users/approve flow — not a
    directory route — so admitting a contact runs the fail-closed admission path
    (invariant #2/#4). Encodes the seam at the JS callsite."""
    users_js = (_ADMIN / "evolve_admin" / "web" / "static" / "js" / "pages"
                / "users.js").read_text()
    # The admit handler exists and targets /users/approve.
    assert "_usersDirAdmit" in users_js
    idx = users_js.index("async function _usersDirAdmit")
    body = users_js[idx:idx + 1200]
    assert "/users/approve" in body
    assert "/directory/" not in body  # never a directory write for admission


# ── Forgery guard (gateway-attested requester) ────────────────────────────────


def test_unprivileged_attested_requester_is_forbidden(app, shared):
    """A gateway-attested requester lacking bot.roster.mutate is 403'd — so a
    non-operator relayed through a bot gateway cannot reach these routes to forge
    operator-verified. (The header-less admin-UI path below is the operator.)"""
    _seed_contact(shared)
    with app.test_client() as c:
        r = c.post(_email_url(), json={"op": "add", "addr": "x@example.net"},
                   headers={"X-Requester-Identity": "telegram:70000999"})
    assert r.status_code == 403
    # Nothing was written.
    assert _emails(shared) == []


def test_ui_path_without_header_succeeds(app, shared):
    _seed_contact(shared)
    with app.test_client() as c:
        r = c.post(_email_url(), json={"op": "add", "addr": "x@example.net"})
    assert r.status_code == 200


# ── Pure email-op transform (unit) ────────────────────────────────────────────


def test_apply_email_op_dup_add_rejected():
    cur = [{"addr": "a@example.net", "rank": "primary", "verified": False}]
    with pytest.raises(rd._EmailOpError):
        rd.apply_email_op(cur, "add", {"addr": "a@example.net"})


def test_apply_email_op_missing_target_rejected():
    with pytest.raises(rd._EmailOpError):
        rd.apply_email_op([], "delete", {"addr": "ghost@example.net"})


def test_apply_email_op_set_primary_keeps_single_primary():
    cur = [
        {"addr": "a@example.net", "rank": "primary", "verified": False},
        {"addr": "b@acme.example", "rank": "secondary", "verified": False},
        {"addr": "c@example.net", "rank": "secondary", "verified": False},
    ]
    out = rd.apply_email_op(cur, "set_primary", {"addr": "c@example.net"})
    assert [e["rank"] for e in out] == ["secondary", "secondary", "primary"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
