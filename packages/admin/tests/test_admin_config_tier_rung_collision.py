"""tests/test_admin_config_tier_rung_collision.py — rung-collision clobber guard.

The same false-success bug fixed for the Model Freshness endpoints
(test_model_freshness_endpoints.py) also lived in
``PUT /api/admin/config/<bot_id>/tiers`` — and unlike the freshness path it had
NO post-write verification, so it returned success while the operator's edit
vanished.

Root cause: on the new (rungs/roles) tier shape several legacy tier keys collapse
onto one rung — on the live ``evolve`` bot ``standard``(tier2) and ``judge``(tier0)
BOTH map to ``sonnet-class``. ``oc_model.apply_tiers_update_new_shape`` folds a
legacy ``{tierN: {models}}`` update into the rung store last-writer-wins, so an
unchanged sibling tier sent alongside a changed one clobbers it.

Two layers, both pinned here:
  1. The WRITER (``oc_model.json_full_config_set``) refuses a tiers update that
     names two legacy tiers folding to one rung with DIFFERENT models, raising a
     ``RUNG_COLLISION``-prefixed error (the per-bot editor's block-on-conflict
     contract).
  2. The ENDPOINT maps that to a 409, and verifies post-write that every model
     the operator asked for actually landed in the synthesized tiers — a 500 if
     a silent non-persist slips through.
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


# A new-shape file where standard + judge share the sonnet-class rung — the
# exact collision geometry of the live ``evolve`` bot.
def _evolve_shape_file() -> dict:
    return {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
        ],
        "roles": {
            "fast": "haiku-class",
            "standard": "sonnet-class",
            "power": "opus-class",
            "judge": {"rung": "sonnet-class", "provider": "not-standard"},
        },
    }


# ── unit: detect_rung_collisions (pure) ──────────────────────────────────────


def test_detect_no_collision_on_legacy_only_file():
    import oc_model
    # No rungs → legacy-only file → nothing shares a rung.
    legacy = {"tiers": {"tier2": {"models": ["a"]}}}
    assert oc_model.detect_rung_collisions(
        legacy, {"tier2": {"models": ["a"]}, "tier0": {"models": ["b"]}}
    ) == []


def test_detect_no_collision_for_single_tier():
    import oc_model
    assert oc_model.detect_rung_collisions(
        _evolve_shape_file(), {"tier2": {"models": ["anthropic/claude-opus-4-8"]}}
    ) == []


def test_detect_no_collision_when_shared_rung_models_match():
    import oc_model
    # standard + judge both → sonnet-class with the SAME models is not a
    # conflict — it is exactly the editor's load state and a catalog re-save.
    same = ["anthropic/claude-sonnet-4-6"]
    assert oc_model.detect_rung_collisions(
        _evolve_shape_file(),
        {"tier2": {"models": same}, "tier0": {"models": list(same)}},
    ) == []


def test_detect_no_collision_when_only_order_differs():
    import oc_model
    # Order/dupes are not a conflict — the model SET is identical.
    assert oc_model.detect_rung_collisions(
        _evolve_shape_file(),
        {
            "tier2": {"models": ["a", "b"]},
            "tier0": {"models": ["b", "a", "a"]},
        },
    ) == []


def test_detect_collision_when_shared_rung_models_differ():
    import oc_model
    cols = oc_model.detect_rung_collisions(
        _evolve_shape_file(),
        {
            "tier2": {"models": ["anthropic/claude-opus-4-8"]},
            "tier0": {"models": ["anthropic/claude-sonnet-4-6"]},
        },
    )
    assert len(cols) == 1
    assert cols[0]["rung"] == "sonnet-class"
    assert set(cols[0]["roles"]) == {"standard", "judge"}
    msg = oc_model.format_rung_collision_error(cols)
    assert "sonnet-class" in msg
    assert "standard" in msg and "judge" in msg


def test_detect_collision_uses_default_tier_to_rung_without_explicit_roles():
    import oc_model
    # Even a new-shape file with no explicit standard/judge roles defaults
    # tier2 AND tier0 to sonnet-class (_TIER_TO_RUNG) — the collision is
    # structural, not specific to the evolve roles map.
    bare = {"rungs": [{"id": "sonnet-class", "models": [], "costClass": "medium"}]}
    cols = oc_model.detect_rung_collisions(
        bare, {"tier2": {"models": ["x"]}, "tier0": {"models": ["y"]}}
    )
    assert len(cols) == 1
    assert cols[0]["rung"] == "sonnet-class"


# ── unit: writer guard (json_full_config_set raises) ─────────────────────────


def test_json_full_config_set_raises_on_collision(tmp_path, monkeypatch):
    import oc_model

    monkeypatch.setattr(oc_model, "_load_tiers_file", lambda bot: _evolve_shape_file())
    # Guard must trip BEFORE any save — fail loudly if a save is attempted.
    monkeypatch.setattr(
        oc_model, "_save_tiers_file",
        lambda *a, **k: pytest.fail("write must be refused before _save_tiers_file"),
    )
    oc_json = tmp_path / "openclaw.json"
    oc_json.write_text("{}")

    with pytest.raises(ValueError) as exc:
        oc_model.json_full_config_set(
            "evolve",
            {"tiers": {
                "tier2": {"models": ["anthropic/claude-opus-4-8"]},
                "tier0": {"models": ["anthropic/claude-sonnet-4-6"]},
            }},
            oc_json_path=oc_json,
        )
    assert str(exc.value).startswith(oc_model.RUNG_COLLISION_PREFIX)


def test_json_full_config_set_allows_single_tier_on_shared_rung(tmp_path, monkeypatch):
    import oc_model

    store = _evolve_shape_file()
    monkeypatch.setattr(oc_model, "_load_tiers_file", lambda bot: store)
    monkeypatch.setattr(oc_model, "_save_tiers_file", lambda *a, **k: None)
    # The recompute of openclaw.json's flat fallback list shells out to the OC
    # CLI; short-circuit it so the unit test stays hermetic.
    monkeypatch.setattr(oc_model, "generate_fallback_list", lambda *a, **k: [])
    oc_json = tmp_path / "openclaw.json"
    oc_json.write_text("{}")

    oc_model.json_full_config_set(
        "evolve",
        {"tiers": {"tier2": {"models": ["anthropic/claude-opus-4-8"]}}},
        oc_json_path=oc_json,
    )
    # The shared sonnet-class rung took the edit — no raise.
    sonnet = next(r for r in store["rungs"] if r["id"] == "sonnet-class")
    assert sonnet["models"] == ["anthropic/claude-opus-4-8"]


# ── endpoint: 409 on collision, verification, honest reporting ───────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app wired to a per-bot NEW-shape tier store, exercised through the
    real oc_model fold/synthesize so the rung-collision is real. ``store`` maps
    bot_id → tiers_file dict (rungs+roles); the fakes mirror the writer's
    collision-guard + synthesize contract that the live subprocess path runs."""
    from evolve_admin.web.server import create_app
    import oc_cli
    import oc_model

    network = {"bots": {"evolve": {"user": "evolve"}}, "sharedDir": str(tmp_path / "shared")}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    store: dict = {"evolve": _evolve_shape_file()}

    def shape_get(bot_id, network_path=None):
        return {
            "bot": bot_id,
            "tiers": oc_model.synthesize_legacy_tiers(store.get(bot_id, {})),
            "catalog": ["anthropic/claude-sonnet-4-6"],
        }

    def shape_set_err(bot_id, updates, network_path=None):
        tf = store.setdefault(bot_id, {})
        if "tiers" in updates:
            # Mirror json_full_config_set: refuse a colliding fold, then apply.
            cols = oc_model.detect_rung_collisions(tf, updates["tiers"])
            if cols:
                return None, oc_model.RUNG_COLLISION_PREFIX + oc_model.format_rung_collision_error(cols)
            oc_model.apply_tiers_update_new_shape(tf, updates["tiers"])
        return (
            {
                "bot": bot_id,
                "tiers": oc_model.synthesize_legacy_tiers(tf),
                "catalog": updates.get("catalog", ["anthropic/claude-sonnet-4-6"]),
                "generatedFallbacks": [],
            },
            None,
        )

    monkeypatch.setattr(oc_cli, "oc_full_config_get", shape_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", shape_set_err)
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {
            "keys": {"anthropic": {"api_key": True}}, "source": "sqlite",
        },
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "store": store}


