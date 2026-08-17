"""tests/test_admin_config_routing_put.py — ``PUT /api/admin/config/<bot>/routing``
shape validation + partial-merge semantics (#3566 audit E-3).

Before this file's fix the endpoint did NO validation and the writer did a
bare ``tiers_file["routing"] = updates["routing"]``. Two distinct defects:

  1. WHOLESALE REPLACE — a partial write (any body that doesn't mention
     ``enabled``) silently cleared ``routing.enabled=false``. That flag is the
     OC-2026.7 kill-switch for tier routing, and every consumer defaults
     absent-to-enabled (``DEFAULT_ROUTING["enabled"] = True``,
     ``config.routing?.enabled !== false`` in the plugin's ModelRouter), so
     dropping it silently RE-ENABLES routing.

  2. NON-DICT POISON — a str / list / int / bool body was persisted BEFORE the
     post-write read-back. ``json_full_config_set`` ends with
     ``return json_full_config(bot, path)`` → ``get_routing`` →
     ``dict(DEFAULT_ROUTING).update(stored)`` → ValueError/TypeError forever
     after. Self-concealing: the repairing PUT lands but still 500s, because
     the throw happens in the read that follows the (successful) save.

Locked here:
  - every non-dict body is a 400 and leaves evolve-tiers.json byte-identical
  - a bad-typed / unknown key is a 400 and leaves the file byte-identical
  - a partial PUT merges — it cannot clear ``enabled=false``
  - the merge is per-SLOT: ``<x>Tier`` and ``<x>Role`` are one slot, so writing
    one evicts the other (the plugin resolves ``maintenanceRole ??
    maintenanceTier``, so a key-wise merge would let a stale role shadow the
    operator's edit forever); sending both halves at once is a 400
  - the READ side projects ``<x>Role`` onto ``<x>Tier`` so the tier view every
    python consumer speaks is TRUE for a migrated bot — which is what makes
    eviction a faithful round-trip, and what keeps a read-modify-write client
    (the arbiter's tier_adjustment applier) from posting both halves
  - a valid full write still round-trips (the SPA's routing card path)
  - ``get_routing`` does not raise on a file already poisoned by the old code,
    and the next valid write heals the block

The endpoint tests drive the REAL ``oc_model.json_full_config_set`` against a
temp bot HOME (the oc_cli seam is the subprocess boundary in production), so
"the file is untouched" is asserted on actual bytes on disk.
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


# Every body shape that used to be persisted verbatim and then wedge
# get_routing on the read-back.
NON_DICT_BODIES = [
    "tier3",
    ["tier3"],
    42,
    True,
    3.5,
]


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app whose config-set seam runs the real oc_model writer against a
    temp bot HOME. ``tiers_path`` is the on-disk evolve-tiers.json."""
    from evolve_admin.web.server import create_app
    import oc_cli
    import oc_model

    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({
        "agents": {"defaults": {"model": {
            "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
        }}},
    }))
    monkeypatch.setenv("HOME", str(home))

    network = {"bots": {"evolve": {"user": "evolve"}}, "sharedDir": str(tmp_path / "shared")}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    def real_get(bot_id, network_path=None):
        return oc_model.json_full_config(bot_id, oc_json)

    def real_set_err(bot_id, updates, network_path=None):
        try:
            return oc_model.json_full_config_set(bot_id, updates, oc_json_path=oc_json), None
        except (ValueError, TypeError) as exc:
            return None, str(exc)

    monkeypatch.setattr(oc_cli, "oc_full_config_get", real_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", real_set_err)
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {
            "keys": {"anthropic": {"api_key": True}}, "source": "sqlite",
        },
    )

    flask_app = create_app(network_path)
    flask_app.config["TESTING"] = True
    return {
        "app": flask_app,
        "tiers_path": home / ".openclaw" / "evolve-tiers.json",
        "oc_json": oc_json,
    }


def _seed(app, routing):
    """Write a starting routing block straight to disk (bypasses the endpoint)."""
    app["tiers_path"].write_text(json.dumps({"routing": routing}, indent=2))


# ── Defect 1: the kill-switch must survive a partial write ───────────────────


