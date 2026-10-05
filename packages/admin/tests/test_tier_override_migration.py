"""tests/test_tier_override_migration.py — moving caps that are already stranded.

Fixing the writer does nothing for a value already sitting where routing does
not look, and the operator has no way to notice: the CLI printed a green check
when it wrote it. ``evolve-admin models migrate-tier-overrides`` finds those
values and relocates them.

The rules it must not break, one test each: never overwrite a canonical value,
never invent one, never launder an untrusted mirror into the routing config,
and be safe to run twice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ._tier_pod import (  # noqa: E402 — must follow the sys.path bootstrap
    BOT,
    flat as _flat,
    make_pod,
    run as _run,
    tiers_file as _tiers_file,
)


@pytest.fixture
def pod(tmp_path, monkeypatch):
    return make_pod(tmp_path, monkeypatch)


def _canonical(pod, doc: dict) -> None:
    (pod["home"] / ".openclaw" / "evolve-tiers.json").write_text(
        json.dumps(doc, indent=2)
    )


def _write_mirror(pod, doc: dict) -> None:
    path = pod["shared"] / BOT / "tiers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2))


def _members(pod) -> None:
    """Put BOT on the pod roster — the command sweeps ``network.members``."""
    net = json.loads(pod["network_path"].read_text())
    net["members"] = [BOT]
    pod["network_path"].write_text(json.dumps(net))


def _migrate(pod, *args):
    _members(pod)
    return _run(pod, ["models", "migrate-tier-overrides", *args])


def _effective(pod) -> int:
    from evolve_admin.power_cap import resolve_effective_power_cap

    return resolve_effective_power_cap(
        json.loads(pod["network_path"].read_text()), _tiers_file(pod),
    )


# ── generation 1: the value only ever reached the shared-dir mirror ──────────

def test_a_cap_stranded_in_the_mirror_is_reported_then_moved(pod):
    _canonical(pod, {})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 3}})

    dry = _migrate(pod)
    assert dry.exit_code == 0, dry.output
    assert "would move" in _flat(dry)
    assert "roleCaps" not in _tiers_file(pod), "dry run must not write"

    applied = _migrate(pod, "--apply")
    assert applied.exit_code == 0, applied.output
    assert _effective(pod) == 3


def test_the_move_names_where_the_value_came_from(pod):
    """A migration that changes routing without saying what it moved, from
    where, is the same invisible failure it exists to repair."""
    _canonical(pod, {})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 3}})
    out = _flat(_migrate(pod, "--apply"))
    assert "3/day" in out
    assert "mirror" in out
    assert "roleCaps.power.maxPerDayPerBot" in out


# ── generation 2: #4023 wrote the canonical file, but the legacy key ─────────

def test_a_cap_under_the_canonical_legacy_key_is_lifted(pod):
    """What ``models cap`` produced between #4023 and this change: the right
    file, the shadowed key. Nothing about the value changes — it moves."""
    _canonical(pod, {"userTierOverride": {"dailyCap": 7}})
    assert _effective(pod) == 10, "precondition: the legacy key does not route"

    assert _migrate(pod, "--apply").exit_code == 0
    assert _effective(pod) == 7
    assert _tiers_file(pod)["userTierOverride"]["dailyCap"] == 7


def test_the_canonical_legacy_key_wins_over_a_disagreeing_mirror(pod):
    """The mirror is bot-writable; the canonical file is the one routing
    reads. When both carry a value, prefer the one that is not forgeable
    through the #3565 shared-dir ACE."""
    _canonical(pod, {"userTierOverride": {"dailyCap": 7}})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 99}})
    _migrate(pod, "--apply")
    assert _effective(pod) == 7


# ── never overwrite, never invent ───────────────────────────────────────────

def test_a_cap_that_already_routes_is_left_alone(pod):
    _canonical(pod, {"roleCaps": {"power": {"maxPerDayPerBot": 4}}})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 3}})
    out = _flat(_migrate(pod, "--apply"))
    assert _effective(pod) == 4
    assert "the mirror says 3" in out
    assert f"models cap {BOT}" in out, "a conflict must name how to settle it"


