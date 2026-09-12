"""Unit tests for the Phase-1 user directory — model, store, and resolve_person.

Spec: internal/spec-user-directory-2026-06-22.md. Covers the three pieces of the read-side
foundation:

  * ``model`` — the ``Person`` invariants (single ``rank:"primary"``; provenance enum)
    and the lenient read-side coercion.
  * ``storage`` — the directory store at ``{shared_dir}/directory/{bot_id}.json``: atomic
    write + 0600 mode, audit log, and stable ``person_id`` minting across reloads.
  * ``resolver`` — ``resolve_person`` / ``resolve_persons``: the roster⋈directory join
    (admitted ⇒ membership present; contact ⇒ membership None; empty store ⇒ empty
    emails), and ref resolution by stable id / handle / email.

All identities use FAKE placeholder ids per docs/PLACEHOLDER_NAMING.md (``U0FAKE…`` /
bot ``team_bot_a``) — never real user data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import roster_resolver as rr  # noqa: E402
from evolve_admin.user_directory import model  # noqa: E402
from evolve_admin.user_directory import resolver as udr  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402


BOT = "team_bot_a"


# ── Fixtures ─────────────────────────────────────────────────────────────


def _network(tmp_path: Path, **overrides) -> dict:
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"team_bot_a": {"role": "member", "port": 19002, "multiUser": True}},
        "pod": {"admins": {
            "external_ids": {"telegram": ["999"]}, "names": {},
            "resolved_names": {},
        }},
    }
    net.update(overrides)
    return net


def _shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _roster(net: dict, shared: Path, allow: dict) -> list:
    """Build a roster from an injected allowFrom map — no filesystem/sudo."""
    return rr.resolve_roster(
        net, BOT, shared_dir=shared, activity={},
        allowfrom_reader=lambda ch: allow.get(ch))


# ── model: Person invariants ─────────────────────────────────────────────


def test_email_rejects_bad_provenance_and_rank():
    with pytest.raises(ValueError):
        model.Email(addr="a@x.test", rank="primary", provenance="made-up")
    with pytest.raises(ValueError):
        model.Email(addr="a@x.test", rank="tertiary", provenance="bot-asserted")


def test_identity_rejects_bad_source():
    with pytest.raises(ValueError):
        model.Identity(platform="slack", id="U0FAKEUSR1", source="nonsense")


def test_person_rejects_two_primary_emails():
    emails = [
        model.Email("a@x.test", "primary", "bot-asserted"),
        model.Email("b@x.test", "primary", "operator-verified"),
    ]
    with pytest.raises(ValueError):
        model.Person(person_id="pers_x", emails=emails)


def test_person_one_primary_ok_and_primary_email_property():
    emails = [
        model.Email("a@x.test", "primary", "operator-verified"),
        model.Email("b@x.test", "secondary", "bot-asserted"),
    ]
    p = model.Person(person_id="pers_x", emails=emails)
    assert p.primary_email is not None
    assert p.primary_email.addr == "a@x.test"


def test_provenance_outranks_ordering():
    assert model.provenance_outranks("operator-verified", "bot-asserted")
    assert model.provenance_outranks("bot-asserted", "channel-captured")
    assert not model.provenance_outranks("channel-captured", "operator-verified")
    # Unknown ranks below every known value.
    assert model.provenance_outranks("bot-asserted", "garbage")
    assert not model.provenance_outranks("garbage", "channel-captured")


def test_coerce_emails_drops_malformed_and_repairs_double_primary():
    raw = [
        {"addr": "weak@x.test", "rank": "primary", "provenance": "channel-captured"},
        {"addr": "strong@x.test", "rank": "primary", "provenance": "operator-verified"},
        {"addr": "bad@x.test", "rank": "primary", "provenance": "nope"},  # dropped
        "not-a-dict",  # dropped
    ]
    out = model.coerce_emails(raw)
    primaries = [e for e in out if e.rank == "primary"]
    # Exactly one primary survives, and it is the strongest-provenance one.
    assert len(primaries) == 1
    assert primaries[0].addr == "strong@x.test"
    assert {e.addr for e in out} == {"weak@x.test", "strong@x.test"}


def test_audit_entry_serializes_from_key():
    a = model.AuditEntry(field="emails", from_=None, to=["x"], by="op",
                         source="operator-verified", at="2026-06-22T00:00:00Z")
    d = a.to_dict()
    assert d["from"] is None and d["to"] == ["x"]
    assert "from_" not in d
    # round-trips
    assert model.AuditEntry.from_dict(d).to_dict() == d


def test_person_to_dict_round_trips_shape():
    p = model.Person(
        person_id="pers_x",
        names={"display": "P1"},
        identities=[model.Identity("slack", "U0FAKEUSR1", "p1", "channel-captured")],
        emails=[model.Email("a@x.test", "primary", "bot-asserted")],
        contact={"phone": "+100"},
        membership=model.Membership(True, "participant", ["bot.roster.read"], ["slack"]),
        profile_ref=".openclaw/profiles/u.md",
    )
    d = p.to_dict()
    assert d["person_id"] == "pers_x"
    assert d["membership"]["role"] == "participant"
    assert d["emails"][0]["rank"] == "primary"
    assert d["identities"][0]["handle"] == "p1"


# ── storage: directory store ─────────────────────────────────────────────


def test_person_id_for_is_deterministic_and_stable():
    a = uds.person_id_for(BOT, "slack", "U0FAKEUSR1")
    b = uds.person_id_for(BOT, "slack", "U0FAKEUSR1")
    assert a == b and a.startswith("pers_")
    # Different identity ⇒ different id.
    assert a != uds.person_id_for(BOT, "slack", "U0FAKEUSR2")
    assert a != uds.person_id_for("other_bot", "slack", "U0FAKEUSR1")


def test_load_directory_absent_returns_empty(tmp_path):
    shared = _shared(tmp_path)
    d = uds.load_directory(shared, BOT)
    assert d["bot_id"] == BOT and d["persons"] == {}


def test_mint_person_id_persists_and_reuses(tmp_path):
    shared = _shared(tmp_path)
    pid1 = uds.mint_person_id(shared, BOT, "slack", "U0FAKEUSR1")
    # Persisted to disk.
    assert uds.directory_path(shared, BOT).exists()
    # A fresh load reads back the same id, and a second mint reuses it.
    pid2 = uds.mint_person_id(shared, BOT, "slack", "U0FAKEUSR1")
    assert pid1 == pid2
    stored = uds.get_entry(uds.load_directory(shared, BOT), "slack", "U0FAKEUSR1")
    assert stored["person_id"] == pid1


def test_directory_file_mode_is_0600(tmp_path):
    shared = _shared(tmp_path)
    uds.mint_person_id(shared, BOT, "slack", "U0FAKEUSR1")
    mode = uds.directory_path(shared, BOT).stat().st_mode & 0o777
    assert mode == 0o600


def test_upsert_entry_writes_fields_audit_and_log(tmp_path):
    shared = _shared(tmp_path)
    entry = uds.upsert_entry(
        shared, BOT, "slack", "U0FAKEUSR1",
        by="operator@test", provenance="operator-verified",
        emails=[{"addr": "primary@x.test", "rank": "primary"},
                {"addr": "alt@x.test", "rank": "secondary"}],
        contact={"org": "Palace"},
    )
    assert entry["person_id"].startswith("pers_")
    assert {e["addr"] for e in entry["emails"]} == {"primary@x.test", "alt@x.test"}
    assert entry["contact"]["org"] == "Palace"
    # Provenance never lost: a per-field audit entry exists for each changed field.
    fields = {a["field"] for a in entry["audit"]}
    assert {"emails", "contact"} <= fields
    assert all(a["source"] == "operator-verified"
               for a in entry["audit"] if a["field"] in ("emails", "contact"))
    # The daily audit log got at least one line.
    logs = list((shared / "directory" / "log").glob("*.jsonl"))
    assert logs and logs[0].read_text().strip()
    # Emails were stamped with the upsert provenance.
    assert all(e["provenance"] == "operator-verified" for e in entry["emails"])


def test_upsert_entry_rejects_two_primaries_and_leaves_store_untouched(tmp_path):
    shared = _shared(tmp_path)
    with pytest.raises(ValueError):
        uds.upsert_entry(
            shared, BOT, "slack", "U0FAKEUSR1",
            by="op", provenance="operator-verified",
            emails=[{"addr": "a@x.test", "rank": "primary"},
                    {"addr": "b@x.test", "rank": "primary"}])
    # Nothing was written.
    assert not uds.directory_path(shared, BOT).exists()


def test_upsert_entry_provenance_stamp_wins_over_caller_row(tmp_path):
    """A caller cannot forge a stronger provenance via a per-row ``provenance``/``source``
    — the upsert's declared provenance always wins, so the bot can't assert authority it
    wasn't granted (spec §10 invariant #2/#3). The stored row must match the audit."""
    shared = _shared(tmp_path)
    entry = uds.upsert_entry(
        shared, BOT, "slack", "U0FAKEUSR1",
        by="team_bot_a", provenance="bot-asserted",
        emails=[{"addr": "a@x.test", "rank": "primary",
                 "provenance": "operator-verified"}],  # forgery attempt
        identities=[{"platform": "slack", "id": "U0FAKEUSR1",
                     "source": "operator-verified"}])   # forgery attempt
    assert entry["emails"][0]["provenance"] == "bot-asserted"
    assert entry["identities"][0]["source"] == "bot-asserted"
    # Store row and audit entry agree — provenance is never lost or diverged.
    email_audit = [a for a in entry["audit"] if a["field"] == "emails"][0]
    assert email_audit["source"] == "bot-asserted"