def test_partial_put_cannot_clear_enabled_false(app):
    """The reproduction: enabled=false on disk, a PUT that never mentions
    ``enabled``, and the flag must still be false afterwards."""
    _seed(app, {"enabled": False, "maintenanceTier": "tier3"})
    with app["app"].test_client() as c:
        resp = c.put(
            "/api/admin/config/evolve/routing",
            json={"routing": {"maintenanceTier": "tier2"}},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["routing"]["enabled"] is False

    stored = json.loads(app["tiers_path"].read_text())["routing"]
    assert stored["enabled"] is False, "partial write cleared the kill-switch"
    assert stored["maintenanceTier"] == "tier2", "the intended edit still landed"


def test_tier_write_evicts_its_role_sibling(app):
    """``<slot>Tier`` and ``<slot>Role`` are ONE logical slot. The plugin's
    ``_normalizeRouting`` resolves ``toRole(r.maintenanceRole ?? r.maintenanceTier)``
    — the Role key WINS — so a key-wise merge that kept both would let a stale
    role written by ``migrate-model-roles`` permanently shadow the operator's
    routing-card edit, with no UI path to repair it (the card only ever posts
    ``*Tier``). Writing one half of a slot must delete the other.

    Eviction is only faithful because the READ side projects: the card renders
    ``get_routing()``, which now reports the stored role AS its tier
    (``power`` → ``tier1``), so a body that says ``tier3`` is a deliberate
    operator change rather than a default they never saw. See
    ``test_get_routing_projects_role_onto_tier``."""
    _seed(app, {"enabled": False, "maintenanceRole": "power", "backgroundRole": "power"})
    with app["app"].test_client() as c:
        resp = c.put(
            "/api/admin/config/evolve/routing",
            json={"routing": {"maintenanceTier": "tier3", "backgroundTier": "tier3"}},
        )
        assert resp.status_code == 200, resp.get_json()

    stored = json.loads(app["tiers_path"].read_text())["routing"]
    assert "maintenanceRole" not in stored, "stale role would shadow the edit"
    assert "backgroundRole" not in stored
    assert stored["maintenanceTier"] == "tier3"
    # …and the unrelated key in the block still merged through.
    assert stored["enabled"] is False


def test_role_write_evicts_its_tier_sibling(app):
    """The reverse direction — a role-shaped writer (migrate-model-roles
    speaks this generation) must not leave a stale tier behind either."""
    _seed(app, {"enabled": True, "maintenanceTier": "tier3"})
    with app["app"].test_client() as c:
        resp = c.put(
            "/api/admin/config/evolve/routing",
            json={"routing": {"maintenanceRole": "power"}},
        )
        assert resp.status_code == 200, resp.get_json()
    stored = json.loads(app["tiers_path"].read_text())["routing"]
    assert stored["maintenanceRole"] == "power"
    assert "maintenanceTier" not in stored


def test_both_halves_of_one_slot_is_400(app):
    """Ambiguous by construction — the plugin would honor the Role and drop
    the Tier on the floor. Refuse rather than silently pick."""
    _seed(app, {"enabled": False})
    before = app["tiers_path"].read_text()
    with app["app"].test_client() as c:
        resp = c.put(
            "/api/admin/config/evolve/routing",
            json={"routing": {"maintenanceTier": "tier3", "maintenanceRole": "power"}},
        )
        assert resp.status_code == 400, resp.get_json()
        assert "same slot" in resp.get_json()["error"]
    assert app["tiers_path"].read_text() == before


def test_explicit_enabled_true_still_turns_routing_back_on(app):
    """Merge is not a one-way ratchet — an operator who ticks the box back on
    (an explicit ``enabled: true``) still gets routing enabled."""
    _seed(app, {"enabled": False})
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/routing", json={"routing": {"enabled": True}})
        assert resp.status_code == 200, resp.get_json()
    assert json.loads(app["tiers_path"].read_text())["routing"]["enabled"] is True


# ── Defect 2: a non-dict body is a 400 and never reaches the file ────────────


@pytest.mark.parametrize("bad", NON_DICT_BODIES)
def test_non_dict_body_is_400_and_leaves_file_untouched(app, bad):
    _seed(app, {"enabled": False, "maintenanceTier": "tier3"})
    before = app["tiers_path"].read_text()
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/routing", json={"routing": bad})
        assert resp.status_code == 400, resp.get_json()
        assert "error" in resp.get_json()
    assert app["tiers_path"].read_text() == before, "a rejected body still hit the file"


@pytest.mark.parametrize("bad", NON_DICT_BODIES)
def test_bad_write_leaves_the_read_endpoint_working(app, bad):
    """Belt: the rejected write must not have poisoned anything, so the GET
    still returns the operator's stored block. (The wedge itself — a file that
    is ALREADY poisoned — is covered end-to-end by
    ``test_poisoned_file_is_readable_and_healed_through_the_endpoints``.)"""
    _seed(app, {"enabled": False})
    with app["app"].test_client() as c:
        c.put("/api/admin/config/evolve/routing", json={"routing": bad})
        resp = c.get("/api/admin/config/evolve")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["routing"]["enabled"] is False


@pytest.mark.parametrize("bad", NON_DICT_BODIES)
def test_poisoned_file_is_readable_and_healed_through_the_endpoints(app, bad):
    """The self-concealing half, end-to-end: a file poisoned by the OLD code
    used to 500 every read AND every repairing write (the throw is in the
    post-save read-back). After the fix the GET works, and the next valid PUT
    replaces the poison. Deleting the get_routing heal fails this test."""
    app["tiers_path"].write_text(json.dumps({"routing": bad}))
    with app["app"].test_client() as c:
        resp = c.get("/api/admin/config/evolve")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["routing"]["enabled"] is True  # code defaults

        resp = c.put("/api/admin/config/evolve/routing", json={"routing": {"enabled": False}})
        assert resp.status_code == 200, resp.get_json()
    assert json.loads(app["tiers_path"].read_text())["routing"] == {"enabled": False}


@pytest.mark.parametrize("body", [
    {"routing": {"enabled": "yes"}},
    {"routing": {"confidenceThreshold": "high"}},
    {"routing": {"confidenceThreshold": 5}},
    {"routing": {"confidenceThreshold": True}},
    {"routing": {"maintenanceTier": 3}},
    {"routing": {"classifierDowngrade": "true"}},
    {"routing": {"nonsense": 1}},
])
def test_bad_typed_or_unknown_key_is_400_and_leaves_file_untouched(app, body):
    _seed(app, {"enabled": False, "maintenanceTier": "tier3"})
    before = app["tiers_path"].read_text()
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/routing", json=body)
        assert resp.status_code == 400, resp.get_json()
    assert app["tiers_path"].read_text() == before


def test_missing_routing_key_still_400(app):
    with app["app"].test_client() as c:
        assert c.put("/api/admin/config/evolve/routing", json={}).status_code == 400


# ── The real caller: the SPA's routing card sends the full block ────────────


def test_full_spa_block_round_trips(app):
    """Exactly what ``_aiSaveRouting()`` posts — all six keys, every save."""
    payload = {
        "enabled": True,
        "maintenanceTier": "tier3",
        "backgroundTier": "tier3",
        "ambiguousTier": None,
        "confidenceThreshold": 0.65,
        "classifierDowngrade": False,
    }
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/routing", json={"routing": payload})
        assert resp.status_code == 200, resp.get_json()
        for k, v in payload.items():
            assert resp.get_json()["routing"][k] == v
    stored = json.loads(app["tiers_path"].read_text())["routing"]
    for k, v in payload.items():
        assert stored[k] == v


def test_routing_write_does_not_touch_openclaw_json(app):
    """Regression guard: routing lives only in evolve-tiers.json."""
    before = app["oc_json"].read_text()
    with app["app"].test_client() as c:
        assert c.put(
            "/api/admin/config/evolve/routing",
            json={"routing": {"enabled": False}},
        ).status_code == 200
    assert app["oc_json"].read_text() == before


# ── Writer-layer safety net + already-poisoned-file heal ────────────────────


@pytest.mark.parametrize("bad", NON_DICT_BODIES)
def test_get_routing_survives_an_already_poisoned_file(tmp_path, monkeypatch, bad):
    """A pod that already took a bad write under the old code: get_routing
    must return usable defaults instead of raising forever."""
    import oc_model

    home = tmp_path / "poisoned"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "evolve-tiers.json").write_text(
        json.dumps({"routing": bad, "fallbackMode": "static"})
    )
    monkeypatch.setenv("HOME", str(home))

    routing = oc_model.get_routing("evolve")
    assert routing["enabled"] is True
    assert routing["maintenanceTier"] == "tier3"


