"""tests/test_tier_write_guard_dict_entries.py — the guard must not crash on a
dict-shaped models[] entry.

``tier_write_guard.tiers_did_not_land`` is the post-write truthfulness check for
``PUT /api/admin/config/<bot_id>/tiers``: a truthy write result is not proof the
edit persisted, so it re-reads the synthesized tiers and reports anything the
operator asked for that is missing. It built its "present" set with a bare
``set(... ["models"])``, which raises ``TypeError: unhashable type: 'dict'`` the
moment an on-disk models[] holds a dict entry — turning the endpoint whose whole
theme is honest reporting into an opaque 500.

Reachability (narrow but real, #3566 audit C-2/C-3 follow-up):
  * The writer's fold (``oc_model.apply_tiers_update_new_shape``) keeps strings
    only, so a new-shape file can't grow one. A HAND-EDITED legacy-shape
    ``{bot}/.openclaw/evolve-tiers.json`` can: the PRESERVE branch of
    ``json_full_config_set`` round-trips the legacy ``tiers`` key verbatim, and
    ``synthesize_legacy_tiers`` returns it unchanged on the read back.
  * ``generate_fallback_list`` would hit the same unhashable-dict wall first for
    most tiers — but it skips ``tier0`` (Judge is excluded from the cascade), so
    a dict entry parked in tier0 sails past it and lands on this guard. That is
    the path pinned below.

The fix mirrors the string-only filter already applied to ``want`` three lines
above: a non-string on-disk entry is "not present", not a crash.

The endpoint round-trip below also pins the #3592 contract, which answers the
open question #3593 left behind: ``model_catalog.validate_tiers_shape`` refuses
a dict entry, but only in a tier the PUT is MODIFYING. The dict parked in an
untouched ``tier0`` rides along (the writer's PRESERVE branch re-writes it
verbatim); the same dict sent as an edit to the tier being written is still a
400 — see ``test_dict_entry_in_the_tier_being_written_is_still_rejected``.
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


_SONNET = "anthropic/claude-sonnet-4-6"
_HAIKU = "anthropic/claude-haiku-4-5"


# ── unit: tiers_did_not_land tolerates junk on the RESULT side ───────────────


def test_dict_entry_in_result_does_not_raise():
    from evolve_admin.web import tier_write_guard as g

    # The requested string IS present alongside a hand-edited dict entry.
    missing = g.tiers_did_not_land(
        {"tier0": {"models": [_SONNET]}},
        {"tier0": {"models": [{"model": _SONNET}, _SONNET]}},
    )
    assert missing == []


def test_dict_entry_does_not_mask_a_genuine_non_persist():
    from evolve_admin.web import tier_write_guard as g

    # A dict entry is NOT evidence the requested model landed — even when its
    # inner id matches. The guard reports honestly rather than being talked out
    # of a real miss by an unhashable lookalike.
    missing = g.tiers_did_not_land(
        {"tier0": {"models": [_SONNET]}},
        {"tier0": {"models": [{"model": _SONNET}]}},
    )
    assert missing == [_SONNET]


def test_non_dict_tier_value_in_result_does_not_raise():
    from evolve_admin.web import tier_write_guard as g

    # ``(x or {}).get`` only survives a FALSY junk value; a non-empty list or a
    # string would raise AttributeError. Same class of opaque 500.
    assert g.tiers_did_not_land({"tier0": {"models": [_SONNET]}}, {"tier0": [_SONNET]}) == [_SONNET]
    assert g.tiers_did_not_land({"tier0": {"models": [_SONNET]}}, {"tier0": _SONNET}) == [_SONNET]


# ── endpoint: a legacy file with a dict tier0 entry returns a real response ──


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app over a LEGACY-shape tier store driven through the real
    ``oc_model.json_full_config_set`` PRESERVE branch, so the dict entry
    round-trips exactly as it does on a hand-edited pod file."""
    from evolve_admin.web.server import create_app
    import oc_cli
    import oc_model

    network = {"bots": {"evolve": {"user": "evolve"}}, "sharedDir": str(tmp_path / "shared")}
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    # Hand-edited on disk: tier0 carries a dict entry the UI never writes.
    store: dict = {
        "evolve": {
            "tiers": {
                "tier0": {"models": [{"model": _SONNET, "note": "hand-edited"}, _SONNET]},
                "tier2": {"models": [_SONNET]},
            }
        }
    }
    oc_json = tmp_path / "openclaw.json"
    oc_json.write_text("{}")

    monkeypatch.setattr(oc_model, "_load_tiers_file", lambda bot: store.get(bot, {}))
    monkeypatch.setattr(
        oc_model, "_save_tiers_file", lambda bot, data: store.__setitem__(bot, data)
    )
    # The openclaw.json half of the write shells out to `openclaw config
    # validate`; short-circuit just that so the test stays hermetic. The flat
    # fallback recompute (generate_fallback_list) stays REAL — it is the step
    # that would trip over a dict entry in any tier BUT tier0, and running it
    # for real is what proves the tier0 path reaches the guard at all.
    monkeypatch.setattr(
        oc_model, "_preserve_write",
        lambda data, path: Path(path).write_text(json.dumps(data)),
    )

    def real_get(bot_id, network_path=None):
        return oc_model.json_full_config(bot_id, oc_json_path=oc_json)

    def real_set_err(bot_id, updates, network_path=None):
        try:
            return oc_model.json_full_config_set(bot_id, updates, oc_json_path=oc_json), None
        except ValueError as exc:
            return None, str(exc)

    monkeypatch.setattr(oc_cli, "oc_full_config_get", real_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", real_set_err)
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {
            "keys": {"anthropic": {"api_key": True}}, "source": "sqlite",
        },
    )

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "store": store, "oc_json": oc_json}


