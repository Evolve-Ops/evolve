"""tests/test_model_discovery_adopt.py — adopt a discovered model from the AI
Optimization Model Freshness card (spec §Addendum 12).

The model_discovery generator is signal-only; adoption lives in the admin
helper ``evolve_admin.web.model_discovery_adopt`` + four thin
``/api/models/*`` routes. These cover both: the helper functions (read /
adopt / ignore / adopt-all, driving the AdoptModel applier with NO Proposal)
and the HTTP routes end-to-end through a Flask test client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from signals import store as signals_store  # noqa: E402
from arbiter.appliers import adopt_model  # noqa: E402
from evolve_admin.web import model_discovery_adopt as mda  # noqa: E402


def _discovery_spec(
    provider, model_id, *, rung_slug="", cost_class="medium",
    verdict="fits_existing", role="standard", evidence=None,
):
    """A firing model_discovery Signal spec, as observe_signals emits.

    Defaults to a ``fits_existing`` finding mapped to the ``standard`` role —
    the kind the adopt card surfaces. Pass ``verdict``/``role`` for the
    partition tests (new_tier / mode_variant / specialist / cannot_place)."""
    qualified = f"{provider}/{model_id}"
    ev = evidence if evidence is not None else {
        "context_window": 200000, "max_output_tokens": 8192,
    }
    return {
        "signature": f"model_discovery:{provider}:{model_id}",
        "producer": "model_discovery",
        "type": "model_discovery",
        "severity": "info",
        "scope": "pod",
        "category": "hygiene",
        "title": f"New model available: {qualified}",
        "body": "a new model line exists",
        "details": {
            "provider": provider,
            "model_id": model_id,
            "qualified_id": qualified,
            "suggested_rung_slug": rung_slug,
            "suggested_cost_class": cost_class,
            "suggested_position": 0,
            "cost_band_source": "heuristic",
            "cost_band_evidence": {},
            "placement_verdict": verdict,
            "recommended_role": role if verdict == "fits_existing" else None,
            "recommended_rung_slug": rung_slug or None,
            "fit_reason": "a plain-language fit reason",
            "fit_confidence": 0.8,
            "evidence": ev,
        },
    }


@pytest.fixture
def shared(tmp_path):
    s = tmp_path / "shared"
    s.mkdir()
    return s


@pytest.fixture
def net_state():
    """Back the AdoptModel applier with an in-memory network dict so adopts
    never touch a real network.json. Reset on teardown."""
    state = {"net": {"bots": {}, "models": {"rungs": []}}}
    adopt_model.set_network_io(
        lambda: state["net"], lambda n: state.__setitem__("net", n)
    )
    yield state
    adopt_model.set_network_io(None, None)


def _firing(shared):
    return [
        s for s in signals_store.iter_active(
            shared, producer="model_discovery", state="firing")
        if s.type == "model_discovery"
    ]


# ── list_adoptable_discoveries ────────────────────────────────────────────────

def test_list_adoptable_discoveries_from_firing_signals(shared):
    signals_store.observe(shared, **_discovery_spec("anthropic", "claude-mythos-5"))
    signals_store.observe(shared, **_discovery_spec("openai", "o5-pro"))
    # A degraded-mode signal of a DIFFERENT type must NOT appear as adoptable.
    signals_store.observe(shared, **{
        "signature": "model_discovery:degraded", "producer": "model_discovery",
        "type": "model_discovery_degraded", "severity": "warn", "scope": "pod",
        "category": "hygiene", "title": "degraded", "body": "x", "details": {},
    })

    rows = mda.list_adoptable_discoveries(shared)
    assert [r["qualified_id"] for r in rows] == [
        "anthropic/claude-mythos-5", "openai/o5-pro",
    ]
    r0 = rows[0]
    assert r0["provider"] == "anthropic"
    assert r0["model_id"] == "claude-mythos-5"
    assert r0["evidence"]["context_window"] == 200000
    assert "signal_id" in r0
    # fits_existing rows carry the routing role the card pre-selects + the
    # applier maps (never a slug surfaced to the operator).
    assert r0["role"] == "standard"


def test_only_fits_existing_is_adopt_surfaced(shared):
    # fits_existing → adopt list; new_tier → separate list; the off-ladder
    # verdicts are suppressed entirely (unroutable, so adopting is busywork).
    signals_store.observe(shared, **_discovery_spec(
        "anthropic", "claude-mythos-5", verdict="fits_existing", role="standard"))
    signals_store.observe(shared, **_discovery_spec(
        "anthropic", "claude-frontier-6", verdict="new_tier", cost_class="premium"))
    signals_store.observe(shared, **_discovery_spec(
        "openai", "o5-thinking", verdict="mode_variant"))
    signals_store.observe(shared, **_discovery_spec(
        "openai", "o5-code", verdict="specialist"))
    signals_store.observe(shared, **_discovery_spec(
        "xai", "grok-mystery", verdict="cannot_place"))

    adopt = mda.list_adoptable_discoveries(shared)
    new_tiers = mda.list_new_tier_discoveries(shared)
    assert [r["qualified_id"] for r in adopt] == ["anthropic/claude-mythos-5"]
    assert [r["qualified_id"] for r in new_tiers] == ["anthropic/claude-frontier-6"]
    # mode_variant / specialist / cannot_place appear in NEITHER list.
    surfaced = {r["qualified_id"] for r in adopt} | {r["qualified_id"] for r in new_tiers}
    assert "openai/o5-thinking" not in surfaced
    assert "openai/o5-code" not in surfaced
    assert "xai/grok-mystery" not in surfaced


def test_best_per_rung_collapses_same_role_same_provider(shared):
    # The screenshot case: three gemini *-flash-lite models all → fast. Only the
    # newest generation survives the adopt list.
    for mid in ("gemini-1.5-flash-lite", "gemini-2.0-flash-lite",
                "gemini-2.5-flash-lite"):
        signals_store.observe(shared, **_discovery_spec(
            "google", mid, verdict="fits_existing", role="fast"))

    adopt = mda.list_adoptable_discoveries(shared)
    assert [r["model_id"] for r in adopt] == ["gemini-2.5-flash-lite"]


def test_best_per_rung_keeps_distinct_roles_and_providers(shared):
    # Different (provider, role) groups are all kept — only same-group duplicates
    # collapse.
    signals_store.observe(shared, **_discovery_spec(
        "google", "gemini-2.5-flash-lite", verdict="fits_existing", role="fast"))
    signals_store.observe(shared, **_discovery_spec(
        "google", "gemini-2.5-pro", verdict="fits_existing", role="power"))
    signals_store.observe(shared, **_discovery_spec(
        "anthropic", "claude-haiku-9", verdict="fits_existing", role="fast"))

    adopt = mda.list_adoptable_discoveries(shared)
    assert {r["qualified_id"] for r in adopt} == {
        "google/gemini-2.5-flash-lite",
        "google/gemini-2.5-pro",
        "anthropic/claude-haiku-9",
    }


def test_picker_roles_includes_judge_only_when_pod_defines_it():
    assert mda.picker_roles({"models": {"roles": {"standard": "sonnet-class"}}}) == [
        "fast", "standard", "power", "max",
    ]
    with_judge = mda.picker_roles(
        {"models": {"roles": {"standard": "sonnet-class", "judge": "haiku-class"}}})
    assert with_judge == ["fast", "standard", "power", "max", "judge"]


# ── adopt_discovery ───────────────────────────────────────────────────────────

def test_adopt_discovery_dormant_drives_applier_and_resolves_signal(shared, net_state):
    signals_store.observe(
        shared, **_discovery_spec("anthropic", "claude-mythos-5",
                                  rung_slug="mythos-class", cost_class="premium"))
    assert len(_firing(shared)) == 1

    status, body = mda.adopt_discovery(
        shared, net_state["net"], provider="anthropic",
        model_id="claude-mythos-5", role="none",
    )
    assert status == 200 and body["ok"], body
    # The applier created the rung in the (injected) network.
    rungs = net_state["net"]["models"]["rungs"]
    assert any(r["id"] == "mythos-class"
               and "anthropic/claude-mythos-5" in r["models"] for r in rungs)
    # Dormant: no role re-point.
    assert "roles" not in net_state["net"]["models"] or not net_state["net"]["models"]["roles"]
    # The Signal resolved immediately so the card + nav badge clear.
    assert _firing(shared) == []


def test_adopt_discovery_maps_role_and_seeds_cap(shared, net_state):
    signals_store.observe(
        shared, **_discovery_spec("anthropic", "claude-mythos-5",
                                  rung_slug="mythos-class", cost_class="premium"))
    status, body = mda.adopt_discovery(
        shared, net_state["net"], provider="anthropic",
        model_id="claude-mythos-5", role="max", cap=7,
    )
    assert status == 200 and body["ok"], body
    models = net_state["net"]["models"]
    assert models["roles"]["max"] == "mythos-class"
    assert models["roleCaps"]["max"]["maxPerDayPerBot"] == 7


def test_adopt_discovery_rejects_bad_role(shared, net_state):
    signals_store.observe(shared, **_discovery_spec("anthropic", "claude-mythos-5"))
    status, body = mda.adopt_discovery(
        shared, net_state["net"], provider="anthropic",
        model_id="claude-mythos-5", role="overlord",
    )
    assert status == 400 and not body["ok"]
    assert "invalid role" in body["error"]
    # Rejected before any write — signal stays firing.
    assert len(_firing(shared)) == 1


def test_adopt_discovery_unknown_model_is_404(shared, net_state):
    status, body = mda.adopt_discovery(
        shared, net_state["net"], provider="anthropic", model_id="ghost-9",
    )
    assert status == 404 and not body["ok"]


# ── ignore_discovery ──────────────────────────────────────────────────────────

def test_ignore_discovery_writes_list_and_dismisses_signal(shared):
    signals_store.observe(shared, **_discovery_spec("openai", "o9-experimental"))
    status, body = mda.ignore_discovery(
        shared, provider="openai", model_id="o9-experimental")
    assert status == 200 and body["ok"]

    ignore_path = shared / "model-freshness" / "discovery-ignore.json"
    data = json.loads(ignore_path.read_text())
    assert "openai/o9-experimental" in data["ignore"]
    # Signal dismissed → no longer firing.
    assert _firing(shared) == []

    # The generator's own ignore-list reader sees it (round-trip).
    import model_discovery as md
    assert "o9-experimental" in md.load_ignore_list(shared)


# ── adopt_all_dormant ─────────────────────────────────────────────────────────

def test_adopt_all_dormant_adopts_every_firing_discovery(shared, net_state):
    signals_store.observe(
        shared, **_discovery_spec("anthropic", "claude-mythos-5",
                                  rung_slug="mythos-class", cost_class="premium"))
    signals_store.observe(
        shared, **_discovery_spec("openai", "o5-pro",
                                  rung_slug="o5-class", cost_class="high"))
    status, body = mda.adopt_all_dormant(shared, net_state["net"])
    assert status == 200 and body["ok"], body
    assert body["adopted"] == 2 and body["failed"] == 0
    rung_ids = {r["id"] for r in net_state["net"]["models"]["rungs"]}
    assert {"mythos-class", "o5-class"} <= rung_ids
    assert _firing(shared) == []


def test_adopt_all_dormant_empty_is_400(shared, net_state):
    status, body = mda.adopt_all_dormant(shared, net_state["net"])
    assert status == 400 and not body["ok"]


# ── routes (HTTP, end-to-end) ─────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, shared, net_state):
    from evolve_admin.web.server import create_app

    network = {"bots": {}, "members": [], "sharedDir": str(shared)}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    app = create_app(network_path)
    app.config["TESTING"] = True
    return app.test_client()


def test_route_discoveries_then_adopt(client, shared, net_state):
    signals_store.observe(
        shared, **_discovery_spec("anthropic", "claude-mythos-5",
                                  rung_slug="mythos-class", cost_class="premium"))

    r = client.get("/api/models/discoveries")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["count"] == 1
    assert payload["discoveries"][0]["qualified_id"] == "anthropic/claude-mythos-5"

    r = client.post("/api/models/adopt-discovery", json={
        "provider": "anthropic", "model_id": "claude-mythos-5", "role": "none",
    })
    assert r.status_code == 200
    assert r.get_json()["ok"]
    assert any(rg["id"] == "mythos-class" for rg in net_state["net"]["models"]["rungs"])
    # Discoveries list now empty (signal resolved).
    assert client.get("/api/models/discoveries").get_json()["count"] == 0


def test_route_ignore_discovery(client, shared):
    signals_store.observe(shared, **_discovery_spec("openai", "o9-experimental"))
    r = client.post("/api/models/ignore-discovery", json={
        "provider": "openai", "model_id": "o9-experimental",
    })
    assert r.status_code == 200 and r.get_json()["ok"]
    assert client.get("/api/models/discoveries").get_json()["count"] == 0


# ── Version upgrades — the PRIMARY surface (spec §Addendum 15) ─────────────────

import model_discovery as md  # noqa: E402


def _write_listings(shared, models_by_provider):
    """Persist a listings cache the upgrade pass reads off."""
    enums = [
        md.ProviderEnumeration(
            provider=p, ok=True,
            models=[md.ListedModel(provider=p, model_id=m, qualified_id=f"{p}/{m}")
                    for m in mids],
        )
        for p, mids in models_by_provider.items()
    ]
    md.write_listings_cache(Path(shared), enums, refreshed_at="2026-06-30T00:00:00Z")


def _net_with_rung(rung_slug, models, *, role="standard", cost="medium"):
    return {"bots": {}, "models": {
        "rungs": [{"id": rung_slug, "models": models, "costClass": cost}],
        "roles": {role: rung_slug},
    }}


def test_list_version_upgrades_surfaces_newer_class(shared):
    net = _net_with_rung("sonnet-class", ["anthropic/claude-sonnet-4-5"])
    _write_listings(shared, {"anthropic": ["claude-sonnet-4-5", "claude-sonnet-5"]})
    rows = mda.list_version_upgrades(shared, net)
    assert len(rows) == 1
    r = rows[0]
    assert r["current_model"] == "anthropic/claude-sonnet-4-5"
    assert r["latest_model"] == "anthropic/claude-sonnet-5"
    assert r["latest_model_id"] == "claude-sonnet-5"
    assert r["rung_slug"] == "sonnet-class"
    assert r["role_label"] == "Standard"


def test_list_version_upgrades_empty_without_cache(shared):
    net = _net_with_rung("sonnet-class", ["anthropic/claude-sonnet-4-5"])
    assert mda.list_version_upgrades(shared, net) == []


def test_apply_upgrade_extends_rung_no_role_change(shared, net_state):
    net = _net_with_rung("sonnet-class", ["anthropic/claude-sonnet-4-5"])
    net_state["net"] = net
    _write_listings(shared, {"anthropic": ["claude-sonnet-4-5", "claude-sonnet-5"]})
    status, body = mda.apply_upgrade(
        shared, net, provider="anthropic", latest_model_id="claude-sonnet-5")
    assert status == 200, body
    assert body["ok"]
    rung = net_state["net"]["models"]["rungs"][0]
    # The newer version must land AHEAD of its predecessor: the resolver routes
    # to the FIRST credentialed cluster member, not the newest, so a plain
    # append would leave routing on the predecessor (silent no-op). Predecessor
    # is demoted to the immediate fallback slot.
    assert rung["models"] == ["anthropic/claude-sonnet-5", "anthropic/claude-sonnet-4-5"]
    # role still points at the rung — no re-point needed now that the new
    # version leads the cluster.
    assert net_state["net"]["models"]["roles"]["standard"] == "sonnet-class"
    # The upgrade actually moves routing (the whole point of Addendum 15).
    import primary_bot as pb  # noqa: E402
    resolved = pb.resolve_role_with_availability(
        net_state["net"]["models"], "standard", {"anthropic"})
    assert resolved["model"] == "anthropic/claude-sonnet-5"


def test_apply_upgrade_preserves_other_provider_primary(shared, net_state):
    """In a multi-provider rung where a DIFFERENT provider is primary, the
    upgrade splices the newer version ahead of only its own predecessor — the
    primary provider's routing is untouched, the same-provider fallback rides
    the new version."""
    net = _net_with_rung(
        "sonnet-class", ["openai/gpt-5", "anthropic/claude-sonnet-4-5"])
    net_state["net"] = net
    _write_listings(shared, {
        "anthropic": ["claude-sonnet-4-5", "claude-sonnet-5"],
        "openai": ["gpt-5"],
    })
    status, body = mda.apply_upgrade(
        shared, net, provider="anthropic", latest_model_id="claude-sonnet-5")
    assert status == 200, body
    rung = net_state["net"]["models"]["rungs"][0]
    assert rung["models"] == [
        "openai/gpt-5", "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-4-5",
    ]


def test_apply_upgrade_idempotent(shared, net_state):
    """Re-applying the same upgrade after it already led the cluster is a
    no-op: once the pod runs the newest member as primary, no upgrade surfaces."""
    net = _net_with_rung("sonnet-class", ["anthropic/claude-sonnet-4-5"])
    net_state["net"] = net
    _write_listings(shared, {"anthropic": ["claude-sonnet-4-5", "claude-sonnet-5"]})
    mda.apply_upgrade(
        shared, net_state["net"], provider="anthropic", latest_model_id="claude-sonnet-5")
    first = list(net_state["net"]["models"]["rungs"][0]["models"])
    status, body = mda.apply_upgrade(
        shared, net_state["net"], provider="anthropic", latest_model_id="claude-sonnet-5")
    assert status == 404  # no remaining upgrade to apply
    assert net_state["net"]["models"]["rungs"][0]["models"] == first


def test_apply_upgrade_unknown_target_is_404(shared, net_state):
    net = _net_with_rung("sonnet-class", ["anthropic/claude-sonnet-4-5"])
    _write_listings(shared, {"anthropic": ["claude-sonnet-4-5", "claude-sonnet-5"]})
    status, body = mda.apply_upgrade(
        shared, net, provider="anthropic", latest_model_id="claude-sonnet-99")
    assert status == 404
    assert not body["ok"]


def test_apply_all_upgrades_applies_each(shared, net_state):
    net = {"bots": {}, "models": {
        "rungs": [
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-5"], "costClass": "medium"},
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-7"], "costClass": "high"},
        ],
        "roles": {"standard": "sonnet-class", "power": "opus-class"},
    }}
    net_state["net"] = net
    _write_listings(shared, {"anthropic": [
        "claude-sonnet-4-5", "claude-sonnet-5",
        "claude-opus-4-7", "claude-opus-4-8",
    ]})
    status, body = mda.apply_all_upgrades(shared, net)
    assert status == 200, body
    assert body["applied"] == 2 and body["failed"] == 0
    rungs = {r["id"]: r["models"] for r in net_state["net"]["models"]["rungs"]}
    assert "anthropic/claude-sonnet-5" in rungs["sonnet-class"]
    assert "anthropic/claude-opus-4-8" in rungs["opus-class"]