@pytest.mark.parametrize("bad", NON_DICT_BODIES)
def test_valid_write_heals_a_poisoned_block(tmp_path, monkeypatch, bad):
    """The repairing PUT: a poisoned block is replaced by a clean dict, and
    the write no longer 500s on the read-back."""
    import oc_model

    home = tmp_path / "poisoned"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"routing": bad}))
    monkeypatch.setenv("HOME", str(home))

    result = oc_model.json_full_config_set(
        "evolve", {"routing": {"enabled": False}}, oc_json_path=oc_json
    )
    assert result["routing"]["enabled"] is False
    assert json.loads(tiers_path.read_text())["routing"] == {"enabled": False}


@pytest.mark.parametrize("bad", NON_DICT_BODIES)
def test_writer_never_persists_a_non_dict_routing(tmp_path, monkeypatch, bad):
    """Defense in depth for non-endpoint callers (the arbiter's
    tier_adjustment applier writes through the same seam)."""
    import oc_model

    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"routing": {"enabled": False}}))
    monkeypatch.setenv("HOME", str(home))

    oc_model.json_full_config_set("evolve", {"routing": bad}, oc_json_path=oc_json)
    assert json.loads(tiers_path.read_text())["routing"] == {"enabled": False}


def test_validate_routing_update_accepts_the_full_default_block():
    """The shipped defaults must pass their own validator."""
    import oc_model

    assert oc_model.validate_routing_update(dict(oc_model.DEFAULT_ROUTING)) is None
    assert oc_model.validate_routing_update({}) is None
    assert oc_model.validate_routing_update(
        {"maintenanceRole": "fast", "backgroundRole": "fast", "ambiguousRole": None}
    ) is None