def test_upsert_entry_rejects_bad_provenance(tmp_path):
    shared = _shared(tmp_path)
    with pytest.raises(ValueError):
        uds.upsert_entry(shared, BOT, "slack", "U0FAKEUSR1",
                         by="op", provenance="made-up", contact={"x": 1})


# ── storage: provenance-preserving merge (invariant #3, the clobber half) ─


def test_merge_emails_preserving_stronger_keeps_operator_row():
    """A weaker (bot) write keeps an existing stronger (operator-verified) row verbatim
    and drops a colliding weaker duplicate — the stronger row wins (invariant #3)."""
    existing = [
        {"addr": "dana@example.net", "rank": "primary",
         "provenance": "operator-verified", "verified": True},
    ]
    incoming = [
        {"addr": "dana@example.net", "rank": "secondary",
         "provenance": "bot-asserted", "verified": False},  # tries to demote the op row
        {"addr": "dana@new.example", "rank": "primary",
         "provenance": "bot-asserted", "verified": False},   # tries to install a new primary
    ]
    merged = uds.merge_emails_preserving_stronger(existing, incoming, "bot-asserted")
    by_addr = {e["addr"]: e for e in merged}
    # The operator row survives unchanged — same provenance, still verified, still primary.
    assert by_addr["dana@example.net"]["provenance"] == "operator-verified"
    assert by_addr["dana@example.net"]["verified"] is True
    assert by_addr["dana@example.net"]["rank"] == "primary"
    # The bot's would-be primary is demoted — a bot cannot unseat the operator's primary.
    assert by_addr["dana@new.example"]["rank"] == "secondary"
    # Exactly one primary overall.
    assert sum(1 for e in merged if e["rank"] == "primary") == 1