def test_endpoint_round_trip_with_dict_tier0_entry(app):
    """The UI's read-modify-write succeeds; the junk in the UNTOUCHED tier rides.

    History, because this one assertion has now flipped twice:

      * #3589 landed it as a 200 — the guard no longer raises ``TypeError:
        unhashable type: 'dict'`` into an opaque 500.
      * #3585 (#3566 audit C-3) landed ``validate_tiers_shape`` 26 seconds
        later, which 400s a dict entry before any write — because the writer
        keeps only string entries, so persisting one SILENTLY EMPTIES that
        tier. Both were green alone and collided on ``main``.
      * #3593 unbroke ``main`` by taking the 400 and wrote down the cost it
        left behind: a PRE-EXISTING dict entry the operator did not touch
        blocked the whole PUT, so tier2 could not be edited until tier0's junk
        was hand-cleaned on the pod — including by the very edit that would
        have removed it. It flagged "should the validator ignore unchanged
        pre-existing junk?" as an open product question.
      * #3592 answers it: **strictness is scoped to the tiers being MODIFIED.**
        Here tier0 is echoed back byte-identical from the GET, so it is not
        this operator's edit and it is not validated; tier2 is, and it is
        clean. The erasure #3585 measured still cannot happen, because erasing
        a tier requires the payload to NAME it with dict entries — which is a
        modification — see
        ``test_dict_entry_in_the_tier_being_written_is_still_rejected``.

    Measured against the real writer before the scoping was built: this fixture
    is the legacy PRESERVE branch (``tiers_file["tiers"] = updates["tiers"]``),
    which re-writes an unchanged tier VERBATIM — so passing tier0 through
    erases nothing. On a new-shape (rungs) file the question cannot even arise:
    ``synthesize_legacy_tiers`` never surfaces a dict, so the UI cannot echo one
    back, and the fold only rewrites the rungs the payload names.
    """
    with app["app"].test_client() as c:
        cfg = c.get("/api/admin/config/evolve").get_json()
        assert {"model": _SONNET, "note": "hand-edited"} in cfg["tiers"]["tier0"]["models"]

        tiers = json.loads(json.dumps(cfg["tiers"]))
        tiers["tier2"] = {"models": [_HAIKU]}
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": tiers})

        assert resp.status_code == 200, resp.get_json()
    # The operator's edit actually landed, and the junk entry was left alone.
    assert app["store"]["evolve"]["tiers"]["tier2"]["models"] == [_HAIKU]
    assert app["store"]["evolve"]["tiers"]["tier0"]["models"][0] == {
        "model": _SONNET, "note": "hand-edited",
    }


def test_endpoint_reports_non_persist_honestly_alongside_dict_entry(app, monkeypatch):
    # Tolerating the dict must not blunt the guard: a write that reports success
    # without moving anything is still a 500, not a false green.
    import oc_cli
    import oc_model

    def lying_set(bot_id, updates, network_path=None):
        return oc_model.json_full_config(bot_id, oc_json_path=app["oc_json"]), None

    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", lying_set)

    with app["app"].test_client() as c:
        resp = c.put(
            "/api/admin/config/evolve/tiers",
            json={"tiers": {"tier0": {"models": [_HAIKU]}}},
        )
        assert resp.status_code == 500, resp.get_json()
        assert "did not" in resp.get_json()["error"].lower()


# ── #3592: strictness is scoped to the tier being MODIFIED ──────────────────


