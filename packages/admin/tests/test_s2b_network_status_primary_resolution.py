"""S2b regression — ``network_status()`` keys the primary admin bot under the
resolved ``primary_bot.primary_bot_id`` instead of the literal ``"evolve"``.

Spec: internal/spec-evo-account-separation-2026-05-25.md. Sibling of
``test_s2_dashboard_primary_resolution.py`` (#3057, the dashboard-card/uptime
write) — this guards the OTHER hardcoded ``"evolve"`` literal in the same
account-separation surface: the ``network_status()`` block that injects the
admin bot into ``/api/status``'s ``bots`` map (what the SPA renders as the
per-bot TABS on Plugins / Users / AI-Optimization).

The bug: that block unconditionally wrote ``result["bots"]["evolve"]``. On a
post-separation pod the primary is ``evo`` (``network.primary == "evo"``), so the
literal injected a PHANTOM second ``evolve`` tab beside the real primary. It was
sticky because the setup-checklist enricher iterates the ``/api/status`` bots
map and re-scaffolds ``network.bots.evolve.setup_checklist`` on every call — so a
manual ``jq 'del(.bots.evolve)'`` never stuck. The fix is on the read-path
(``network_status``): resolve the id via ``primary_bot_id`` and key under that,
so the enricher only ever iterates the real primary.

Backward-compatible by construction: on a legacy pod the resolver returns
``"evolve"`` (top-level fallback), so the entry still keys under ``"evolve"``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN))
sys.path.insert(0, str(_ADMIN.parent / "analyzer"))

from evolve_admin import status  # noqa: E402
from evolve_admin.web import routes_setup_checklist as rsc  # noqa: E402


def _patch_probes(monkeypatch) -> None:
    """Stub the liveness probes so the test never touches the network.

    ``_oc_status`` (member-loop probe) is replaced with an offline stub, and
    ``urllib.request.urlopen`` (the primary block's gateway probe) is forced to
    raise — both legs are wrapped in try/except, so the status dict degrades to
    ``live=False`` from probing and the block then force-marks the primary
    ``online`` exactly as in production. Keeps the test fast and deterministic.
    """
    monkeypatch.setattr(status, "_oc_status", lambda bot_id: (False, None))

    def _boom(*a, **k):  # pragma: no cover - trivial raiser
        raise OSError("no gateway in test")

    monkeypatch.setattr("urllib.request.urlopen", _boom)


def _run(monkeypatch, net: dict) -> dict:
    """Run ``network_status`` against an in-memory network dict."""
    _patch_probes(monkeypatch)
    monkeypatch.setattr(status, "load_network", lambda path=None: net)
    # sharedDir points at a non-existent path so the metrics/proposals reads
    # short-circuit on .exists() — the test is about the bots map, not metrics.
    net.setdefault("sharedDir", "/tmp/evolve-s2b-nonexistent-dir")
    return status.network_status(Path("/tmp/unused-network.json"))


# ── Case 1: evo-primary (dedicated) — keyed under "evo", no phantom ────────────


def test_evo_primary_keys_under_evo_no_phantom(monkeypatch):
    """Post-separation roster: primary ``evo`` (a DEDICATED primary, not a
    member), ``darwin`` a member, and NO legacy ``bots.evolve``. The response
    surfaces ``evo`` as the primary tab and invents no ``evolve`` tab."""
    net = {
        "networkId": "test",
        "primary": "evo",
        "members": ["darwin"],
        "bots": {
            "evo": {"role": "primary", "port": 19030, "user": "evo"},
            "darwin": {"role": "member", "port": 19001},
        },
    }
    result = _run(monkeypatch, net)
    bots = result["bots"]
    # The real primary surfaces under its real id, marked primary + online…
    assert "evo" in bots
    assert bots["evo"]["role"] == "primary"
    assert bots["evo"]["status"] == "online"
    assert bots["evo"]["live"] is True
    assert bots["evo"]["port"] == 19030
    # …the member is present…
    assert "darwin" in bots
    # …and NO phantom "evolve" tab.
    assert "evolve" not in bots


# ── Case 2: legacy evolve-primary — still keyed under "evolve" ─────────────────


def test_legacy_evolve_primary_keys_under_evolve(monkeypatch):
    """Legacy pod: ``primary`` unset, a bot literally named ``evolve`` fills the
    role (the resolver's ``"evolve"`` fallback). The entry still keys under
    ``"evolve"`` — no regression for the mini and other pre-separation pods."""
    net = {
        "networkId": "test",
        # No top-level "primary" — exercises the primary_bot_id legacy fallback.
        "members": ["darwin"],
        "bots": {
            "evolve": {"port": 19030},
            "darwin": {"role": "member", "port": 19001},
        },
    }
    result = _run(monkeypatch, net)
    bots = result["bots"]
    assert "evolve" in bots
    assert bots["evolve"]["role"] == "primary"
    assert bots["evolve"]["status"] == "online"
    assert bots["evolve"]["port"] == 19030


# ── Case 3: stray leftover bots.evolve (not a member) — no phantom ─────────────


def test_stray_bots_evolve_not_surfaced(monkeypatch):
    """An evo-primary pod that still carries a stray leftover ``bots.evolve``
    (e.g. one a prior buggy build re-scaffolded) — but ``evolve`` is NOT in
    ``members``. The hardcode is gone, so the stray never becomes a tab: the
    member loop skips non-members and the primary block keys under ``evo``."""
    net = {
        "networkId": "test",
        "primary": "evo",
        "members": ["evo", "darwin"],
        "bots": {
            "evo": {"role": "primary", "port": 19030, "user": "evo"},
            "darwin": {"role": "member", "port": 19001},
            # Stray leftover from the old hardcode + enricher re-scaffold.
            "evolve": {"setup_checklist": {"foo": "bar"}},
        },
    }
    result = _run(monkeypatch, net)
    bots = result["bots"]
    assert "evo" in bots
    assert bots["evo"]["role"] == "primary"
    assert "evolve" not in bots


# ── Case 4: existing-bot-as-primary (member) — enriched in place, not clobbered ─


def test_member_as_primary_not_clobbered(monkeypatch):
    """Existing-bot-as-primary setup: the primary IS a network member, so the
    member loop already built its card (with ``multiUser`` etc.). The primary
    block must enrich that card in place — mark it online + primary — without
    discarding the member-loop fields."""
    net = {
        "networkId": "test",
        "primary": "teambot",
        "members": ["teambot"],
        "bots": {
            "teambot": {
                "role": "primary",
                "port": 19000,
                "multiUser": True,
                "user": "teamhost",
            },
        },
    }
    result = _run(monkeypatch, net)
    bots = result["bots"]
    assert set(bots.keys()) == {"teambot"}
    assert bots["teambot"]["role"] == "primary"
    assert bots["teambot"]["status"] == "online"
    assert bots["teambot"]["live"] is True
    # Member-loop field survived the enrich (was NOT clobbered by a fresh dict).
    assert bots["teambot"]["multiUser"] is True
    assert "evolve" not in bots


# ── Case 5: end-to-end — the enricher synthesizes no phantom evolve ───────────


def test_enricher_re_scaffold_chain_broken(monkeypatch, tmp_path):
    """Feed the real ``network_status`` bots map through the setup-checklist
    enricher (the surface that re-scaffolded ``bots.evolve``). With the read-path
    keyed under ``evo``, the enricher never iterates ``evolve``, so neither the
    response nor ``network.bots`` gains a phantom ``evolve`` entry."""
    monkeypatch.setattr(rsc, "_load_openclaw_cfg", lambda bot_id, net: None)
    monkeypatch.setattr(
        "evolve_admin.config.bot_home",
        lambda bot, net=None: tmp_path / "Users" / bot,
    )
    net = {
        "networkId": "test",
        "primary": "evo",
        "members": ["darwin"],
        "bots": {
            "evo": {"role": "primary", "port": 19030, "user": "evo"},
            "darwin": {"role": "member", "port": 19001},
        },
    }
    result = _run(monkeypatch, net)
    bots = result["bots"]
    assert "evolve" not in bots

    rsc.enrich_tiles_with_setup_chip(net, bots)
    # The enricher iterated only the real ids — no phantom evolve anywhere.
    assert "evolve" not in bots
    assert "evolve" not in net["bots"]
    # The real primary is still present (the pass actually ran over it).
    assert "evo" in bots