def test_merge_emails_operator_write_is_plain_replace():
    """The strongest provenance protects nothing (nothing outranks it), so the operator
    path keeps its historical whole-field-replace semantics — no behavior change."""
    existing = [{"addr": "old@x.test", "rank": "primary", "provenance": "bot-asserted"}]
    incoming = [{"addr": "new@x.test", "rank": "primary", "provenance": "operator-verified"}]
    merged = uds.merge_emails_preserving_stronger(existing, incoming, "operator-verified")
    assert [e["addr"] for e in merged] == ["new@x.test"]  # old (weaker) fully replaced


def test_upsert_bot_write_cannot_downgrade_operator_email(tmp_path):
    """End-to-end through ``upsert_entry``: the operator sets a verified primary; a later
    bot-asserted write that tries to overwrite it leaves the operator row intact."""
    shared = _shared(tmp_path)
    uds.upsert_entry(
        shared, BOT, "email", "dana@acme.example",
        by="operator@test", provenance="operator-verified",
        emails=[{"addr": "dana@example.net", "rank": "primary", "verified": True}])
    # The bot now asserts a different primary for the same person.
    entry = uds.upsert_entry(
        shared, BOT, "email", "dana@acme.example",
        by=BOT, provenance="bot-asserted",
        emails=[{"addr": "dana@bot.example", "rank": "primary"}])
    by_addr = {e["addr"]: e for e in entry["emails"]}
    assert by_addr["dana@example.net"]["provenance"] == "operator-verified"
    assert by_addr["dana@example.net"]["rank"] == "primary"      # operator primary held
    assert by_addr["dana@bot.example"]["provenance"] == "bot-asserted"
    assert by_addr["dana@bot.example"]["rank"] == "secondary"    # bot primary demoted