def test_dict_entry_in_the_tier_being_written_is_still_rejected(app):
    """The other half of the contract (#3585's finding, kept intact).

    The round-trip above passes because ``tier0`` is echoed back byte-identical
    — the operator is not editing it. Send a dict as an actual EDIT to a tier
    and the write is refused before anything is written: the writer keeps only
    string entries, so it would set that tier's rung to ``[]`` and report
    success.
    """
    before = json.loads(json.dumps(app["store"]["evolve"]))

    with app["app"].test_client() as c:
        cfg = c.get("/api/admin/config/evolve").get_json()
        tiers = json.loads(json.dumps(cfg["tiers"]))
        # An EDIT to tier0 (different from what is on disk), still dict-shaped.
        tiers["tier0"] = {"models": [{"model": _HAIKU, "note": "new"}]}
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": tiers})

    assert resp.status_code == 400, resp.get_json()
    err = resp.get_json()["error"]
    assert "tier0" in err and "models[0]" in err
    assert "silently empties the tier" in err
    # Nothing was written — not even the untouched tiers in the same payload.
    assert app["store"]["evolve"] == before


def test_modifying_a_junk_tier_to_a_clean_value_is_accepted(app):
    """The escape hatch the blanket 400 took away: the operator can fix it.

    ``tier0`` holds a dict on disk. Replacing it with plain model ids IS a
    modification, so it is validated strictly — and it passes, because the new
    value is clean. Under a whole-payload 400 this write was impossible from
    the UI.
    """
    with app["app"].test_client() as c:
        cfg = c.get("/api/admin/config/evolve").get_json()
        tiers = json.loads(json.dumps(cfg["tiers"]))
        tiers["tier0"] = {"models": [_SONNET]}
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": tiers})
        assert resp.status_code == 200, resp.get_json()

    assert app["store"]["evolve"]["tiers"]["tier0"]["models"] == [_SONNET]


def test_unhashable_id_in_an_untouched_tier_does_not_500(app):
    """``_all_tier_models`` walks the WHOLE payload, untouched tiers included.

    ``{"id": {"nested": 1}}`` used to raise ``TypeError: unhashable type:
    'dict'`` out of its ``mid not in seen`` — an opaque 500. Now that an
    untouched tier can carry junk past the validator, that walk has to survive
    it.
    """
    app["store"]["evolve"]["tiers"]["tier0"] = {"models": [{"id": {"nested": 1}}]}

    with app["app"].test_client() as c:
        cfg = c.get("/api/admin/config/evolve").get_json()
        tiers = json.loads(json.dumps(cfg["tiers"]))
        tiers["tier2"] = {"models": [_HAIKU]}
        resp = c.put("/api/admin/config/evolve/tiers", json={"tiers": tiers})

    assert resp.status_code == 200, resp.get_json()
    assert app["store"]["evolve"]["tiers"]["tier2"]["models"] == [_HAIKU]


# ── unit: the scoping rule itself ───────────────────────────────────────────


def test_validate_tiers_shape_scopes_per_entry_rules_to_changed_tiers():
    from evolve_admin.model_catalog import validate_tiers_shape

    current = {
        "tier0": {"models": [{"model": _SONNET, "note": "hand-edited"}]},
        "tier2": {"models": [_SONNET]},
    }
    payload = {
        "tier0": {"models": [{"model": _SONNET, "note": "hand-edited"}]},
        "tier2": {"models": [_HAIKU]},
    }
    # tier0 unchanged → skipped; tier2 changed but clean → fine.
    assert validate_tiers_shape(payload, current_tiers=current) is None
    # Same payload with no baseline → strict everywhere (the in-process guard).
    assert "tier0" in (validate_tiers_shape(payload) or "")


def test_validate_tiers_shape_rejects_a_changed_tier_and_names_it():
    from evolve_admin.model_catalog import validate_tiers_shape

    current = {"tier0": {"models": [_SONNET]}, "tier2": {"models": [_SONNET]}}
    err = validate_tiers_shape(
        {"tier0": {"models": [_SONNET]}, "tier2": {"models": [{"id": _HAIKU}]}},
        current_tiers=current,
    )
    assert err is not None
    assert "tier2" in err and "models[0]" in err
    assert "tier0" not in err          # the untouched tier is not blamed
    assert "silently empties the tier" in err


def test_validate_tiers_shape_structural_rules_stay_unconditional():
    """A string ``models`` corrupts the catalog from ANY tier in the payload —
    ``_all_tier_models`` walks the whole document — so the structural rules are
    never scoped, and an unreadable/absent baseline validates strictly."""
    from evolve_admin.model_catalog import validate_tiers_shape

    current = {"tier0": {"models": _SONNET}}
    assert "must be a list" in (validate_tiers_shape(
        {"tier0": {"models": _SONNET}}, current_tiers=current) or "")
    assert "must be an object" in (validate_tiers_shape(
        {"tier0": "nope"}, current_tiers=current) or "")
    assert "tiers must be an object" in (validate_tiers_shape(
        "nope", current_tiers=current) or "")
    # No baseline at all (a failed config read) → nothing is "unchanged".
    assert validate_tiers_shape(
        {"tier0": {"models": [{"id": _SONNET}]}}, current_tiers=None) is not None
    assert validate_tiers_shape(
        {"tier0": {"models": [{"id": _SONNET}]}}, current_tiers="junk") is not None
