"""tests/test_models_cap_reaches_routing.py — the cap the operator set is the
cap the gateway enforces.

PR #4023 moved ``models cap`` to the file the plugin reads. Its review then
recorded the second half of the same bug: the KEY it wrote,
``userTierOverride.dailyCap``, is legacy, and the merged ``roleCaps`` block
shadows it — measured, on every pod, because ``DEFAULT_MODEL_CATALOG`` ships
``roleCaps.power.maxPerDayPerBot`` in code and the router folds that catalog
in as its base layer. So the operator could set a cap, see the green check,
and route on 10 regardless.

``test_user_tier_override_cli`` covers WHERE the write lands. This file covers
WHAT ROUTING THEN RESOLVES, which is the claim the PR body made. Both fixtures
below (``roleCaps`` present and absent) fail against 00e14727ce18 — the file
was written by the old code and ``resolve_effective_power_cap`` returns 10.
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
    mirror as _mirror,
    run as _run,
    tiers_file as _tiers_file,
)


@pytest.fixture
def pod(tmp_path, monkeypatch):
    return make_pod(tmp_path, monkeypatch)


def _effective_cap(pod) -> int:
    """The cap the gateway would enforce, from the bot's real config on disk.

    Goes through the ONE Python resolver (``power_cap``), the twin of
    ``ModelRouter._roleCap`` — not a hand-walk of the JSON here, which is how
    a test ends up agreeing with a writer that both readers disagree with.
    """
    from evolve_admin.power_cap import resolve_effective_power_cap

    return resolve_effective_power_cap(
        json.loads(pod["network_path"].read_text()), _tiers_file(pod),
    )


def _default_cap() -> int:
    from evolve_admin.power_cap import default_power_cap

    return default_power_cap()


def _seed_role_caps(pod, caps: dict) -> None:
    """Give the bot a canonical ``roleCaps`` block before the CLI runs.

    This is the migrated-pod shape: ``migrate_model_roles`` lifted every bot's
    ``dailyCap`` into ``roleCaps.power.maxPerDayPerBot`` fleet-wide on
    2026-08-15, and "Customize this bot" materializes one too.
    """
    path = pod["home"] / ".openclaw" / "evolve-tiers.json"
    doc = json.loads(path.read_text()) if path.exists() else {}
    doc["roleCaps"] = caps
    path.write_text(json.dumps(doc, indent=2))


# ── item 5: the routing claim, on both config shapes ─────────────────────────

def test_the_cap_reaches_routing_on_a_config_with_no_roleCaps_block(pod):
    """The un-migrated shape. Against 00e14727ce18 this resolves to the code
    default (10) rather than 3, because the CLI wrote only the legacy key."""
    result = _run(pod, ["models", "cap", BOT, "3"])
    assert result.exit_code == 0, result.output
    assert _effective_cap(pod) == 3


def test_the_cap_reaches_routing_on_a_config_that_already_has_roleCaps(pod):
    """The migrated shape — the case the Hold named. Against 00e14727ce18 the
    seeded 25 stands and the operator's 3 is ignored entirely."""
    _seed_role_caps(pod, {"power": {"maxPerDayPerBot": 25}})
    result = _run(pod, ["models", "cap", BOT, "3"])
    assert result.exit_code == 0, result.output
    assert _effective_cap(pod) == 3


# ── item 1: it writes that key by READ-MERGE, not by replacing the block ─────

def test_setting_the_power_cap_does_not_drop_the_max_cap(pod):
    """``roleCaps`` is a wholesale replace at the write seam. A non-merging
    writer would delete ``max`` here — raising this bot's Fable ceiling from 2
    back to the product default as a side effect of LOWERING its Opus cap."""
    _seed_role_caps(pod, {
        "power": {"maxPerDayPerBot": 25}, "max": {"maxPerDayPerBot": 2},
    })
    _run(pod, ["models", "cap", BOT, "3"])
    assert _tiers_file(pod)["roleCaps"]["max"] == {"maxPerDayPerBot": 2}


def test_the_legacy_key_is_still_written_for_the_readers_that_have_not_moved(pod):
    """``home_chat_routes._read_user_tier_override`` reads ``dailyCap`` and
    nothing else. Dropping it would move the bug to the admin chat gate."""
    _run(pod, ["models", "cap", BOT, "3"])
    assert _tiers_file(pod)["userTierOverride"]["dailyCap"] == 3
    assert _mirror(pod)["userTierOverride"]["dailyCap"] == 3