def _rung(store, bot_id, rung_id):
    return next(r for r in store[bot_id]["rungs"] if r["id"] == rung_id)


def test_endpoint_409_on_rung_collision(app):
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": {
            "tier2": {"models": ["anthropic/claude-opus-4-8"]},
            "tier0": {"models": ["anthropic/claude-sonnet-4-6"]},
        }})
        assert resp.status_code == 409, resp.get_json()
        err = resp.get_json()["error"]
        assert "sonnet-class" in err
        assert "standard" in err and "judge" in err
        # The prefix must be stripped from the operator-facing message.
        assert "RUNG_COLLISION" not in err
    # The store is untouched — the clobber never happened.
    assert _rung(app["store"], "evolve", "sonnet-class")["models"] == [
        "anthropic/claude-sonnet-4-6"
    ]


def test_endpoint_single_changed_tier_lands(app):
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": {
            "tier2": {"models": ["anthropic/claude-opus-4-8"]},
        }})
        assert resp.status_code == 200, resp.get_json()
    # The shared sonnet-class rung actually moved.
    assert _rung(app["store"], "evolve", "sonnet-class")["models"] == [
        "anthropic/claude-opus-4-8"
    ]


def test_endpoint_full_dict_identical_siblings_ok(app):
    # The reconcile path resends the full synthesized tiers — sibling tiers that
    # share a rung carry IDENTICAL models, so it must NOT trip the collision
    # guard (regression: don't false-block catalog reconcile).
    with app["app"].test_client() as c:
        cfg = c.get("/api/admin/config/evolve").get_json()
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": cfg["tiers"]})
        assert resp.status_code == 200, resp.get_json()


def test_endpoint_500_on_silent_non_persist(app, monkeypatch):
    # A write that reports success but does NOT move the rung (silent
    # non-persist) must be caught by the post-write verification — no
    # false-green success.
    import oc_cli
    import oc_model

    store = app["store"]

    def lying_set(bot_id, updates, network_path=None):
        # Accept the write, return success, but never apply the tiers update.
        return (
            {
                "bot": bot_id,
                "tiers": oc_model.synthesize_legacy_tiers(store.get(bot_id, {})),
                "catalog": updates.get("catalog", ["anthropic/claude-sonnet-4-6"]),
                "generatedFallbacks": [],
            },
            None,
        )

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", lying_set)

    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": {
            "tier2": {"models": ["anthropic/claude-opus-4-8"]},
        }})
        assert resp.status_code == 500, resp.get_json()
        assert "did not" in resp.get_json()["error"].lower()
