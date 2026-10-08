"""Tests for roster_coherence_monitor.

The monitor reports identities a bot's ``openclaw.json`` names that no Evolve
roster source knows. We unit-test the pure harvest/gap helpers directly, and the
orchestration (fire, dedup, sweep-resolve on reconcile, the unreadable fail-safe
on BOTH sides) with an in-memory fake for the ``evolve_admin`` seams and the REAL
signal store writing to a tmp shared dir.

Bot ids and platform ids here are role placeholders (docs/PLACEHOLDER_NAMING.md)
— never real accounts.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import roster_coherence_monitor as monitor  # noqa: E402


# Placeholder ids. The guild-nested one is the live-violation shape: a real
# human whose platform id exists only in openclaw.json's guild allowlist.
TEAM_BOT = "team_bot_b"
OTHER_BOT = "team_bot_a"
KNOWN_ID = "100000000000000001"
UNKNOWN_ID = "100000000000000002"
SECOND_UNKNOWN_ID = "100000000000000003"


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_harvest_flat_allowfrom():
    block = {"dmPolicy": "pairing", "allowFrom": [KNOWN_ID, UNKNOWN_ID]}
    assert monitor.harvest_identities(block) == {KNOWN_ID, UNKNOWN_ID}


def test_harvest_nested_guild_users():
    """The live-violation shape: identity only in a nested guild member list."""
    block = {
        "token": "redacted",
        "guilds": {"900000000000000001": {"users": [UNKNOWN_ID]}},
    }
    assert monitor.harvest_identities(block) == {UNKNOWN_ID}


def test_harvest_deeply_nested_guild_channel_users():
    block = {
        "guilds": {
            "900000000000000001": {
                "users": [KNOWN_ID],
                "channels": {"800000000000000001": {"users": [UNKNOWN_ID]}},
            }
        }
    }
    assert monitor.harvest_identities(block) == {KNOWN_ID, UNKNOWN_ID}


def test_harvest_group_allowfrom_and_dm_block():
    block = {
        "groupAllowFrom": [KNOWN_ID],
        "dm": {"allowFrom": [UNKNOWN_ID]},
    }
    assert monitor.harvest_identities(block) == {KNOWN_ID, UNKNOWN_ID}


def test_harvest_skips_roles_and_policy_lists():
    """Role ids are not people; tool/skill names are not identities."""
    block = {
        "allowFrom": [KNOWN_ID],
        "guilds": {
            "900000000000000001": {
                "roles": ["700000000000000001"],
                "tools": {"allow": ["bash"], "deny": ["web"]},
                "toolsBySender": {UNKNOWN_ID: {"allow": ["bash"]}},
                "channels": {"800000000000000001": {"skills": ["notes"]}},
            }
        },
        "groupChannels": ["-100200300"],
    }
    assert monitor.harvest_identities(block) == {KNOWN_ID}


def test_harvest_drops_wildcard_and_blanks():
    block = {"allowFrom": ["*", "", "   ", KNOWN_ID, 12345, None]}
    assert monitor.harvest_identities(block) == {KNOWN_ID}


def test_harvest_depth_bounded():
    """A pathologically nested config terminates rather than spinning."""
    node = {"users": [UNKNOWN_ID]}
    for _ in range(monitor._MAX_WALK_DEPTH + 5):
        node = {"nested": node}
    assert monitor.harvest_identities(node) == set()


def test_read_oc_identities_unreadable_sentinel():
    """None config → UNREADABLE, never an empty map (a blind tick is not clean)."""
    assert monitor.read_oc_identities(None, ("telegram",)) is monitor.UNREADABLE


def test_read_oc_identities_empty_config_is_clean_not_unreadable():
    assert monitor.read_oc_identities({}, ("telegram",)) == {}


def test_read_oc_identities_only_configured_channels():
    oc = {"channels": {
        "telegram": {"allowFrom": [KNOWN_ID]},
        "slack": {"allowFrom": []},
    }}
    assert monitor.read_oc_identities(oc, ("telegram", "slack", "discord")) == {
        "telegram": {KNOWN_ID},
    }


def test_channel_policies():
    oc = {"channels": {"discord": {"groupPolicy": "allowlist", "dmPolicy": "pairing"}}}
    assert monitor.channel_policies(oc, ("discord", "slack")) == {
        "discord": {"dmPolicy": "pairing", "groupPolicy": "allowlist"},
    }


def test_coherence_gaps():
    oc = {"discord": {KNOWN_ID, UNKNOWN_ID}, "telegram": {KNOWN_ID}}
    known = {"discord": {KNOWN_ID}, "telegram": {KNOWN_ID}}
    assert monitor.coherence_gaps(oc, known) == {"discord": [UNKNOWN_ID]}


def test_coherence_gaps_unknown_channel_is_all_gap():
    oc = {"discord": {UNKNOWN_ID}}
    assert monitor.coherence_gaps(oc, {}) == {"discord": [UNKNOWN_ID]}


# ── Channel enumeration comes off the registry ────────────────────────────


def test_coherence_channels_is_registry_projection():
    """Registry-driven (invariant 7), and a real widening over the 4 pairing
    channels the drift sibling covers."""
    pytest.importorskip("evolve_admin.channel_registry")
    from evolve_admin import channel_registry as cr

    chans = monitor.coherence_channels()
    assert set(chans) == {
        c.id for c in cr.all_channels()
        if c.install is not None and c.supports_allowlist
    }
    assert set(cr.pairing_channel_ids()) <= set(chans)
    # Delivery-only rows (no OC install) are never reconciled.
    assert all(cr.get(ch).install is not None for ch in chans)


# ── The real roster-side union (overlay + network.json) ───────────────────


def test_known_roster_identities_unions_overlay_and_network(monkeypatch, tmp_path):
    """Overlay identities/blocked/ignored + network external ids all count as
    'Evolve knows this person'. A blocked identity IS known to the admin."""
    pytest.importorskip("evolve_admin.roster_overlay")
    from evolve_admin import roster_overlay as ro
    from evolve_admin import roster_resolver as rr

    # Stub only the allowlist-file join so the test needs no bot home.
    monkeypatch.setattr(rr, "resolve_roster", lambda *a, **kw: [])

    shared = tmp_path / "shared"
    ro.save_overlay(shared, TEAM_BOT, {
        "identities": {f"discord:{KNOWN_ID}": {"role": "participant"}},
        "blocked": {f"discord:{SECOND_UNKNOWN_ID}": {"by": "admin"}},
        "ignored": {},
    })
    network = {
        "bots": {TEAM_BOT: {"primary_user": {
            "external_ids": {"telegram": "600000000001"}}}},
        "pod": {"admins": {"external_ids": {"telegram": ["600000000002"]}}},
    }

    known = monitor.known_roster_identities(
        network, TEAM_BOT, shared_dir=shared, channels=("discord", "telegram"))

    assert known["discord"] == {KNOWN_ID, SECOND_UNKNOWN_ID}
    assert known["telegram"] == {"600000000001", "600000000002"}


def test_known_roster_identities_counts_the_group_allowlist(tmp_path):
    """REGRESSION (live on the reference pod's Slack team bot, 2026-09-22): an
    identity admitted through
    ``openclaw.json``'s group/channel allowlist is KNOWN. Evolve's own Users page
    renders it under "Channel access · group" and writes it there (R1a PR1/PR2),
    so calling it "unknown to Evolve" made the page contradict itself — 30 ids
    listed by name one card below the panel accusing them.

    ``oc_config`` is the config the caller already read; the known-set must take
    it rather than re-reading (a second read can disagree with the first)."""
    pytest.importorskip("evolve_admin.roster_resolver")

    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    network = {"sharedDir": str(shared), "bots": {TEAM_BOT: {}}}
    oc_config = {"channels": {"discord": {
        "groupPolicy": "allowlist", "allowFrom": [KNOWN_ID]}}}

    known = monitor.known_roster_identities(
        network, TEAM_BOT, shared_dir=shared, channels=("discord",),
        oc_config=oc_config)

    assert KNOWN_ID in known["discord"]
    # …and therefore the gap check is silent on it.
    assert monitor.coherence_gaps({"discord": {KNOWN_ID}}, known) == {}


# ── Orchestration fixtures ────────────────────────────────────────────────


@pytest.fixture
def network_json(tmp_path):
    shared = tmp_path / "shared"
    for sub in ("firing", "snoozed", "archived", "log"):
        (shared / "signals" / sub).mkdir(parents=True)
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(shared),
        "members": [TEAM_BOT, OTHER_BOT],
    }))
    return net, shared


@pytest.fixture
def stub_admin(monkeypatch, network_json):
    """Inject fake evolve_admin.{config,roster_resolver} + stub the monitor's
    registry/roster seams so the orchestration runs without the admin package."""
    net, shared = network_json

    state = {
        "oc": {},       # bot_id -> parsed openclaw.json dict, or None (unreadable)
        "known": {},    # bot_id -> {channel: set(ids)}
        "roster_raises": set(),
    }

    def _reader(network, bot_id):
        def _read():
            return state["oc"].get(bot_id)
        return _read

    rr_mod = types.ModuleType("evolve_admin.roster_resolver")
    rr_mod._default_openclaw_reader = _reader
    # The channels the admin Users page renders — the monitor reads this to
    # decide whether its fix steps may point at the page at all (B3a).
    rr_mod.ROSTER_CHANNELS = ("telegram", "slack", "discord", "whatsapp")

    config_mod = types.ModuleType("evolve_admin.config")
    config_mod.load_network = lambda p: json.loads(Path(p).read_text())

    ea = types.ModuleType("evolve_admin")
    ea.config = config_mod
    ea.roster_resolver = rr_mod
    monkeypatch.setitem(sys.modules, "evolve_admin", ea)
    monkeypatch.setitem(sys.modules, "evolve_admin.config", config_mod)
    monkeypatch.setitem(sys.modules, "evolve_admin.roster_resolver", rr_mod)

    monkeypatch.setattr(monitor, "coherence_channels", lambda: ("discord", "telegram"))

    def _known(network, bot_id, *, shared_dir, channels, oc_config=None):
        if bot_id in state["roster_raises"]:
            raise PermissionError("overlay unreadable")
        base = {ch: set() for ch in channels}
        base.update({k: set(v) for k, v in state["known"].get(bot_id, {}).items()})
        return base

    monkeypatch.setattr(monitor, "known_roster_identities", _known)
    return state, net, shared


def _firing(shared):
    from signals import store as signals_store
    return list(signals_store.iter_signals(shared, subdirs=("firing",)))


# ── Orchestration ─────────────────────────────────────────────────────────


def test_live_violation_shape_fires(stub_admin):
    """ACCEPTANCE: a Discord-only bot with primary_user null and a real human in
    the guild allowlist. The identity exists only in openclaw.json — no roster
    row anywhere — so the coherence check must fire on it."""
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {
        "groupPolicy": "allowlist",
        "guilds": {"900000000000000001": {"users": [UNKNOWN_ID]}},
    }}}
    state["known"][TEAM_BOT] = {}          # primary_user null, empty roster
    state["oc"][OTHER_BOT] = {"channels": {}}

    summary = monitor.run(net)

    assert summary["unknown_identities"] == 1
    fired = _firing(shared)
    assert len(fired) == 1
    sig = fired[0]
    assert sig.type == monitor.GAP_TYPE
    assert sig.bot_id == TEAM_BOT
    assert sig.details["channel"] == "discord"
    assert sig.details["unknown_identities"] == [UNKNOWN_ID]
    assert sig.signature == f"{monitor.PRODUCER}:{monitor.GAP_TYPE}:{TEAM_BOT}/discord"
    assert sig.incident_key == f"{monitor.PRODUCER}:{TEAM_BOT}"


def test_known_identity_does_not_fire(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {"allowFrom": [KNOWN_ID]}}}
    state["known"][TEAM_BOT] = {"discord": {KNOWN_ID}}
    state["oc"][OTHER_BOT] = {"channels": {}}

    summary = monitor.run(net)

    assert summary["unknown_identities"] == 0
    assert _firing(shared) == []


def test_title_reads_correctly_for_one_and_many(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {"allowFrom": [UNKNOWN_ID]}}}
    state["oc"][OTHER_BOT] = {"channels": {"discord": {
        "allowFrom": [UNKNOWN_ID, SECOND_UNKNOWN_ID]}}}

    monitor.run(net)

    titles = {s.bot_id: s.title for s in _firing(shared)}
    assert "1 identity in the config" in titles[TEAM_BOT]
    assert "1 identities" not in titles[TEAM_BOT]
    assert "2 identities in the config" in titles[OTHER_BOT]
    # Titles stay under the signal store's 80-char soft limit.
    assert all(len(t) <= 80 for t in titles.values())


def test_repeat_dedups(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {"allowFrom": [UNKNOWN_ID]}}}
    state["oc"][OTHER_BOT] = {"channels": {}}

    monitor.run(net)
    monitor.run(net)

    fired = _firing(shared)
    assert len(fired) == 1
    assert fired[0].observation_count == 2


def test_reconcile_sweep_resolves(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {"allowFrom": [UNKNOWN_ID]}}}
    state["oc"][OTHER_BOT] = {"channels": {}}
    monitor.run(net)
    assert len(_firing(shared)) == 1

    # Operator admits the identity → it is now roster-known.
    state["known"][TEAM_BOT] = {"discord": {UNKNOWN_ID}}
    summary = monitor.run(net)

    assert summary["signals_resolved"] == 1
    assert _firing(shared) == []


def test_unreadable_config_fires_and_does_not_report_gaps(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = None            # unreadable
    state["oc"][OTHER_BOT] = {"channels": {}}

    summary = monitor.run(net)

    assert summary["unreadable"] == 1
    assert summary["unknown_identities"] == 0
    fired = _firing(shared)
    assert len(fired) == 1
    assert fired[0].type == monitor.UNREADABLE_TYPE
    assert fired[0].details["side"] == "openclaw_config"


def test_unreadable_roster_fires_rather_than_mass_false_positive(stub_admin):
    """A roster-side read failure must NOT report the whole config as unknown."""
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {
        "allowFrom": [KNOWN_ID, UNKNOWN_ID]}}}
    state["roster_raises"].add(TEAM_BOT)
    state["oc"][OTHER_BOT] = {"channels": {}}

    summary = monitor.run(net)

    assert summary["unknown_identities"] == 0
    fired = _firing(shared)
    assert len(fired) == 1
    assert fired[0].type == monitor.UNREADABLE_TYPE
    assert fired[0].details["side"] == "roster"


def test_unreadable_does_not_resolve_prior_gap(stub_admin):
    """A blind tick must leave the bot's existing coherence Signals firing."""
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {"allowFrom": [UNKNOWN_ID]}}}
    state["oc"][OTHER_BOT] = {"channels": {}}
    monitor.run(net)
    assert {s.type for s in _firing(shared)} == {monitor.GAP_TYPE}

    state["oc"][TEAM_BOT] = None
    monitor.run(net)

    types_now = {s.type for s in _firing(shared)}
    assert types_now == {monitor.GAP_TYPE, monitor.UNREADABLE_TYPE}