def test_both_keys_land_in_one_write(pod):
    """Two writes could half-land and leave the two readers reporting
    different caps — the exact divergence this chip closes."""
    _run(pod, ["models", "cap", BOT, "3"])
    updates = pod["seen"]["updates"]
    assert updates["userTierOverride"] == {"dailyCap": 3}
    assert updates["roleCaps"]["power"] == {"maxPerDayPerBot": 3}


def test_zero_reaches_routing_as_zero(pod):
    """``cap 0`` is the "stop Power turns" sentinel. Resolving it as a missing
    value would read as 10/day — the opposite of what the operator asked."""
    _run(pod, ["models", "cap", BOT, "0"])
    assert _effective_cap(pod) == 0


def test_a_cap_that_cannot_read_the_current_config_refuses_to_write(pod, monkeypatch):
    """Fail closed. A degraded read looks like "this bot has no roleCaps", and
    merging onto that would REPLACE the block — silently widening every other
    role's cap on a customized bot."""
    import oc_cli

    _seed_role_caps(pod, {"max": {"maxPerDayPerBot": 2}})
    monkeypatch.setattr(
        oc_cli, "oc_full_config_get", lambda bot_id, network_path=None: None,
    )
    result = _run(pod, ["models", "cap", BOT, "3"])
    assert result.exit_code == 1
    # Rich hard-wraps to the terminal width, so match on the collapsed text.
    assert "refusing to replace it" in _flat(result)
    assert _tiers_file(pod)["roleCaps"] == {"max": {"maxPerDayPerBot": 2}}


def test_the_confirmation_line_still_reports_the_value_set(pod):
    result = _run(pod, ["models", "cap", BOT, "3"])
    assert "3" in result.output and "day" in result.output


def test_a_cap_that_routing_would_shadow_is_reported_loudly(pod, monkeypatch):
    """The permanent detector for this whole bug class.

    ``models cap`` reports what the RESOLVER says the gateway will enforce, not
    the number the operator typed — echoing the operator's own number back is
    precisely what it did while routing used a different one. Nothing in the
    current config can produce a divergence (the bot layer wins the merge and
    the CLI range-checks the value first), so the resolver is stubbed to force
    one: what is pinned is that the command READS the answer rather than
    asserting it, and says so when the two differ.
    """
    from evolve_admin import power_cap

    monkeypatch.setattr(
        power_cap, "resolve_effective_power_cap", lambda network, doc: 9,
    )
    out = _flat(_run(pod, ["models", "cap", BOT, "3"]))
    assert "routing will still use 9/day" in out


def test_no_warning_when_the_cap_and_routing_agree(pod):
    """The differential half — without it the test above would pass on a
    command that warned unconditionally."""
    out = _flat(_run(pod, ["models", "cap", BOT, "3"]))
    assert "routing will still use" not in out


# ── item 3: `user-tier-control off` says what it now does ────────────────────

def _help(pod, cmd: str) -> str:
    return _flat(_run(pod, ["models", cmd, "--help"]))


def test_user_tier_control_help_names_self_escalation_as_well_as_the_chip(pod):
    """``off`` trips ``canEscalateToRole`` gate 1 (``feature_disabled``) as
    well as hiding the composer chip, so ``session_set_tier`` is refused too.
    An operator who used ``off`` cosmetically is changing routing behaviour;
    the help has to say so."""
    text = _help(pod, "user-tier-control").lower()
    assert "chip" in text
    assert "self-escalat" in text


def test_turning_it_off_says_both_things_in_the_confirmation_line(pod):
    result = _run(pod, ["models", "user-tier-control", BOT, "off"])
    assert result.exit_code == 0, result.output
    out = _flat(result).lower()
    assert "chip hidden" in out
    assert "self-escalate" in out


def test_turning_it_on_says_both_things_too(pod):
    out = _flat(_run(pod, ["models", "user-tier-control", BOT, "on"])).lower()
    assert "chip shown" in out
    assert "self-escalate" in out


def test_the_cap_help_no_longer_calls_zero_a_chip_toggle(pod):
    """``cap 0`` and ``user-tier-control off`` are different gates
    (``daily_cap_exhausted`` vs ``feature_disabled``). The old wording said
    cap 0 "disables the Power chip", which is the other command's job."""
    text = _help(pod, "cap").lower()
    assert "disable the power chip" not in text
    assert "power turns" in text
