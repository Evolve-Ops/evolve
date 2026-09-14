"""Unit + coherence tests for the canonical roster resolver.

``roster_resolver`` is the single allowlist+overlay+name+activity join that both the
admin Users-page GET and (in F1b) the bot's roster projection consume — invariant #1
of internal/spec-identity-meta-2026-06-15.md ("one source of truth, projected"). These
tests cover:

  * ``build_record`` — the per-identity join (name/role/engagement/activity/labels/
    rights).
  * ``resolve_roster`` — the full-read entry point, both with an injected allowFrom
    reader (pure, no I/O) and through the production bot-home file reader.
  * The **coherence regression lock** the spec backlog names: every Users-page-admitted
    identity is resolvable by name through the resolver — the exact failure that left a
    bot blind to verified users.
  * A **byte-identical proof**: the resolver, mapped through the GET's own
    record→entry mapper, reproduces the GET's ``approved[]`` exactly — i.e. the page
    and the resolver are genuinely one join.

Placeholder-only data per docs/PLACEHOLDER_NAMING.md: bot ``team_bot_a``, users
``P1``–``P4``, opaque ids like ``U0AAAAAAAA`` / ``U0XXXXXXXX``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Flask

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin import roster_overlay as ro  # noqa: E402
from evolve_admin import roster_resolver as rr  # noqa: E402
from evolve_admin.web import routes_bot_users as rbu  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────


def _network(tmp_path: Path, **overrides) -> dict:
    """Minimal network dict the resolver can read."""
    net = {
        "networkId": "test-pod",
        "sharedDir": str(tmp_path / "shared"),
        "bots": {
            "team_bot_a": {"role": "member", "port": 19002, "multiUser": True},
        },
        "pod": {
            "admins": {
                # Telegram 999 is a pod admin.
                "external_ids": {"telegram": ["999"]},
                "names": {},
                "resolved_names": {},
            },
        },
    }
    net.update(overrides)
    return net


def _shared(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── build_record: the per-identity join ──────────────────────────────────


def test_build_record_resolves_name_role_engagement_activity(tmp_path):
    """A participant with a cached name and recent activity resolves into a fully
    populated record."""
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {}, "names": {},
            "resolved_names": {
                "slack:U0AAAAAAAA": {
                    "name": "P1", "username": "p1_handle",
                    "cached_at": "2026-06-10T00:00:00Z"},
            },
        }},
    )
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    activity = {"slack:U0AAAAAAAA": {"last_ts": "2026-06-14T10:00:00Z",
                                     "turns_7d": 5}}
    rec = rr.build_record(
        net, "team_bot_a", "slack", "U0AAAAAAAA",
        overlay=overlay, activity=activity, shared_dir=shared)
    assert rec.platform == "slack"
    assert rec.stable_id == "U0AAAAAAAA"
    assert rec.display_name == "P1"
    assert rec.name_source == "resolved_names"
    assert rec.handle == "p1_handle"
    assert rec.role == "participant"
    assert rec.engagement_surfaces  # non-empty
    assert rec.rights_summary == "chat only"
    assert rec.last_seen == "2026-06-14T10:00:00Z"
    assert rec.turn_count == 5
    assert rec.labels == []


def test_build_record_pod_admin_label_role_and_rights(tmp_path):
    """Pod-admin claim → role ``admin``, ``pod_admin`` label, full-control rights."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    rec = rr.build_record(
        net, "team_bot_a", "telegram", "999",
        overlay=overlay, activity={}, shared_dir=shared)
    assert rec.role == "admin"
    assert "pod_admin" in rec.labels
    assert rec.rights_summary == "full control"