def test_empty_update_does_not_mint_a_routing_key(tmp_path, monkeypatch):
    """A no-op update must not create ``routing: {}`` on a file that never had
    one — that dirties the file and burns a privileged write for nothing."""
    import oc_model

    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"fallbackMode": "static"}))
    monkeypatch.setenv("HOME", str(home))

    oc_model.json_full_config_set("evolve", {"routing": {}}, oc_json_path=oc_json)
    assert "routing" not in json.loads(tiers_path.read_text())


def test_writer_logs_the_keys_it_refused(tmp_path, monkeypatch, capsys):
    """A silent drop is how a non-endpoint caller ends up believing a write
    landed. The tiers fold path prints for the same case; so must this one."""
    import oc_model

    home = tmp_path / "bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    monkeypatch.setenv("HOME", str(home))

    oc_model.json_full_config_set(
        "evolve", {"routing": {"enabled": "yes", "nonsense": 1}}, oc_json_path=oc_json
    )
    err = capsys.readouterr().err
    assert "enabled" in err and "nonsense" in err


def test_validate_rejects_a_non_tier_shaped_tier_value():
    """The endpoint is the trust boundary — the SPA's <select> is not."""
    import oc_model

    assert oc_model.validate_routing_update({"maintenanceTier": "banana"}) is not None
    assert oc_model.validate_routing_update({"maintenanceTier": "tier3"}) is None
    # Roles stay free-form: the role set is per-bot configurable.
    assert oc_model.validate_routing_update({"maintenanceRole": "power"}) is None


# ── Read-path projection: the tier view must tell the truth ─────────────────


def test_get_routing_projects_role_onto_tier(tmp_path, monkeypatch):
    """A migrated (role-shaped) bot used to render the DEFAULT tier in the
    card while actually routing to its stored role — the card showed a value
    the operator never chose, and saving wrote that fiction back."""
    import oc_model

    home = tmp_path / "migrated"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "evolve-tiers.json").write_text(json.dumps({
        "routing": {"enabled": True, "maintenanceRole": "power", "backgroundRole": "fast"},
    }))
    monkeypatch.setenv("HOME", str(home))

    view = oc_model.get_routing("evolve")
    assert view["maintenanceTier"] == "tier1", "power must project to tier1"
    assert view["backgroundTier"] == "tier3"
    # One generation out, one generation in — no client can post both halves.
    assert not [k for k in view if k.endswith("Role")]
    assert oc_model.validate_routing_update(view) is None


def test_unprojectable_role_suppresses_the_default_tier(tmp_path, monkeypatch):
    """``max`` has no legacy tier. Keep the role visible AND drop the defaulted
    ``maintenanceTier`` beside it — a view naming both halves of one slot is a
    document the endpoint's own PUT would 400, and it suppresses the writer's
    slot eviction (silently no-opping a read-modify-write caller)."""
    import oc_model

    home = tmp_path / "maxbot"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "evolve-tiers.json").write_text(
        json.dumps({"routing": {"maintenanceRole": "max"}})
    )
    monkeypatch.setenv("HOME", str(home))
    view = oc_model.get_routing("evolve")
    assert view["maintenanceRole"] == "max"
    assert "maintenanceTier" not in view
    # The view the API hands out for a CANONICAL role is always PUT-able. (A
    # hand-edited non-canonical role stays visible in the view and its
    # round-trip is a 400 that names it — the file genuinely holds a value the
    # API will not accept, and saying so beats silently normalizing it.)
    assert oc_model.validate_routing_update(view) is None