def test_upsert_operator_can_overwrite_bot_asserted_email(tmp_path):
    """The operator (strongest) overrides a bot-asserted row — protection is one-directional
    (only a WEAKER write is constrained)."""
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, BOT, "email", "x@acme.example", by=BOT,
                     provenance="bot-asserted",
                     emails=[{"addr": "guess@bot.example", "rank": "primary"}])
    entry = uds.upsert_entry(
        shared, BOT, "email", "x@acme.example", by="operator@test",
        provenance="operator-verified",
        emails=[{"addr": "real@example.net", "rank": "primary"}])
    assert [e["addr"] for e in entry["emails"]] == ["real@example.net"]
    assert entry["emails"][0]["provenance"] == "operator-verified"


def test_merge_identities_preserving_stronger_keeps_operator_identity():
    existing = [{"platform": "telegram", "id": "70000001", "source": "operator-verified"}]
    incoming = [{"platform": "telegram", "id": "70000001", "source": "bot-asserted",
                 "handle": "@spoof"}]
    merged = uds.merge_identities_preserving_stronger(existing, incoming, "bot-asserted")
    assert len(merged) == 1
    assert merged[0]["source"] == "operator-verified"
    assert "handle" not in merged[0]  # the operator row, untouched by the bot's version


def test_save_load_round_trip_preserves_persons(tmp_path):
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, BOT, "telegram", "111",
                     by="op", provenance="bot-asserted", contact={"note": "hi"})
    raw = json.loads(uds.directory_path(shared, BOT).read_text())
    assert "telegram:111" in raw["persons"]
    assert raw["persons"]["telegram:111"]["contact"]["note"] == "hi"


# ── resolver: the roster ⋈ directory join ────────────────────────────────


def test_resolve_person_admitted_has_membership(tmp_path):
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    roster = _roster(net, shared, {"telegram": ["999"]})  # 999 is a pod admin
    person = udr.resolve_person(
        net, BOT, ("telegram", "999"),
        shared_dir=shared, roster=roster)
    assert person is not None
    assert person.membership is not None
    assert person.membership.admitted is True
    assert person.membership.role == "admin"
    assert person.membership.channels == ["telegram"]
    assert person.membership.capabilities  # admin → non-empty cap set
    # person_id present + consistent with the store's deterministic mint.
    assert person.person_id == uds.person_id_for(BOT, "telegram", "999")


def test_resolve_person_empty_directory_store_yields_empty_emails(tmp_path):
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    roster = _roster(net, shared, {"telegram": ["999"]})
    person = udr.resolve_person(
        net, BOT, ("telegram", "999"), shared_dir=shared, roster=roster)
    assert person is not None
    assert person.emails == []
    assert person.contact == {}
    # The base channel identity is still synthesized from the roster.
    assert any(i.platform == "telegram" and i.id == "999"
               for i in person.identities)