def test_unreadable_auto_resolves_when_readable_again(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = None
    state["oc"][OTHER_BOT] = {"channels": {}}
    monitor.run(net)
    assert len(_firing(shared)) == 1

    state["oc"][TEAM_BOT] = {"channels": {}}
    monitor.run(net)

    assert _firing(shared) == []


def test_dry_run_writes_nothing(stub_admin):
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {"allowFrom": [UNKNOWN_ID]}}}
    state["oc"][OTHER_BOT] = {"channels": {}}

    summary = monitor.run(net, dry_run=True)

    assert summary["signals_fired"] == 0
    assert _firing(shared) == []


def test_skips_cleanly_without_admin_package(monkeypatch, network_json):
    """Early bootstrap: no evolve_admin → no-op, never a crash."""
    net, _shared = network_json
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _blocked(name, *a, **kw):
        if name.startswith("evolve_admin"):
            raise ImportError("no evolve_admin")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _blocked)
    assert monitor.run(net)["bots_scanned"] == 0


# ── B3a: the Signal names the exact config key ────────────────────────────
#
# "Open the Users page and look at the <channel> access lists" was a lie for an
# id that lives only in a nested guild list on a channel the page cannot manage
# — the page read the top-level allowlist only, so it rendered a gated-but-EMPTY
# channel. The Signal now carries, per id, the key it actually sits at, and says
# whether the page can act on it.

