"""Reader-resolve helpers for the primary bot's gateway port + comms mode.

evo-account-separation S1: the provisioner stopped writing a legacy top-level
``network["evolve"]`` block (which a reader assuming the literal ``"evolve"``
would mint a phantom primary surface from). The primary's gateway port + comms
mode now live on its own ``bots[<primary>]`` entry, resolved via
``primary_bot.primary_bot_gateway_port`` / ``primary_bot_comms_mode``.

These tests pin BOTH directions:
  - the NEW shape (data on the primary's bots[] entry), and
  - the LEGACY fallback (old top-level block) so pre-S1 pods keep resolving
    until the S4 migration runs.
"""

import sys
from pathlib import Path

ANALYZER_DIR = Path(__file__).resolve().parents[1]
if str(ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYZER_DIR))

import primary_bot  # noqa: E402


# ── New shape: data on the primary's bots[] entry ─────────────────────────────


def test_gateway_port_resolves_from_primary_bot_entry():
    """A fresh-install network (no top-level evolve block) resolves the port
    from bots[<primary>].port."""
    net = {
        "primary": "evo",
        "bots": {
            "evo": {"role": "primary", "user": "evo", "port": 19030,
                    "comms_mode": "dedicated"},
            "team_bot_a": {"role": "member", "port": 19001},
        },
    }
    assert primary_bot.primary_bot_gateway_port(net) == 19030
    # No phantom top-level block on the fresh-install shape.
    assert "evolve" not in net


def test_comms_mode_resolves_from_primary_bot_entry():
    net = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19030,
                         "comms_mode": "headless"}},
    }
    assert primary_bot.primary_bot_comms_mode(net) == "headless"


def test_gateway_port_resolves_when_primary_is_renamed_bot():
    """Resolution follows primary_bot_id, not the literal 'evolve' — a pod whose
    primary is an existing member bot resolves that bot's port."""
    net = {
        "primary": "team_bot_a",
        "bots": {"team_bot_a": {"role": "primary", "port": 18789,
                                "comms_mode": "dedicated"}},
    }
    assert primary_bot.primary_bot_gateway_port(net) == 18789
    assert primary_bot.primary_bot_comms_mode(net) == "dedicated"


# ── Legacy fallback: old top-level evolve block (pre-S1 pods) ──────────────────


def test_gateway_port_legacy_top_level_block_fallback():
    """A pre-S1 pod that only has the top-level evolve block still resolves."""
    net = {
        "primary": "evolve",
        "bots": {"evolve": {"role": "primary"}},  # no port on the entry
        "evolve": {"gateway_port": 19030, "comms_mode": "dedicated"},
    }
    assert primary_bot.primary_bot_gateway_port(net) == 19030
    assert primary_bot.primary_bot_comms_mode(net) == "dedicated"


def test_primary_bot_entry_wins_over_legacy_block():
    """When both exist, the per-bot entry is authoritative (the new home)."""
    net = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "port": 19030,
                         "comms_mode": "headless"}},
        "evolve": {"gateway_port": 18000, "comms_mode": "dedicated"},
    }
    assert primary_bot.primary_bot_gateway_port(net) == 19030
    assert primary_bot.primary_bot_comms_mode(net) == "headless"


# ── Degenerate inputs ─────────────────────────────────────────────────────────


def test_gateway_port_none_when_unset_everywhere():
    net = {"primary": "evo", "bots": {"evo": {"role": "primary"}}}
    assert primary_bot.primary_bot_gateway_port(net) is None


def test_comms_mode_default_when_unset_everywhere():
    net = {"primary": "evo", "bots": {"evo": {"role": "primary"}}}
    assert primary_bot.primary_bot_comms_mode(net) == ""
    assert primary_bot.primary_bot_comms_mode(net, "dedicated") == "dedicated"


def test_gateway_port_tolerates_string_port():
    net = {"primary": "evo", "bots": {"evo": {"role": "primary", "port": "19030"}}}
    assert primary_bot.primary_bot_gateway_port(net) == 19030


def test_gateway_port_no_primary_resolvable():
    """No primary at all → None, no crash."""
    assert primary_bot.primary_bot_gateway_port({"bots": {}}) is None
    assert primary_bot.primary_bot_comms_mode({"bots": {}}) == ""