def test_resolve_person_contact_has_no_membership(tmp_path):
    """A directory-only entry with no roster admission resolves as a contact."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    # An invitee the bot acts toward: in the directory, never admitted.
    uds.upsert_entry(
        shared, BOT, "email", "dana@acme.example",
        by="team_bot_a", provenance="bot-asserted",
        emails=[{"addr": "dana@example.net", "rank": "primary"},
                {"addr": "dana@acme.example", "rank": "secondary"}])
    roster = _roster(net, shared, {"telegram": ["999"]})  # contact NOT in roster
    person = udr.resolve_person(
        net, BOT, ("email", "dana@acme.example"),
        shared_dir=shared, roster=roster)
    assert person is not None
    assert person.membership is None  # ← contact, never labeled admitted
    assert person.primary_email is not None
    assert person.primary_email.addr == "dana@example.net"


def test_resolve_person_joins_directory_fields_for_admitted(tmp_path):
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    uds.upsert_entry(
        shared, BOT, "telegram", "999",
        by="operator@test", provenance="operator-verified",
        emails=[{"addr": "admin@x.test", "rank": "primary"}],
        contact={"phone": "+1555"})
    roster = _roster(net, shared, {"telegram": ["999"]})
    person = udr.resolve_person(
        net, BOT, ("telegram", "999"), shared_dir=shared, roster=roster)
    assert person is not None
    assert person.membership is not None and person.membership.admitted
    assert person.primary_email.addr == "admin@x.test"
    assert person.contact["phone"] == "+1555"


def test_resolve_person_by_stable_id_handle_and_email(tmp_path):
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {}, "names": {},
            "resolved_names": {
                "slack:U0FAKEUSR1": {"name": "P1", "username": "p1_handle",
                                     "email": "p1@x.test"},
            },
        }})
    shared = _shared(tmp_path)
    roster = _roster(net, shared, {"slack": ["U0FAKEUSR1"]})
    # by bare stable id
    by_id = udr.resolve_person(net, BOT, "U0FAKEUSR1", shared_dir=shared, roster=roster)
    # by @handle
    by_handle = udr.resolve_person(net, BOT, "@p1_handle", shared_dir=shared, roster=roster)
    # by roster email
    by_email = udr.resolve_person(net, BOT, "p1@x.test", shared_dir=shared, roster=roster)
    assert by_id and by_handle and by_email
    pid = uds.person_id_for(BOT, "slack", "U0FAKEUSR1")
    assert by_id.person_id == by_handle.person_id == by_email.person_id == pid


def test_resolve_person_by_display_name(tmp_path):
    """A bare string that isn't a stable id falls back to a case-insensitive display-name
    match — against the roster's resolved name and the directory ``names`` overrides.
    This is the canonical name matcher the bot's directory_lookup shares (invariant #1)."""
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {}, "names": {},
            "resolved_names": {
                "slack:U0FAKEUSR1": {"name": "Dana Lopez", "username": "dana"},
            },
        }})
    shared = _shared(tmp_path)
    roster = _roster(net, shared, {"slack": ["U0FAKEUSR1"]})
    # roster display name (case-insensitive, whole-string)
    by_name = udr.resolve_person(net, BOT, "dana lopez", shared_dir=shared, roster=roster)
    assert by_name is not None
    assert by_name.person_id == uds.person_id_for(BOT, "slack", "U0FAKEUSR1")
    # a pure contact resolvable by its directory names.display override
    uds.upsert_entry(shared, BOT, "email", "ivy@acme.example", by=BOT,
                     provenance="bot-asserted", names={"display": "Ivy Brooks"})
    contact = udr.resolve_person(net, BOT, "Ivy Brooks", shared_dir=shared, roster=roster)
    assert contact is not None and contact.names.get("display") == "Ivy Brooks"
    # a partial/wrong name does NOT fuzzily resolve (conservative exact match)
    assert udr.resolve_person(net, BOT, "Dana", shared_dir=shared, roster=roster) is None


