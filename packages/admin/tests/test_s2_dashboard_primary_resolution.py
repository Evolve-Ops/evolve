"""S2 regression — dashboard card / uptime resolves the primary via
``primary_bot.primary_bot_id`` instead of the legacy literal ``"evolve"``.

Spec: internal/spec-evo-account-separation-2026-05-25.md. On a post-separation
pod the primary bot is ``evo`` (account-separated from ``evolve``), so a
correctly-provisioned ``network.json`` has ``bots = {"evo": role=primary}``
with NO ``bots["evolve"]`` entry.

Two render-layer bugs this guards against:

  1. **Lost uptime.** ``/api/status`` augmented ``data["bots"]["evolve"]``
     with ``admin_uptime_seconds``. Keyed on the literal "evolve", the
     guard was always false on an evo-primary pod, so the real primary's
     card never received uptime. The fix resolves the target id via
     ``primary_bot_id(network)`` → ``"evo"``.

  2. **Phantom card.** The setup-checklist enrichment
     (``enrich_tiles_with_setup_chip``) scaffolds a ``Setup X/N`` chip onto
     every bot already present in ``data["bots"]``. It must NOT synthesize an
     ``evolve`` entry of its own — once #1 stops minting ``bots['evolve']``,
     no phantom ``evolve · primary · Setup 0/8`` card can appear. This test
     asserts the enrichment invents nothing for an evo-primary roster.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))
sys.path.insert(0, str(_ADMIN.parent / "analyzer"))

from primary_bot import primary_bot_id  # noqa: E402
from evolve_admin.web import routes_setup_checklist as rsc  # noqa: E402


def _evo_primary_network() -> dict:
    """A correctly-provisioned (post-S1) evo-primary roster: the primary is
    ``evo`` and there is NO legacy ``evolve`` bot entry."""
    return {
        "networkId": "test",
        "primary": "evo",
        "sharedDir": "/tmp/evolve-test",
        "bots": {
            "evo": {"role": "primary", "port": 19000, "user": "evo"},
            "team_bot_a": {"role": "member", "port": 19001},
        },
    }


def _augment_primary_uptime(data: dict, network: dict, uptime_seconds: int) -> None:
    """Faithful copy of the server.py:/api/status uptime-augment block under
    test — keeps the assertion pinned to the exact production logic without
    spinning up the Flask app. If the production block changes shape, mirror
    it here."""
    _primary_id = primary_bot_id(network)
    if _primary_id and _primary_id in (data.get("bots") or {}):
        data["bots"][_primary_id]["admin_uptime_seconds"] = uptime_seconds


# ── Half 1: uptime lands on the real primary (evo), never on "evolve" ──────


def test_primary_resolves_to_evo_not_legacy_literal():
    net = _evo_primary_network()
    assert primary_bot_id(net) == "evo"
    assert "evolve" not in (net.get("bots") or {})


def test_uptime_lands_on_evo_card():
    net = _evo_primary_network()
    data = {"bots": {"evo": {"tile": {}}, "team_bot_a": {"tile": {}}}}
    _augment_primary_uptime(data, net, uptime_seconds=4242)
    # Uptime landed on the real primary…
    assert data["bots"]["evo"]["admin_uptime_seconds"] == 4242
    # …and NOT on a phantom "evolve" key (the legacy-literal bug).
    assert "evolve" not in data["bots"]
    # …and not smeared onto member bots.
    assert "admin_uptime_seconds" not in data["bots"]["team_bot_a"]


def test_uptime_noop_when_primary_card_absent():
    """If the resolved primary isn't in the rendered bots dict (e.g. a
    transient roster), the augment is a clean no-op — no KeyError, no
    auto-vivified phantom entry."""
    net = _evo_primary_network()
    data = {"bots": {"team_bot_a": {"tile": {}}}}
    _augment_primary_uptime(data, net, uptime_seconds=99)
    assert "evo" not in data["bots"]
    assert "evolve" not in data["bots"]


# ── Half 2: the checklist enrichment synthesizes NO phantom evolve card ────


def test_enrichment_invents_no_evolve_card(monkeypatch, tmp_path):
    """``enrich_tiles_with_setup_chip`` only keys bots already present in the
    response. On an evo-primary roster it must not mint a phantom ``evolve``
    tile / Setup chip."""
    monkeypatch.setattr(rsc, "_load_openclaw_cfg", lambda bot_id, net: None)
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    net = _evo_primary_network()
    bots = {
        "evo": {"tile": {"health_chips": []}},
        "team_bot_a": {"tile": {"health_chips": []}},
    }
    rsc.enrich_tiles_with_setup_chip(net, bots)
    # No phantom key was synthesized…
    assert set(bots.keys()) == {"evo", "team_bot_a"}
    assert "evolve" not in bots
    # …and the real primary did get its setup chip (proves the pass ran).
    evo_chip_ids = [c["id"] for c in bots["evo"]["tile"]["health_chips"]]
    assert "setup_progress" in evo_chip_ids
