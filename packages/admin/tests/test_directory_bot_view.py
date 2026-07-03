"""Unit tests for ``user_directory.bot_view`` — the bot-facing Person projection.

Spec: docs/spec-user-directory-2026-06-22.md §5 + §10 invariants #1 (one read path),
#5 (directory ≠ private profile). Covers the sanitizer + digest builder that sit between
``resolve_person`` / ``resolve_persons`` (THE one read path) and the bot:

  * ``person_to_bot_dict`` — strips the behavioral-profile link (``profile_ref``) and the
    audit trail; allow-list so a future Person field can't silently leak.
  * ``build_digest`` — filters blocked, sorts admitted-before-contacts, caps to N, emits
    slim rows; ``total`` reflects the count before the cap.
  * **coherence** — a digest built from ``resolve_persons`` matches the resolved Persons
    field-for-field (the admin and the bot cannot diverge — the whole point of the aspect).

FAKE ids + ``*.example`` / ``example.net`` emails only (docs/PLACEHOLDER_NAMING.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import roster_resolver as rr  # noqa: E402
from evolve_admin.user_directory import bot_view  # noqa: E402
from evolve_admin.user_directory import model  # noqa: E402
from evolve_admin.user_directory import resolver as udr  # noqa: E402
from evolve_admin.user_directory import storage as uds  # noqa: E402


BOT = "team_bot_a"


# ── Fixtures (mirror test_user_directory) ─────────────────────────────────


def _network(tmp_path: Path) -> dict:
    return {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {BOT: {"role": "member", "port": 19002, "multiUser": True}},
        "pod": {"admins": {
            "external_ids": {"telegram": ["999"]}, "names": {}, "resolved_names": {}}},
    }


def _shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _roster(net: dict, shared: Path, allow: dict) -> list:
    return rr.resolve_roster(
        net, BOT, shared_dir=shared, activity={},
        allowfrom_reader=lambda ch: allow.get(ch))


def _person(**kw) -> model.Person:
    kw.setdefault("person_id", "pers_0000000000000001")
    return model.Person(**kw)


# ── person_to_bot_dict: strips profile + audit (invariant #5) ─────────────


def test_person_to_bot_dict_omits_profile_ref_and_audit():
    p = _person(
        names={"display": "Dana Lopez"},
        identities=[model.Identity("telegram", "70000001", handle="@dana")],
        emails=[model.Email("dana@example.net", "primary", "bot-asserted")],
        contact={"org": "Example LLC"},
        membership=model.Membership(admitted=True, role="participant"),
        profile_ref=".openclaw/profiles/dana.md",
        audit=[model.AuditEntry(
            field="emails", from_=None, to="x", by="operator@host",
            source="operator-verified", at="2026-06-22T00:00:00Z")],
    )
    d = bot_view.person_to_bot_dict(p)
    # The behavioral-profile link and the audit trail NEVER cross to the bot.
    assert "profile_ref" not in d
    assert "audit" not in d
    # Everything the bot legitimately needs IS present.
    assert d["person_id"] == "pers_0000000000000001"
    assert d["names"] == {"display": "Dana Lopez"}
    assert d["identities"][0]["handle"] == "@dana"
    assert d["emails"][0]["addr"] == "dana@example.net"
    assert d["contact"] == {"org": "Example LLC"}
    assert d["membership"]["role"] == "participant"


def test_person_to_bot_dict_is_allowlist_not_denylist():
    """The projection NAMES the fields it emits — so the only keys that can ever reach
    the bot are the known-safe ones. A regression that added a sensitive field to Person
    can't silently leak it through here."""
    d = bot_view.person_to_bot_dict(_person())
    assert set(d.keys()) == {
        "person_id", "names", "identities", "emails", "contact", "membership"}


def test_person_to_bot_dict_contact_has_null_membership():
    d = bot_view.person_to_bot_dict(_person())  # no membership ⇒ pure contact
    assert d["membership"] is None


# ── build_digest: filter / sort / cap / count ─────────────────────────────


def _admitted(pid: str, display: str, role: str = "participant") -> model.Person:
    return model.Person(
        person_id=pid, names={"display": display},
        identities=[model.Identity("telegram", pid[-6:])],
        membership=model.Membership(admitted=True, role=role))