_GUILD = "900000000000000001"
_GUILD_CHANNEL = "800000000000000001"


def test_harvest_identity_paths_records_where_each_id_lives():
    block = {
        "allowFrom": [KNOWN_ID],
        "guilds": {_GUILD: {
            "users": [UNKNOWN_ID, KNOWN_ID],
            "roles": ["700000000000000009"],
            "channels": {_GUILD_CHANNEL: {"users": [SECOND_UNKNOWN_ID]}},
        }},
    }
    assert monitor.harvest_identity_paths(block) == {
        KNOWN_ID: ["allowFrom", f"guilds.{_GUILD}.users"],
        UNKNOWN_ID: [f"guilds.{_GUILD}.users"],
        SECOND_UNKNOWN_ID: [f"guilds.{_GUILD}.channels.{_GUILD_CHANNEL}.users"],
    }
    # The set projection is unchanged — the gap computation still reads it.
    assert monitor.harvest_identities(block) == {
        KNOWN_ID, UNKNOWN_ID, SECOND_UNKNOWN_ID}


def test_read_oc_identity_paths_keeps_the_unreadable_sentinel():
    assert monitor.read_oc_identity_paths(
        None, ("discord",)) is monitor.UNREADABLE
    assert monitor.read_oc_identity_paths({}, ("discord",)) == {}


