"""Tests for ``users_roster`` — the pod's people, read-only.

`internal/dispatch/done/users-roster-read-only-surface.md` Verify clause: a fixture
roster with three users across two bots and two apps renders and resolves as expected
(golden JSON); ``whoami`` for a user outside an app's audience returns the access
design's refusal, not an empty record; no write path exists (grep-level test).

The fixture (placeholder-only ids per docs/PLACEHOLDER_NAMING.md):

  * ``P1`` — telegram ``111``, participant on ``team_bot_a`` only.
  * ``P2`` — slack ``U0OWNER000``, primary_user (owner) on ``team_bot_a``.
  * ``P3`` — telegram ``999``, pod admin, admitted on BOTH bots via the SAME id —
    exercises the cross-bot person merge (one row, two ``bots`` entries).

  * ``app-everyone`` (audience ``everyone``) installed on ``team_bot_a`` only.
  * ``app-owners`` (audience ``owners``) installed on both bots.

``roster_reader`` is injected so these tests never touch a filesystem or ``sudo``
fallback — the roster join itself is covered by ``test_roster_resolver.py``; this
file covers the person-merge, app-audience join, and the two verbs.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin import roster_resolver as rr  # noqa: E402
from evolve_admin import users_roster as ur  # noqa: E402
from evolve_admin.applications.app_spec import (  # noqa: E402
    AUDIENCE_EVERYONE,
    AUDIENCE_NAMED,
    AUDIENCE_OWNERS,
)

BOT_A = "team_bot_a"
BOT_B = "team_bot_b"


def _network(**overrides) -> dict:
    net = {
        "networkId": "test-pod",
        "sharedDir": "/nonexistent/shared",
        "bots": {
            BOT_A: {"role": "member", "port": 19002, "multiUser": True,
                    "primary_user": {"external_ids": {"slack": ["U0OWNER000"]},
                                     "name": "P2"}},
            BOT_B: {"role": "member", "port": 19003, "multiUser": True},
        },
        "pod": {"admins": {"external_ids": {"telegram": ["999"]}, "names": {}}},
    }
    net.update(overrides)
    return net


def _rec(**kwargs) -> rr.RosterRecord:
    defaults = dict(
        handle=None, engagement_surfaces=["dm"], rights_summary="chat only",
        last_seen=None, turn_count=None, labels=[], name_source="allowlist_only",
        email=None,
    )
    defaults.update(kwargs)
    return rr.RosterRecord(**defaults)


def _roster_reader(network, bot_id, shared_dir):
    rosters = {
        BOT_A: [
            _rec(platform="telegram", stable_id="111", display_name="P1",
                 role="participant", rights_summary="chat only"),
            _rec(platform="slack", stable_id="U0OWNER000", display_name="P2",
                 role="primary_user", labels=["owner"], rights_summary="full control"),
            _rec(platform="telegram", stable_id="999", display_name="P3",
                 role="admin", labels=["pod_admin"], rights_summary="full control"),
        ],
        BOT_B: [
            _rec(platform="telegram", stable_id="999", display_name="P3",
                 role="admin", labels=["pod_admin"], rights_summary="full control"),
        ],
    }
    return rosters.get(bot_id, [])


def _manifest(app_id: str, name: str, audience_operator: str) -> dict:
    return {
        "id": app_id,
        "app_id": app_id,
        "name": name,
        "definition_status": "defined",
        "identity": {"purpose": f"{name} does one thing."},
        "audience_scoping": {"operator": audience_operator},
        "created_at": "2026-08-01T00:00:00Z",
        "schema_version": 30,
    }


def _read_manifests(bot_id: str):
    everyone = _manifest("app-everyone", "Everyone App", "open")
    owners = _manifest("app-owners", "Owners App", "operator_only")
    by_bot = {
        BOT_A: [("app-everyone", everyone), ("app-owners", owners)],
        BOT_B: [("app-owners", owners)],
    }
    return by_bot.get(bot_id, [])


def _list_users(network=None):
    return ur.list_users(
        network or _network(),
        bots=[BOT_A, BOT_B],
        roster_reader=_roster_reader,
        read_manifests=_read_manifests,
    )


# ── list_users: the golden shape ──────────────────────────────────────────


def test_three_users_two_bots_two_apps_golden_shape():
    users = _list_users()
    by_name = {u["display_name"]: u for u in users}
    assert set(by_name) == {"P1", "P2", "P3"}

    p1, p2, p3 = by_name["P1"], by_name["P2"], by_name["P3"]

    # P1 — participant on bot_a only, sees only the everyone-audience app.
    assert [b["bot_id"] for b in p1["bots"]] == [BOT_A]
    assert p1["bots"][0]["role"] == "participant"
    assert [a["app_id"] for a in p1["apps"]] == ["app-everyone"]
    assert p1["apps"][0]["access"] == ur.GRANTED

    # P2 — primary_user/owner on bot_a only, sees BOTH apps there.
    assert [b["bot_id"] for b in p2["bots"]] == [BOT_A]
    assert {a["app_id"] for a in p2["apps"]} == {"app-everyone", "app-owners"}

    # P3 — the SAME telegram id admitted on both bots merges into ONE record
    # with two bot memberships (cross-bot person merge, D1).
    assert p3["person_key"] == "telegram:999"
    assert sorted(b["bot_id"] for b in p3["bots"]) == [BOT_A, BOT_B]
    # owners-audience app is visible via EITHER bot install.
    app_bot_pairs = {(a["app_id"], a["bot_id"]) for a in p3["apps"]}
    assert ("app-owners", BOT_A) in app_bot_pairs
    assert ("app-owners", BOT_B) in app_bot_pairs
    assert ("app-everyone", BOT_A) in app_bot_pairs

    # Linked accounts: no per-user account store exists yet (module docstring) —
    # tri-state unknown, never a guessed count.
    for u in users:
        assert u["linked_accounts"] == {"status": "unknown"}


def test_owners_audience_excludes_a_participant():
    users = _list_users()
    p1 = next(u for u in users if u["display_name"] == "P1")
    assert "app-owners" not in {a["app_id"] for a in p1["apps"]}


def test_named_audience_is_unknown_not_denied_for_display():
    """No named-list is stored anywhere on this pod — the roster is silent, and
    silence renders as the tri-state ``unknown``, never a guessed grant/deny."""
    manifests = {
        BOT_A: [("app-named", _manifest("app-named", "Named App", "named_users"))],
    }
    users = ur.list_users(
        _network(), bots=[BOT_A], roster_reader=_roster_reader,
        read_manifests=lambda bot_id: manifests.get(bot_id, []),
    )
    p1 = next(u for u in users if u["display_name"] == "P1")
    # DENIED entries are dropped; UNKNOWN survives as a visible, honest tri-state.
    named_entries = [a for a in p1["apps"] if a["app_id"] == "app-named"]
    assert len(named_entries) == 1
    assert named_entries[0]["access"] == ur.UNKNOWN


# ── whoami / list_for_caller: the two verbs ───────────────────────────────


def test_whoami_returns_own_record_for_everyone_audience():
    users = _list_users()
    result = ur.whoami(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=AUDIENCE_EVERYONE, users=users,
    )
    assert isinstance(result, dict)
    assert result["display_name"] == "P1"


def test_whoami_refuses_when_caller_outside_owners_audience():
    """A participant is outside an owners-audience app — REFUSED, never an empty
    or null record (the Verify clause, verbatim)."""
    users = _list_users()
    result = ur.whoami(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=AUDIENCE_OWNERS, users=users,
    )
    assert isinstance(result, ur.Refusal)
    assert result.reason
    assert result.to_dict() == {"refused": True, "reason": result.reason}


def test_whoami_refuses_for_unresolvable_named_audience_fail_closed():
    """Design-app-access §5's own rule: an unresolvable ``named`` audience fails
    CLOSED for enforcement — even for someone the DISPLAY side calls 'unknown'."""
    users = _list_users()
    result = ur.whoami(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=AUDIENCE_NAMED, users=users,
    )
    assert isinstance(result, ur.Refusal)


def test_whoami_allows_owner_for_owners_audience():
    users = _list_users()
    result = ur.whoami(
        _network(), bot_id=BOT_A, platform="slack", stable_id="U0OWNER000",
        app_audience=AUDIENCE_OWNERS, users=users,
    )
    assert isinstance(result, dict)
    assert result["display_name"] == "P2"


def test_list_for_caller_everyone_audience_returns_whole_bot_roster():
    users = _list_users()
    result = ur.list_for_caller(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=AUDIENCE_EVERYONE, users=users,
    )
    assert isinstance(result, list)
    assert {u["display_name"] for u in result} == {"P1", "P2", "P3"}


def test_list_for_caller_owners_audience_returns_only_self_for_a_non_owner():
    """The access design's rule, applied: an app not open to everyone returns
    only the caller's own record — never the rest of the bot's roster."""
    users = _list_users()
    result = ur.list_for_caller(
        _network(), bot_id=BOT_A, platform="slack", stable_id="U0OWNER000",
        app_audience=AUDIENCE_OWNERS, users=users,
    )
    assert isinstance(result, list)
    assert [u["display_name"] for u in result] == ["P2"]


def test_list_for_caller_refuses_a_non_owner_for_owners_audience():
    users = _list_users()
    result = ur.list_for_caller(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=AUDIENCE_OWNERS, users=users,
    )
    assert isinstance(result, ur.Refusal)


def test_no_app_audience_is_unrestricted_like_a_plain_conversation():
    """``app_audience=None`` — not app-mediated — behaves like today's
    unscoped bot conversation: no refusal, full bot roster from ``list``."""
    users = _list_users()
    whoami_result = ur.whoami(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=None, users=users,
    )
    assert isinstance(whoami_result, dict)
    list_result = ur.list_for_caller(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=None, users=users,
    )
    assert {u["display_name"] for u in list_result} == {"P1", "P2", "P3"}


# ── Cross-platform merge (D1, via roster_identity — reused, not re-derived) ──


def test_primary_user_recorded_on_two_channels_merges_into_one_person():
    """A bot's OWN primary_user row recording both a Telegram and a Slack id for
    the same owner (an operator assertion, not a guess) collapses the two
    roster records into one person — the case the base (platform, stable_id)
    key alone cannot merge, and exactly what ``roster_identity`` exists for."""
    net = _network(bots={
        BOT_A: {"role": "member", "port": 19002, "multiUser": True,
                "primary_user": {
                    "external_ids": {"slack": ["U0OWNER000"], "telegram": ["222"]},
                    "name": "P2"}},
    })

    def reader(network, bot_id, shared_dir):
        return [
            _rec(platform="slack", stable_id="U0OWNER000", display_name="P2",
                 role="primary_user", labels=["owner"]),
            _rec(platform="telegram", stable_id="222", display_name="P2",
                 role="primary_user", labels=["owner"]),
        ]

    users = ur.list_users(
        net, bots=[BOT_A], roster_reader=reader, read_manifests=lambda b: [])
    assert len(users) == 1
    channels = {(c["platform"], c["stable_id"]) for c in users[0]["channels"]}
    assert channels == {("slack", "U0OWNER000"), ("telegram", "222")}


def test_pod_admin_bag_never_merges_two_different_admins():
    """Two DIFFERENT pod admins must never collapse into one row just because
    both resolve to the pod-admin bag — that bag is 'a bag of admin
    identities, not a person' (roster_identity's own boundary)."""
    net = _network()
    net["pod"]["admins"]["external_ids"]["telegram"] = ["999", "888"]

    def reader(network, bot_id, shared_dir):
        return [
            _rec(platform="telegram", stable_id="999", display_name="P3",
                 role="admin", labels=["pod_admin"]),
            _rec(platform="telegram", stable_id="888", display_name="P4",
                 role="admin", labels=["pod_admin"]),
        ]

    users = ur.list_users(
        net, bots=[BOT_A], roster_reader=reader, read_manifests=lambda b: [])
    assert {u["display_name"] for u in users} == {"P3", "P4"}


# ── No write path exists (grep-level) ─────────────────────────────────────


_WRITE_VERBS = ("write", "save", "mutate", "delete", "remove", "set_", "update",
                "create", "grant", "revoke", "block", "unblock", "patch")


# ── The two Hold items from reviews/pr-4409.md ──────────────────────────────
#
# Both were fail-OPEN paths in the ENFORCEMENT half: one reachable by naming an
# app that does not resolve, one reachable by not being on the bot at all. Each
# test below fails against the pre-fix module.


def test_unresolved_app_id_denies_instead_of_returning_the_roster():
    """Hold 1. An ``app_id`` that resolves to nothing is app-mediated with an
    UNKNOWN audience — never "not app-mediated". Returning the bot roster here
    is how a ``named`` app reached everyone by passing a typo."""
    users = _list_users()
    result = ur.list_for_caller(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=ur.AUDIENCE_UNRESOLVED, users=users,
    )
    assert isinstance(result, ur.Refusal)
    assert "could not be resolved" in result.reason


def test_unresolved_app_id_denies_whoami_too():
    users = _list_users()
    result = ur.whoami(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="111",
        app_audience=ur.AUDIENCE_UNRESOLVED, users=users,
    )
    assert isinstance(result, ur.Refusal)
    assert "could not be resolved" in result.reason


def test_app_audience_sentinel_is_not_a_legal_audience_value():
    """The sentinel must never collide with a real manifest audience, or a
    manifest could name itself into the deny branch (or out of it)."""
    from evolve_admin.applications.app_spec import (
        AUDIENCE_EVERYONE, AUDIENCE_NAMED, AUDIENCE_OWNERS,
    )
    assert ur.AUDIENCE_UNRESOLVED not in (
        AUDIENCE_EVERYONE, AUDIENCE_OWNERS, AUDIENCE_NAMED)
    # And it denies through the ordinary unrecognized-audience branch.
    assert ur._enforce_access(ur.AUDIENCE_UNRESOLVED, "admin") is False
    assert ur._display_access(ur.AUDIENCE_UNRESOLVED, "admin") == ur.UNKNOWN


def test_app_audience_returns_the_sentinel_for_an_unknown_app(monkeypatch):
    """The route-level composition the old test never reached: the helper the
    bot routes actually call must report unresolved, not None."""
    monkeypatch.setattr(
        ur, "_apps_by_bot", lambda *a, **k: {BOT_A: []})
    assert ur.app_audience(_network(), BOT_A, "no-such-app") == ur.AUDIENCE_UNRESOLVED


def test_app_audience_returns_the_sentinel_when_the_manifest_read_raises(monkeypatch):
    """A transient manifest read failure must not downgrade a ``named`` app to
    unrestricted — the blanket except used to return None."""
    def _boom(*a, **k):
        raise OSError("simulated manifest read failure")

    monkeypatch.setattr(ur, "_apps_by_bot", _boom)
    assert ur.app_audience(_network(), BOT_A, "some-app") == ur.AUDIENCE_UNRESOLVED


def test_app_audience_is_none_only_when_no_app_id_is_supplied():
    assert ur.app_audience(_network(), BOT_A, None) is None
    assert ur.app_audience(_network(), BOT_A, "") is None


def test_list_refuses_an_unadmitted_caller_even_for_everyone():
    """Hold 2. ``everyone`` passes every ROLE, but admission is a different
    question — and ``whoami`` already refuses this caller."""
    users = _list_users()
    result = ur.list_for_caller(
        _network(), bot_id=BOT_A, platform="telegram", stable_id="not-admitted",
        app_audience=AUDIENCE_EVERYONE, users=users,
    )
    assert isinstance(result, ur.Refusal)
    assert "admitted" in result.reason


def test_the_two_verbs_agree_on_an_unadmitted_caller():
    """The asymmetry itself, pinned: whichever way they answer, they answer the
    same way. This is what made the broader verb leakier than the narrower one."""
    users = _list_users()
    kwargs = dict(
        bot_id=BOT_A, platform="telegram", stable_id="not-admitted", users=users)
    for audience in (None, AUDIENCE_EVERYONE):
        w = ur.whoami(_network(), app_audience=audience, **kwargs)
        lst = ur.list_for_caller(_network(), app_audience=audience, **kwargs)
        assert isinstance(w, ur.Refusal), audience
        assert isinstance(lst, ur.Refusal), audience
        assert w.reason == lst.reason == "caller is not an admitted identity on this bot"


def test_module_exposes_no_mutating_function():
    """Every top-level ``def`` in ``users_roster.py`` is a read — none of them
    write anything. Grep-level by design: a future contributor adding a writer
    to this module (rather than a sibling) trips this before review does."""
    source = (Path(ur.__file__)).read_text()
    tree = ast.parse(source)
    top_level_funcs = [
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    offenders = [
        name for name in top_level_funcs
        if any(verb in name.lower() for verb in _WRITE_VERBS)
    ]
    assert offenders == [], f"users_roster.py defines write-shaped function(s): {offenders}"

    # And no direct filesystem write call anywhere in the module.
    assert ".write_text(" not in source
    assert "open(" not in source or "'w'" not in source