def test_unprojectable_role_is_replaced_by_a_card_save(app):
    """…and the operator can still change it: a card Save posts the tier,
    which evicts the role instead of being shadowed by it."""
    _seed(app, {"enabled": True, "maintenanceRole": "max"})
    with app["app"].test_client() as c:
        routing = dict(c.get("/api/admin/config/evolve").get_json()["routing"])
        assert routing["maintenanceRole"] == "max"
        assert "maintenanceTier" not in routing
        routing.pop("maintenanceRole")
        routing["maintenanceTier"] = "tier3"   # what the card posts
        assert c.put(
            "/api/admin/config/evolve/routing", json={"routing": routing},
        ).status_code == 200
    stored = json.loads(app["tiers_path"].read_text())["routing"]
    assert "maintenanceRole" not in stored
    assert stored["maintenanceTier"] == "tier3"


def test_free_form_role_is_rejected_at_the_boundary(app):
    """A role the projection cannot invert must never reach the file — it
    would read back as a two-generation view."""
    _seed(app, {"enabled": True})
    before = app["tiers_path"].read_text()
    with app["app"].test_client() as c:
        resp = c.put(
            "/api/admin/config/evolve/routing",
            json={"routing": {"maintenanceRole": "turbo"}},
        )
        assert resp.status_code == 400, resp.get_json()
    assert app["tiers_path"].read_text() == before


def test_read_modify_write_of_the_api_view_lands_on_a_migrated_bot(app):
    """End-to-end for the arbiter's tier_adjustment applier shape: GET the
    config, change ONE field, PUT the whole block back. On a role-shaped bot
    this must actually change what the plugin resolves — under a key-wise
    merge the stale role would shadow it forever."""
    _seed(app, {"enabled": True, "maintenanceRole": "power"})
    with app["app"].test_client() as c:
        routing = dict(c.get("/api/admin/config/evolve").get_json()["routing"])
        assert routing["maintenanceTier"] == "tier1"
        routing["maintenanceTier"] = "tier2"
        resp = c.put("/api/admin/config/evolve/routing", json={"routing": routing})
        assert resp.status_code == 200, resp.get_json()

    stored = json.loads(app["tiers_path"].read_text())["routing"]
    # maintenanceRole ?? maintenanceTier — the plugin's resolution order.
    assert stored.get("maintenanceRole") is None
    assert stored["maintenanceTier"] == "tier2"


def test_applier_shaped_rmw_lands_on_an_unprojectable_role(tmp_path, monkeypatch):
    """The arbiter's tier_adjustment applier reads the projected view and SETS
    ``<x>Tier`` on it — so when the view carried an unprojectable ``<x>Role``
    (``max``) its body names both halves of the slot. Without the writer's
    both-halves rule the eviction is suppressed, the stale role keeps winning
    ``Role ?? Tier`` in the plugin, and the applier reports ok=True on a write
    that changed nothing. Driven through the applier's own helpers, not a
    hand-built body."""
    import oc_model
    from arbiter.appliers import tier_adjustment as ta

    home = tmp_path / "maxbot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    tiers_path = home / ".openclaw" / "evolve-tiers.json"
    tiers_path.write_text(json.dumps({"routing": {"enabled": True, "maintenanceRole": "max"}}))
    monkeypatch.setenv("HOME", str(home))

    ta.set_config_io(
        get_fn=lambda b: oc_model.json_full_config(b, oc_json),
        set_fn=lambda b, u: oc_model.json_full_config_set(b, u, oc_json_path=oc_json),
    )
    try:
        routing = ta._read_routing("evolve")
        routing["maintenanceTier"] = "tier1"
        ta._write_routing("evolve", routing)
    finally:
        ta.set_config_io(get_fn=None, set_fn=None)

    stored = json.loads(tiers_path.read_text())["routing"]
    assert stored.get("maintenanceRole") is None, "stale role would win Role ?? Tier"
    assert stored["maintenanceTier"] == "tier1"
    assert stored["enabled"] is True