def test_a_pod_wide_cap_does_not_make_a_stranded_bot_look_migrated(pod):
    """"Has this bot been migrated?" is a presence question about the BOT's
    own key, and it has to stay one. Routing resolves 6/day here from the pod
    layer, so a check that asked "does a cap resolve?" would report this bot
    as fine and leave its operator's 3 stranded forever.

    The row the command prints, though, is the RESOLVER's answer — so after
    the move it reads 3, the bot layer correctly beating the pod's 6.
    """
    _canonical(pod, {"roleCaps": {"max": {"maxPerDayPerBot": 2}}})
    net = json.loads(pod["network_path"].read_text())
    net["models"] = {"roleCaps": {"power": {"maxPerDayPerBot": 6}}}
    pod["network_path"].write_text(json.dumps(net))
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 3}})
    assert _effective(pod) == 6, "precondition: the pod layer already answers"

    assert "would move" in _flat(_migrate(pod))
    assert _migrate(pod, "--apply").exit_code == 0
    assert _effective(pod) == 3
    assert "already uses 3/day" in _flat(_migrate(pod))


def test_a_bot_with_no_cap_anywhere_gets_no_cap_invented(pod):
    _canonical(pod, {})
    out = _flat(_migrate(pod, "--apply"))
    assert "no per-bot cap set anywhere" in out
    assert "roleCaps" not in _tiers_file(pod)


def test_a_junk_value_is_not_relocated(pod):
    """Moving it would only reproduce it at the new key, where the resolver
    discards it anyway — while the row misreported a cap as migrated."""
    _canonical(pod, {})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 1e9}})
    out = _flat(_migrate(pod, "--apply"))
    assert "no per-bot cap set anywhere" in out
    assert "roleCaps" not in _tiers_file(pod)


def test_running_it_twice_changes_nothing_the_second_time(pod):
    _canonical(pod, {})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 3}})
    _migrate(pod, "--apply")
    first = _tiers_file(pod)

    second = _migrate(pod, "--apply")
    assert second.exit_code == 0
    assert "already uses 3/day" in _flat(second)
    assert _tiers_file(pod) == first


# ── the mirror is untrusted input ───────────────────────────────────────────

def test_an_untrusted_mirror_is_refused_not_promoted(pod, monkeypatch):
    """The bot holds ``add_file`` on ``{sharedDir}/{bot}/`` (#3565), so it can
    replace its own ``tiers.json``. ``home_chat_routes`` bounds a forged cap at
    ``_UNTRUSTED_DAILY_CAP_CEILING``; seeding one into the canonical routing
    config would launder it straight past that clamp."""
    from evolve_admin.web import home_chat_routes

    _canonical(pod, {})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 100}})
    monkeypatch.setattr(home_chat_routes, "_trusted_uids", lambda: {999_999})

    out = _flat(_migrate(pod, "--apply"))
    assert "not trusted" in out
    assert "roleCaps" not in _tiers_file(pod)
    assert _effective(pod) == 10


def test_a_trusted_mirror_still_migrates(pod, monkeypatch):
    """The differential half of the case above — without it, the refusal test
    would still pass if the gate rejected every mirror."""
    import os

    from evolve_admin.web import home_chat_routes

    _canonical(pod, {})
    _write_mirror(pod, {"userTierOverride": {"dailyCap": 3}})
    monkeypatch.setattr(
        home_chat_routes, "_trusted_uids", lambda: {os.getuid()},
    )
    assert _migrate(pod, "--apply").exit_code == 0
    assert _effective(pod) == 3


# ── the sweep must survive one unreachable bot ──────────────────────────────

def test_a_bot_whose_config_cannot_be_read_is_reported_not_skipped(pod, monkeypatch):
    import oc_cli

    monkeypatch.setattr(
        oc_cli, "oc_full_config_get", lambda bot_id, network_path=None: None,
    )
    out = _flat(_migrate(pod, "--apply"))
    assert "could not read" in out


def test_a_read_that_raises_names_the_bot_and_the_reason(pod, monkeypatch):
    """One unreachable bot must not abort the sweep, and must not read as
    "nothing to do" either — the operator has to see which bot and why."""
    import oc_cli

    def boom(bot_id, network_path=None):
        raise RuntimeError("ssh: connection refused")

    monkeypatch.setattr(oc_cli, "oc_full_config_get", boom)
    result = _migrate(pod, "--apply")
    assert result.exit_code == 0, result.output
    out = _flat(result)
    assert BOT in out
    assert "ssh: connection refused" in out