def _contact(pid: str, display: str, email: str | None = None) -> model.Person:
    emails = [model.Email(email, "primary", "bot-asserted")] if email else []
    return model.Person(
        person_id=pid, names={"display": display},
        identities=[model.Identity("email", email or pid)], emails=emails)


def test_build_digest_filters_blocked():
    persons = [
        _admitted("pers_a", "Active One", role="participant"),
        _admitted("pers_b", "Blocked One", role="blocked"),
    ]
    out = bot_view.build_digest(persons, cap=10)
    ids = [r["person_id"] for r in out["persons"]]
    assert "pers_a" in ids
    assert "pers_b" not in ids  # blocked never advertised to the bot
    assert out["total"] == 1


def test_build_digest_caps_and_reports_total():
    persons = [_contact(f"pers_{i:02d}", f"C{i:02d}", f"c{i}@x.example")
               for i in range(5)]
    out = bot_view.build_digest(persons, cap=2)
    assert out["shown"] == 2
    assert len(out["persons"]) == 2
    assert out["total"] == 5  # pre-cap count drives the "+N more" overflow line
    assert out["cap"] == 2


def test_build_digest_sorts_admitted_before_contacts_then_name():
    persons = [
        _contact("pers_z", "Zeb Contact", "zeb@x.example"),
        _admitted("pers_m", "Mara User"),
        _contact("pers_a", "Ana Contact", "ana@x.example"),
        _admitted("pers_b", "Bo User"),
    ]
    out = bot_view.build_digest(persons, cap=10)
    order = [r["display"] for r in out["persons"]]
    # Admitted (alpha) first, then contacts (alpha).
    assert order == ["Bo User", "Mara User", "Ana Contact", "Zeb Contact"]


def test_build_digest_row_fields_and_contact_role():
    p = model.Person(
        person_id="pers_row", names={"display": "Dana Lopez"},
        identities=[model.Identity("telegram", "70000001", handle="dana")],
        emails=[model.Email("dana@example.net", "primary", "bot-asserted"),
                model.Email("dana@acme.example", "secondary", "operator-verified")])
    row = bot_view.build_digest([p], cap=10)["persons"][0]
    assert row["display"] == "Dana Lopez"
    assert row["handle"] == "@dana"           # normalized to @-prefix
    assert row["identity"] == "telegram:70000001"
    assert row["primary_email"] == "dana@example.net"   # the rank:primary one
    assert row["role"] == "contact"           # no membership ⇒ contact


def test_build_digest_empty():
    assert bot_view.build_digest([], cap=10) == {
        "persons": [], "total": 0, "shown": 0, "cap": 10}


# ── coherence: digest matches resolve_persons (invariant #1) ──────────────


def test_digest_coheres_with_resolve_persons(tmp_path):
    """The bot's digest is built from the SAME join the admin Users page uses — so each
    row's fields equal the resolved Person's fields. Admin and bot cannot diverge."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    # One admitted user (pod admin 999) carrying a directory email…
    uds.upsert_entry(
        shared, BOT, "telegram", "999", by=BOT, provenance="operator-verified",
        emails=[{"addr": "admin@example.net", "rank": "primary"}])
    # …and one pure contact.
    uds.upsert_entry(
        shared, BOT, "email", "dana@acme.example", by=BOT, provenance="bot-asserted",
        emails=[{"addr": "dana@acme.example", "rank": "primary"}],
        names={"display": "Dana Lopez"})
    roster = _roster(net, shared, {"telegram": ["999"]})
    persons = udr.resolve_persons(net, BOT, shared_dir=shared, roster=roster)

    digest = bot_view.build_digest(persons, cap=25)
    expected = bot_view.build_digest(persons, cap=25)  # deterministic
    assert digest == expected

    # Each row's primary email + role match the resolved Person it came from.
    by_id = {p.person_id: p for p in persons}
    for row in digest["persons"]:
        person = by_id[row["person_id"]]
        pe = person.primary_email
        assert row["primary_email"] == (pe.addr if pe else "")
        assert row["role"] == (
            person.membership.role if person.membership else "contact")
        assert row["identity"] == (
            f"{person.identities[0].platform}:{person.identities[0].id}"
            if person.identities else "")