def test_resolve_person_ambiguous_name_misses(tmp_path):
    """Two DISTINCT people sharing a display name → a name lookup returns None rather than
    an arbitrary one. A lookup must never silently resolve to the wrong person; the caller
    disambiguates with the precise id/@handle/email forms."""
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {}, "names": {},
            "resolved_names": {
                "slack:U0FAKEUSR1": {"name": "Dana Lopez", "username": "dana1"},
                "slack:U0FAKEUSR2": {"name": "Dana Lopez", "username": "dana2"},
            },
        }})
    shared = _shared(tmp_path)
    roster = _roster(net, shared, {"slack": ["U0FAKEUSR1", "U0FAKEUSR2"]})
    # Collided name ⇒ miss …
    assert udr.resolve_person(
        net, BOT, "Dana Lopez", shared_dir=shared, roster=roster) is None
    # … but each is still reachable by its unambiguous handle.
    assert udr.resolve_person(
        net, BOT, "@dana1", shared_dir=shared, roster=roster) is not None
    assert udr.resolve_person(
        net, BOT, "@dana2", shared_dir=shared, roster=roster) is not None


def test_resolve_person_by_stored_identity_row_id(tmp_path):
    """A contact keyed on its email but carrying a stored ``slack:U…`` identity row is
    reachable by that bare id — symmetry with handle/email lookups, and what stops the
    write path minting a duplicate row for them (invariant #1)."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, BOT, "email", "dana@acme.example", by=BOT,
                     provenance="bot-asserted", names={"display": "Dana Lopez"},
                     identities=[{"platform": "slack", "id": "U0FAKEDANA"}])
    roster = _roster(net, shared, {"telegram": ["999"]})  # contact not admitted
    # resolve_identity (the write path's matcher) finds the email-keyed entry by the id.
    target = udr.resolve_identity(
        net, BOT, "U0FAKEDANA", shared_dir=shared, roster=roster)
    assert target == ("email", "dana@acme.example")
    person = udr.resolve_person(
        net, BOT, "U0FAKEDANA", shared_dir=shared, roster=roster)
    assert person is not None and person.names.get("display") == "Dana Lopez"


def test_resolve_person_unknown_ref_returns_none(tmp_path):
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    roster = _roster(net, shared, {"telegram": ["999"]})
    assert udr.resolve_person(
        net, BOT, "U0NOSUCHUSER", shared_dir=shared, roster=roster) is None


def test_resolve_persons_lists_users_and_contacts(tmp_path):
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    # One admitted user + one pure contact.
    uds.upsert_entry(shared, BOT, "email", "dana@acme.example",
                     by="team_bot_a", provenance="bot-asserted",
                     emails=[{"addr": "dana@example.net", "rank": "primary"}])
    roster = _roster(net, shared, {"telegram": ["999"]})
    persons = udr.resolve_persons(net, BOT, shared_dir=shared, roster=roster)
    admitted = [p for p in persons if p.membership is not None]
    contacts = [p for p in persons if p.membership is None]
    assert len(admitted) == 1 and admitted[0].membership.role == "admin"
    assert len(contacts) == 1
    assert contacts[0].primary_email.addr == "dana@example.net"
    # The contact is never rendered as admitted, and the user never as a contact.
    assert {p.membership is None for p in persons} == {True, False}


def test_resolve_person_membership_is_authoritative_not_trusted(tmp_path):
    """An id absent from the roster is a contact even if it has a directory entry —
    membership comes from the roster join, never from the directory store."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    uds.upsert_entry(shared, BOT, "slack", "U0FAKEUSR9",
                     by="team_bot_a", provenance="bot-asserted", contact={"org": "X"})
    roster = _roster(net, shared, {"slack": []})  # nobody admitted on slack
    person = udr.resolve_person(
        net, BOT, ("slack", "U0FAKEUSR9"), shared_dir=shared, roster=roster)
    assert person is not None
    assert person.membership is None