def test_build_record_primary_owner_label_and_rights(tmp_path):
    """Primary-owner claim → role ``primary_user``, ``owner`` label, bot-scoped rights."""
    net = _network(
        tmp_path,
        bots={"team_bot_a": {
            "role": "member", "port": 19002, "multiUser": True,
            "primary_user": {"external_ids": {"slack": "U0OWNER000"},
                             "name": "P2"},
        }},
    )
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    rec = rr.build_record(
        net, "team_bot_a", "slack", "U0OWNER000",
        overlay=overlay, activity={}, shared_dir=shared)
    assert rec.role == "primary_user"
    assert "owner" in rec.labels
    assert rec.display_name == "P2"
    assert rec.name_source == "bot_primary"
    # rights_summary reflects the *effective* capability set, honoring the overlay's
    # role_bindings — and a freshly-loaded overlay binds primary_user to ["*"]
    # (see roster_overlay._empty_overlay), the same binding _check_capability
    # enforces. So the digest says "full control", matching what the user can
    # actually do. (The narrower DEFAULT_ROLE_CAPABILITIES path is covered by
    # test_rights_summary_primary_user_default_when_no_binding.)
    assert rec.rights_summary == "full control"


def test_build_record_no_activity_yields_none_turn_count(tmp_path):
    """No activity record → last_seen/turn_count are None (the GET then omits the
    last_seen / turns_7d keys)."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    rec = rr.build_record(
        net, "team_bot_a", "telegram", "111",
        overlay=overlay, activity={}, shared_dir=shared)
    assert rec.last_seen is None
    assert rec.turn_count is None
    assert rec.role == "participant"
    assert rec.display_name is None
    assert rec.name_source == "allowlist_only"


def test_build_record_activity_zero_turns_is_not_none(tmp_path):
    """An activity record with turns_7d=0 (seen >7d ago) still yields turn_count 0,
    not None — so the GET emits the key, matching pre-resolver behavior."""
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    activity = {"telegram:111": {"last_ts": "2026-05-20T00:00:00Z", "turns_7d": 0}}
    rec = rr.build_record(
        net, "team_bot_a", "telegram", "111",
        overlay=overlay, activity=activity, shared_dir=shared)
    assert rec.turn_count == 0
    assert rec.last_seen == "2026-05-20T00:00:00Z"


def test_build_record_blocked_rights(tmp_path):
    net = _network(tmp_path)
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    ro.block_identity(overlay, "telegram", "111", by="admin:pod-admin", reason="x")
    rec = rr.build_record(
        net, "team_bot_a", "telegram", "111",
        overlay=overlay, activity={}, shared_dir=shared)
    assert rec.role == "blocked"
    assert rec.rights_summary == "blocked"


def test_build_record_email_surfaced_for_slack(tmp_path):
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {}, "names": {},
            "resolved_names": {
                "slack:U0EMAIL000": {"name": "P3", "email": "p3@example.test"},
            },
        }},
    )
    shared = _shared(tmp_path)
    overlay = ro.load_overlay(shared, "team_bot_a")
    rec = rr.build_record(
        net, "team_bot_a", "slack", "U0EMAIL000",
        overlay=overlay, activity={}, shared_dir=shared)
    assert rec.email == "p3@example.test"


# ── rights_summary ───────────────────────────────────────────────────────


def test_rights_summary_participant_default(tmp_path):
    overlay = ro.load_overlay(_shared(tmp_path), "team_bot_a")
    assert rr.rights_summary("participant", overlay) == "chat only"


def test_rights_summary_admin_is_full_control(tmp_path):
    overlay = ro.load_overlay(_shared(tmp_path), "team_bot_a")
    assert rr.rights_summary("admin", overlay) == "full control"


def test_rights_summary_primary_user_default_when_no_binding():
    """With no role_bindings (the capabilities-module DEFAULT path), primary_user gets
    the bot-scoped admin set — not apps/code/bindings."""
    summary = rr.rights_summary("primary_user", {"role_bindings": {}})
    assert summary == "roster, channels, config, external reach"


def test_rights_summary_honors_overlay_binding(tmp_path):
    """A retuned role binding shows up in the summary — it's computed from the
    effective capability set, not hardcoded per role."""
    overlay = ro.load_overlay(_shared(tmp_path), "team_bot_a")
    overlay["role_bindings"]["participant"] = ["bot.app.install"]
    assert rr.rights_summary("participant", overlay) == "apps"


# ── resolve_roster: full read ────────────────────────────────────────────


def test_resolve_roster_injected_reader_multi_channel(tmp_path):
    """resolve_roster with an injected allowFrom reader joins across channels in a
    deterministic (channel-order, allowFrom-order) sequence — no filesystem."""
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {"telegram": ["999"]}, "names": {},
            "resolved_names": {
                "slack:U0AAAAAAAA": {"name": "P1"},
                "slack:U0BBBBBBBB": {"name": "P2"},
            },
        }},
    )
    allow = {"slack": ["U0AAAAAAAA", "U0BBBBBBBB"], "telegram": ["999"]}
    recs = rr.resolve_roster(
        net, "team_bot_a",
        shared_dir=_shared(tmp_path),
        activity={},
        allowfrom_reader=lambda ch: allow.get(ch),
    )
    keyed = [(r.platform, r.stable_id, r.display_name, r.role) for r in recs]
    assert keyed == [
        ("telegram", "999", None, "admin"),  # telegram before slack (channel order)
        ("slack", "U0AAAAAAAA", "P1", "participant"),
        ("slack", "U0BBBBBBBB", "P2", "participant"),
    ]


def test_resolve_roster_skips_empty_and_falsy_ids(tmp_path):
    net = _network(tmp_path)
    recs = rr.resolve_roster(
        net, "team_bot_a", shared_dir=_shared(tmp_path), activity={},
        allowfrom_reader=lambda ch: ["111", ""] if ch == "telegram" else None,
    )
    assert [r.stable_id for r in recs] == ["111"]


def test_resolve_roster_default_reader_reads_bot_home(tmp_path, monkeypatch):
    """The production path: no injected reader → resolve_roster reads the bot's
    allowFrom files via the routes reader (bot_home monkeypatched to tmp)."""
    net = _network(tmp_path)
    monkeypatch.setattr(rbu, "bot_home", lambda bot, n: tmp_path / "Users" / bot)
    creds = tmp_path / "Users" / "team_bot_a" / ".openclaw" / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["999", "111"]}))
    recs = rr.resolve_roster(net, "team_bot_a", shared_dir=_shared(tmp_path),
                             activity={})
    by_id = {r.stable_id: r for r in recs}
    assert set(by_id) == {"999", "111"}
    assert by_id["999"].role == "admin"
    assert by_id["111"].role == "participant"


# ── to_dict: canonical projection ────────────────────────────────────────


def test_record_to_dict_is_canonical_projection(tmp_path):
    """to_dict carries the bot-facing projection and excludes admin-only fields."""
    net = _network(tmp_path)
    overlay = ro.load_overlay(_shared(tmp_path), "team_bot_a")
    rec = rr.build_record(net, "team_bot_a", "telegram", "999",
                          overlay=overlay, activity={}, shared_dir=_shared(tmp_path))
    d = rec.to_dict()
    assert set(d) == {
        "platform", "stable_id", "display_name", "handle", "role",
        "engagement_surfaces", "rights_summary", "last_seen", "turn_count",
    }
    # admin-only presentation fields are NOT in the bot projection
    assert "labels" not in d and "email" not in d and "name_source" not in d


# ── Channel-set guard ────────────────────────────────────────────────────


def test_channels_match_known_providers():
    """ROSTER_CHANNELS must stay in lockstep with the Users-page provider set so the
    resolver and the page span the same channels."""
    assert rr.ROSTER_CHANNELS == rbu.KNOWN_PROVIDERS


# ── Coherence regression lock (spec §7) ──────────────────────────────────


def test_coherence_admitted_identity_is_resolvable_by_name(tmp_path):
    """THE regression lock for the carve bug: an identity Evolve admitted — with a
    verified ID and a name in the roster — is resolvable *by name* through the
    resolver. The original failure was the bot being blind to exactly this:
    admitted-with-ID users it could not name.

    Mirrors the §6 scenario: four admitted Slack users P1–P4, one of them (P4) with
    only a verified ID (U0XXXXXXXX) and a name. All four come back named.
    """
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {}, "names": {},
            "resolved_names": {
                "slack:U0AAAAAAAA": {"name": "P1"},
                "slack:U0BBBBBBBB": {"name": "P2"},
                "slack:U0CCCCCCCC": {"name": "P3"},
                "slack:U0XXXXXXXX": {"name": "P4"},  # the previously-invisible one
            },
        }},
    )
    allow = ["U0AAAAAAAA", "U0BBBBBBBB", "U0CCCCCCCC", "U0XXXXXXXX"]
    recs = rr.resolve_roster(
        net, "team_bot_a", shared_dir=_shared(tmp_path), activity={},
        allowfrom_reader=lambda ch: allow if ch == "slack" else None)

    by_name = {r.display_name: r for r in recs}
    # Every admitted identity is resolvable by name — none fall through to a bare ID.
    assert set(by_name) == {"P1", "P2", "P3", "P4"}
    assert all(r.display_name is not None for r in recs)
    # And the previously-blind user resolves to its verified ID.
    assert by_name["P4"].stable_id == "U0XXXXXXXX"


# ── Byte-identical proof: resolver IS the page's join ─────────────────────


def test_resolver_reproduces_get_approved_entries_exactly(tmp_path, monkeypatch):
    """The Users-page GET and ``resolve_roster`` produce the *same* roster join: mapping
    resolver records through the GET's own ``_record_to_approved_entry`` reproduces
    the page's ``approved[]`` once the **additive** ``directory`` block is set aside.
    This is the consolidation invariant enforced in a test — there is one roster join,
    consumed identically.

    Phase 1 of spec-user-directory-2026-06-22 layers a nested ``directory`` block on each
    entry (sourced through ``resolve_person`` — the one read path). That layer is purely
    additive: stripping it must leave the byte-identical roster projection. We assert
    both halves — the block is present, and the remainder matches the resolver exactly —
    so neither the roster contract nor the directory seam can regress silently."""
    net = _network(
        tmp_path,
        pod={"admins": {
            "external_ids": {"telegram": ["999"]}, "names": {},
            "resolved_names": {
                "telegram:999": {"name": "P1", "username": "p1"},
                "telegram:111": {"name": "P2"},
            },
        }},
    )
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(net))
    monkeypatch.setattr(rbu, "bot_home", lambda bot, n: tmp_path / "Users" / bot)
    creds = tmp_path / "Users" / "team_bot_a" / ".openclaw" / "credentials"
    creds.mkdir(parents=True, exist_ok=True)
    allow_list = ["999", "111"]
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": allow_list}))

    # 1. What the page returns.
    app = Flask(__name__)
    rbu.register_routes(app, network_path)
    with app.test_client() as c:
        page = c.get("/api/admin/bots/team_bot_a/users").get_json()
    page_approved = page["by_channel"]["telegram"]["approved"]

    # 2. What the resolver returns, mapped through the GET's own mapper.
    recs = rr.resolve_roster(
        net, "team_bot_a", shared_dir=Path(net["sharedDir"]), activity={},
        allowfrom_reader=lambda ch: allow_list if ch == "telegram" else None)
    resolver_approved = [rbu._record_to_approved_entry(r) for r in recs]

    # Every page entry carries the additive directory block (the seam is live)...
    assert all("directory" in e for e in page_approved)
    assert all(e["directory"]["person_id"].startswith("pers_") for e in page_approved)
    # ...and stripping it reproduces the roster projection byte-for-byte.
    page_without_directory = [
        {k: v for k, v in e.items() if k != "directory"} for e in page_approved]
    assert resolver_approved == page_without_directory


# ── R1a: group/channel allowlist (config-level, distinct from DM store) ───
#
# ``channels.<ch>`` group access (groupPolicy: allowlist) is a SEPARATE
# OpenClaw gate from the credentials DM pairing store the ``approved`` list
# reads. These cover the pure fall-through helper and the per-channel reader.


def test_effective_group_allowlist_requires_allowlist_policy():
    # Only groupPolicy=="allowlist" yields a managed list; open/disabled/
    # absent are not allowlist-gated → None (nothing to surface/manage).
    assert rr.effective_group_allowlist(
        {"groupPolicy": "open", "allowFrom": ["U1"]}) is None
    assert rr.effective_group_allowlist({"groupPolicy": "disabled"}) is None
    assert rr.effective_group_allowlist({"allowFrom": ["U1"]}) is None
    assert rr.effective_group_allowlist(None) is None
    assert rr.effective_group_allowlist({}) is None


def test_effective_group_allowlist_prefers_group_allow_from():
    block = {"groupPolicy": "allowlist",
             "groupAllowFrom": ["U1", "U2"], "allowFrom": ["U9"]}
    assert rr.effective_group_allowlist(block) == ["U1", "U2"]


def test_effective_group_allowlist_falls_back_to_allow_from():
    # No groupAllowFrom + default fallback (true) → the group gate IS
    # allowFrom. This is the R1a-diagnosed shape.
    block = {"groupPolicy": "allowlist", "allowFrom": ["U1", "U2"]}
    assert rr.effective_group_allowlist(block) == ["U1", "U2"]


def test_effective_group_allowlist_fallback_disabled_yields_empty():
    block = {"groupPolicy": "allowlist", "allowFrom": ["U1"],
             "groupAllowFromFallbackToAllowFrom": False}
    assert rr.effective_group_allowlist(block) == []


def test_effective_group_allowlist_explicit_empty_group_list_does_not_fall_through():
    # groupAllowFrom present-but-empty means "no group members" — fail
    # closed, do NOT fall through to allowFrom.
    block = {"groupPolicy": "allowlist", "groupAllowFrom": [], "allowFrom": ["U1"]}
    assert rr.effective_group_allowlist(block) == []


def test_effective_group_allowlist_drops_wildcard_and_non_ids():
    block = {"groupPolicy": "allowlist",
             "allowFrom": ["U1", "*", "", "  ", 123, None, "U2"]}
    assert rr.effective_group_allowlist(block) == ["U1", "U2"]


def test_effective_group_allowlist_non_list_allow_from_yields_empty():
    # Malformed allowFrom (a bare string instead of a list) must NOT be
    # iterated char-by-char — the non-list guard returns []. Fail-closed.
    block = {"groupPolicy": "allowlist", "allowFrom": "U1"}
    assert rr.effective_group_allowlist(block) == []


def test_read_group_allowlists_injected_reader_only_group_gated():
    net = {"bots": {"team_bot_a": {}}}
    oc = {"channels": {
        "slack": {"groupPolicy": "allowlist", "allowFrom": ["U1", "U2"]},
        "telegram": {"groupPolicy": "open", "allowFrom": ["111"]},   # open → omit
        "discord": {"groupPolicy": "allowlist", "allowFrom": []},    # empty → omit
    }}
    got = rr.read_group_allowlists(net, "team_bot_a", oc_reader=lambda: oc)
    assert got == {"slack": ["U1", "U2"]}


def test_read_group_allowlists_missing_or_malformed_config():
    assert rr.read_group_allowlists(
        {}, "team_bot_a", oc_reader=lambda: None) == {}
    assert rr.read_group_allowlists(
        {}, "team_bot_a", oc_reader=lambda: {"channels": "nope"}) == {}