def test_gap_signal_names_the_nested_config_key(stub_admin):
    """ACCEPTANCE: the live shape — the remediation points at
    `channels.discord.guilds.<gid>.users`, not at "the access lists"."""
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {
        "groupPolicy": "allowlist",
        "guilds": {_GUILD: {"users": [UNKNOWN_ID]}},
    }}}
    state["known"][TEAM_BOT] = {}
    state["oc"][OTHER_BOT] = {"channels": {}}

    monitor.run(net)

    sig = _firing(shared)[0]
    assert sig.details["config_paths"] == {
        UNKNOWN_ID: [f"channels.discord.guilds.{_GUILD}.users"]}
    assert f"channels.discord.guilds.{_GUILD}.users" in sig.body
    # Allowlist-gated Discord IS rendered by the Users page (B3a surfaces the
    # nested lists there), so the steps send the operator to it.
    assert sig.details["page_manageable"] is True
    assert "Users page" in sig.details["fix_steps"]


def test_gap_signal_says_config_edit_when_the_page_cannot_manage_it(stub_admin):
    """A channel that is not allowlist-gated has no managed list on the page —
    the Signal must say so instead of sending the operator to an empty lane."""
    state, net, shared = stub_admin
    state["oc"][TEAM_BOT] = {"channels": {"discord": {
        "groupPolicy": "open",
        "guilds": {_GUILD: {"users": [UNKNOWN_ID]}},
    }}}
    state["known"][TEAM_BOT] = {}
    state["oc"][OTHER_BOT] = {"channels": {}}

    monitor.run(net)

    sig = _firing(shared)[0]
    assert sig.details["page_manageable"] is False
    assert "config_paths" in sig.details
    assert "no managed list" in sig.details["fix_steps"]


def test_page_manageable_is_false_for_a_channel_the_page_does_not_render():
    """The monitor covers 6 channels; the Users page renders the 4 pairing
    ones. Promising a page surface for signal/imessage would be the same class
    of lie B3a fixes."""
    pytest.importorskip("evolve_admin.roster_resolver")
    policies = {"groupPolicy": "allowlist"}
    assert monitor.page_manageable("discord", policies) is True
    assert monitor.page_manageable("signal", policies) is False
    assert monitor.page_manageable("discord", {"groupPolicy": "open"}) is False
