"""tests/test_tier_write_input_validation.py — input validation on the tier-write surface.

Two findings from the #3566 audit, both reproduced against the real code
before the fix:

C-2 — ``oc_cli.oc_full_config_set_with_error``'s roster guard was FAIL-OPEN
      (``if bots and bot_id not in bots``). An empty roster — network.json
      missing, unreadable, not valid JSON, or valid JSON with no ``bots``
      key — skipped the check entirely, and ``_resolve_user`` then falls back
      to the bot_id itself as a unix username, so an arbitrary bot_id reached
      ``sudo -H -u <bot_id> <python3> …/oc_model.py config set <bot_id> …``.
      Compounding it, the two routes that take ``bot_id`` from the request
      BODY (``/api/models/update-tier``, ``/api/models/update-tier-bulk``) had
      no ``_reject_unknown_bot`` guard at all and relied solely on that
      fail-open check — unlike the eight URL-path routes.

C-3 — a non-list ``models`` value was splatted character-by-character into
      the catalog. ``reconcile_catalog([], {"tier1": {"models":
      "anthropic/claude-opus-5"}})`` returned 17 single-character model ids,
      the route wrote them to ``agents.defaults.models``, and it returned
      ``200 {"ok": true}`` because the post-write truthfulness guard only
      inspects the TIERS side. A non-dict ``tiers`` raised an uncaught
      ``AttributeError`` → opaque 500 where every other bad input gets a 400.

The end-to-end persistence half of C-3 is covered by
``test_string_models_never_reaches_openclaw_json``, which drives the real
``oc_model`` writer against a temp ``openclaw.json`` with a stub ``openclaw``
binary on PATH (the writer shells out to it for schema validation, which is
what blocked the original reporter from confirming persistence).
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


# ── C-2: oc_cli roster guard fails CLOSED ────────────────────────────────────


@pytest.fixture
def oc_cli_mod(monkeypatch):
    """``oc_cli`` with its network.json cache cleared and subprocess.run fatal.

    Any call that reaches ``subprocess.run`` is a guard failure — the whole
    point is that a bot_id we cannot verify never reaches the sudo exec.
    """
    import oc_cli

    monkeypatch.setattr(oc_cli, "_bots_cache", None, raising=False)
    monkeypatch.setattr(oc_cli, "_bots_cache_path", None, raising=False)
    # Hermetic: without this, ``_candidate_network_paths`` falls through to
    # /Users/Shared/evolve/network.json and the analyzer-adjacent dev copy, so
    # on a developer's pod-adjacent machine the "unreadable roster" cases would
    # silently resolve a REAL roster and test the wrong branch.
    monkeypatch.setattr(
        oc_cli, "_candidate_network_paths",
        lambda network_path=None: [network_path] if network_path else [],
    )

    calls: list[list[str]] = []

    def _forbidden(cmd, *a, **kw):
        calls.append(cmd)
        raise AssertionError(f"subprocess.run must not be reached: {cmd!r}")

    monkeypatch.setattr(oc_cli.subprocess, "run", _forbidden)
    return oc_cli


@pytest.mark.parametrize("roster_text", [
    None,                                   # network.json does not exist
    "{ not json at all",                    # unreadable / corrupt
    json.dumps({"sharedDir": "/tmp/x"}),    # valid JSON, no "bots" key
    json.dumps({"bots": {}}),               # valid JSON, empty roster
])
def test_unverifiable_roster_refuses_the_write(oc_cli_mod, tmp_path, roster_text):
    """An unreadable roster is not permission to proceed (C-2, fail-closed)."""
    network_path = tmp_path / "network.json"
    if roster_text is not None:
        network_path.write_text(roster_text)

    result, err = oc_cli_mod.oc_full_config_set_with_error(
        "root", {"tiers": {"tier1": {"models": ["anthropic/claude-opus-5"]}}},
        network_path=str(network_path),
    )

    assert result is None
    assert err is not None
    assert "root" in err and "Refusing write" in err


def test_unknown_bot_with_a_populated_roster_still_refuses(oc_cli_mod, tmp_path):
    """The pre-existing half of the guard is unchanged."""
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {"team_bot_a": {"user": "team_bot_a"}}}))

    result, err = oc_cli_mod.oc_full_config_set_with_error(
        "forge", {"tiers": {}}, network_path=str(network_path),
    )

    assert result is None
    assert err is not None and "unknown bot_id 'forge'" in err
    assert "team_bot_a" in err  # names the known set


def test_a_registered_bot_still_reaches_the_writer(monkeypatch, tmp_path):
    """The fail-closed flip must not block legitimate writes."""
    import oc_cli

    monkeypatch.setattr(oc_cli, "_bots_cache", None, raising=False)
    monkeypatch.setattr(oc_cli, "_bots_cache_path", None, raising=False)
    monkeypatch.setattr(
        oc_cli, "_candidate_network_paths",
        lambda network_path=None: [network_path] if network_path else [],
    )
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({"bots": {"team_bot_a": {"user": "team_bot_a"}}}))

    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"bot": "team_bot_a", "tiers": {}})
        stderr = ""

    def _fake_run(cmd, *a, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(oc_cli.subprocess, "run", _fake_run)

    result, err = oc_cli.oc_full_config_set_with_error(
        "team_bot_a", {"tiers": {}}, network_path=str(network_path),
    )

    assert err is None
    assert result == {"bot": "team_bot_a", "tiers": {}}
    assert "team_bot_a" in seen["cmd"]


# ── Flask app with two real bots ─────────────────────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app over synthetic bot state; writes are captured, not executed."""
    from evolve_admin.web.server import create_app
    import oc_cli

    network = {
        "bots": {"team_bot_a": {"user": "team_bot_a"}},
        "sharedDir": str(tmp_path / "shared"),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))
    (tmp_path / "shared").mkdir()

    from model_registry import RECOMMENDED
    rec_t2 = RECOMMENDED["anthropic"]["tier2"]["model"]

    bot_state: dict = {
        "team_bot_a": {
            "tiers": {
                "tier1": {"models": [RECOMMENDED["anthropic"]["tier1"]["model"]]},
                "tier2": {"models": ["anthropic/claude-sonnet-4-2"]},
            },
            "catalog": [
                RECOMMENDED["anthropic"]["tier1"]["model"],
                "anthropic/claude-sonnet-4-2",
            ],
        },
    }
    writes: list[tuple[str, dict]] = []

    def fake_get(bot_id, network_path=None):
        if bot_id not in bot_state:
            return None
        return {"bot": bot_id, **bot_state[bot_id]}

    def fake_set_err(bot_id, updates, network_path=None):
        writes.append((bot_id, updates))
        if bot_id not in bot_state:
            return None, f"unknown bot {bot_id}"
        if "tiers" in updates:
            # Mirror oc_model's fold: only non-empty STRING model entries
            # survive into the rung store, and the post-write view is
            # synthesized back from it (``synthesize_legacy_tiers`` emits
            # ``{"models": [str, ...]}``). A wholesale echo of the request
            # would make the response shape a fiction.
            folded = {}
            for tier_key, cfg in updates["tiers"].items():
                if not isinstance(cfg, dict):
                    continue
                folded[tier_key] = {"models": [
                    m for m in (cfg.get("models") or [])
                    if isinstance(m, str) and m
                ]}
            bot_state[bot_id].setdefault("tiers", {}).update(folded)
        if "catalog" in updates:
            bot_state[bot_id]["catalog"] = updates["catalog"]
        return {"bot": bot_id, **bot_state[bot_id], "generatedFallbacks": []}, None

    monkeypatch.setattr(oc_cli, "oc_full_config_get", fake_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", fake_set_err)
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {"keys": {"anthropic": {"api_key": True}}},
    )

    flask_app = create_app(network_path)
    flask_app.config["TESTING"] = True
    return {
        "app": flask_app, "bot_state": bot_state, "writes": writes,
        "rec_t2": rec_t2, "network_path": network_path,
    }


# ── C-2: the two body-derived routes reject an unknown bot_id ────────────────


def test_update_tier_rejects_an_unknown_bot_id(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "root", "tier": "tier2",
            "provider": "anthropic", "model": app["rec_t2"],
        })
    assert resp.status_code == 400
    assert "unknown bot_id 'root'" in resp.get_json()["error"]
    assert app["writes"] == []


def test_update_tier_bulk_rejects_an_unknown_bot_id(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "anthropic", "model": app["rec_t2"]},
            {"bot_id": "../../etc", "tier": "tier2",
             "provider": "anthropic", "model": app["rec_t2"]},
        ]})
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert "updates[1]" in err and "unknown bot_id" in err
    # Whole batch rejected up front — the valid entry must not have written.
    assert app["writes"] == []


def test_update_tier_rejects_a_non_string_bot_id(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": {"evil": 1}, "tier": "tier2",
            "provider": "anthropic", "model": app["rec_t2"],
        })
    assert resp.status_code == 400
    assert "bot_id must be a string" in resp.get_json()["error"]
    assert app["writes"] == []


def test_update_tier_still_writes_for_a_registered_bot(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier", json={
            "bot_id": "team_bot_a", "tier": "tier2",
            "provider": "anthropic", "model": app["rec_t2"],
        })
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["ok"] is True
    assert app["rec_t2"] in app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"]


def test_update_tier_bulk_still_writes_for_a_registered_bot(app):
    with app["app"].test_client() as c:
        resp = c.post("/api/models/update-tier-bulk", json={"updates": [
            {"bot_id": "team_bot_a", "tier": "tier2",
             "provider": "anthropic", "model": app["rec_t2"]},
        ]})
    assert resp.status_code == 200, resp.get_json()
    assert app["rec_t2"] in app["bot_state"]["team_bot_a"]["tiers"]["tier2"]["models"]


# ── C-3: malformed tier payloads are a 400, not a corrupt write ──────────────


def test_reconcile_catalog_refuses_a_string_models_value():
    """The original reproduction: 17 single-character model ids."""
    from evolve_admin.model_catalog import reconcile_catalog

    with pytest.raises(ValueError) as exc:
        reconcile_catalog(
            [], {"tier1": {"models": "anthropic/claude-opus-5"}},
            credentialed_providers={"anthropic"},
        )
    assert "models must be a list" in str(exc.value)


def test_reconcile_catalog_refuses_a_non_dict_tiers():
    from evolve_admin.model_catalog import reconcile_catalog

    with pytest.raises(ValueError) as exc:
        reconcile_catalog([], "tier1", credentialed_providers={"anthropic"})
    assert "tiers must be an object" in str(exc.value)


@pytest.mark.parametrize("tiers,fragment", [
    ({"tier1": {"models": "anthropic/claude-opus-5"}}, "models must be a list"),
    # The EMPTY string is the shape that produced a literal 200 ok=true on
    # origin/main: `cfg.get("models") or []` made it invisible to both the
    # reconcile walk and the truthfulness guard, while the writer's fold
    # (`isinstance(models, list)`) dropped the tier — a saved-looking write
    # that wrote nothing. Measured end-to-end, see the PR body.
    ({"tier1": {"models": ""}}, "models must be a list"),
    ({"tier1": {"models": {"0": "anthropic/claude-opus-5"}}}, "models must be a list"),
    ("tier1", "tiers must be an object"),
    (["tier1"], "tiers must be an object"),
    (42, "tiers must be an object"),
    ({"tier1": "anthropic/claude-opus-5"}, "must be an object"),
    ({"tier1": {"models": [["nested"]]}}, "must be a model id string"),
    # Adversarial review: an unhashable id inside a dict entry raised
    # ``TypeError: unhashable type: 'dict'`` out of ``_all_tier_models``'
    # ``mid not in seen`` — the same opaque 500 this validator removes. And
    # a WELL-FORMED dict entry is worse: the writer keeps only string entries,
    # so it empties the rung and reports 200. See
    # ``test_a_dict_model_entry_no_longer_silently_empties_the_tier``.
    ({"tier1": {"models": [{"id": {"nested": 1}}]}}, "must be a model id string"),
    ({"tier1": {"models": [{"id": "anthropic/claude-opus-5"}]}},
     "silently empties the tier"),
    ({"tier1": {"models": [{"provider": "anthropic"}]}}, "must be a model id string"),
    # Blank ids: ``""`` was dropped by the writer's fold while the endpoint
    # answered 200; ``"   "`` is truthy and persisted as a junk catalog entry.
    ({"tier1": {"models": [""]}}, "non-empty model id"),
    ({"tier1": {"models": ["   "]}}, "non-empty model id"),
])
def test_tiers_put_rejects_malformed_payloads_with_400(app, tiers, fragment):
    """400 (not 500, not a silent 200) and nothing reaches the writer."""
    before = json.loads(json.dumps(app["bot_state"]["team_bot_a"]))
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/team_bot_a/tiers", json={"tiers": tiers})

    assert resp.status_code == 400, resp.get_json()
    assert fragment in resp.get_json()["error"]
    assert app["writes"] == []
    assert app["bot_state"]["team_bot_a"] == before


def test_tiers_put_still_round_trips_a_valid_payload(app):
    """The guard must not narrow what already worked."""
    payload = {
        "tier1": {"models": ["anthropic/claude-opus-5"]},
        "tier2": {"models": ["anthropic/claude-sonnet-4-2"]},
        "tier3": {"models": []},
        "tier0": None,
    }
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/config/team_bot_a/tiers", json={"tiers": payload})

    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["ok"] is True
    assert app["writes"][0][1]["tiers"] == payload
    catalog_ids = {
        e["id"] if isinstance(e, dict) else e
        for e in app["bot_state"]["team_bot_a"]["catalog"]
    }
    assert "anthropic/claude-opus-5" in catalog_ids
    # Reconcile added real model ids, not the character-splat of C-3.
    assert all(len(mid) > 1 for mid in catalog_ids)
    assert (
        app["bot_state"]["team_bot_a"]["tiers"]["tier1"]["models"]
        == ["anthropic/claude-opus-5"]
    )


def test_admin_models_put_rejects_an_unknown_bot_id(app, monkeypatch):
    """``oc_model_set`` has no roster guard of its own, so this route is the
    only thing standing between a URL bot_id and ``sudo -H -u <bot_id> …``
    (adversarial-review round 1: it answered 200 for bot_id="root")."""
    import oc_cli

    def _forbidden(*a, **kw):
        raise AssertionError("oc_model_set must not run for an unknown bot")

    monkeypatch.setattr(oc_cli, "oc_model_set", _forbidden)
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/models/root", json={
            "confirm": True, "catalog": ["anthropic/claude-opus-5"],
        })
    assert resp.status_code == 400
    assert "unknown bot_id 'root'" in resp.get_json()["error"]


def test_admin_models_get_rejects_an_unknown_bot_id(app, monkeypatch):
    """Same seam on the read side — ``oc_model_get`` also sudos as the bot."""
    import oc_cli

    def _forbidden(*a, **kw):
        raise AssertionError("oc_model_get must not run for an unknown bot")

    monkeypatch.setattr(oc_cli, "oc_model_get", _forbidden)
    with app["app"].test_client() as c:
        resp = c.get("/api/admin/models/root")
    assert resp.status_code == 400
    assert "unknown bot_id 'root'" in resp.get_json()["error"]


def test_admin_models_put_still_writes_for_a_registered_bot(app, monkeypatch):
    import oc_cli

    seen: dict = {}

    def _set(bot_id, primary, fallbacks, network_path=None):
        seen["args"] = (bot_id, primary, list(fallbacks))
        return True

    monkeypatch.setattr(oc_cli, "oc_model_set", _set)
    with app["app"].test_client() as c:
        resp = c.put("/api/admin/models/team_bot_a", json={
            "confirm": True,
            "catalog": ["anthropic/claude-opus-5", "anthropic/claude-haiku-4-5"],
        })
    assert resp.status_code == 200, resp.get_json()
    assert seen["args"] == (
        "team_bot_a", "anthropic/claude-opus-5", ["anthropic/claude-haiku-4-5"],
    )


def test_validate_model_does_not_sudo_as_an_unknown_scope_bot(app, monkeypatch):
    """Third body-derived bot_id on this surface — it flows to ``oc_keys_get``,
    which has no roster guard of its own, so an unregistered value reached
    ``sudo -H -u <bot_id> … oc_keys.py``.

    Unlike the write routes this is NOT a 400: this endpoint's documented
    contract is to fall open to the pod-wide union for a bot it can't scope to
    (``test_validate_unknown_bot_falls_open_to_pod_wide`` in
    test_model_listings_routes.py pins that). So the scope is dropped rather
    than rejected — the caller sees the same answer as before, and no
    subprocess runs as an unregistered unix user. Adversarial-review round 2,
    narrowed after the existing suite showed the fall-open was deliberate."""
    import oc_cli

    def _forbidden(*a, **kw):
        raise AssertionError("oc_keys_get must not run for an unknown bot")

    monkeypatch.setattr(oc_cli, "oc_keys_get", _forbidden)
    with app["app"].test_client() as c:
        resp = c.post("/api/models/validate-model", json={
            "model": "anthropic/claude-opus-5", "bot_id": "root",
        })
    assert resp.status_code == 200, resp.get_json()


def test_candidate_network_paths_include_the_linux_roster(monkeypatch):
    """``_DEFAULT_NETWORK_PATHS`` is macOS-shaped; on the Linux pod the roster
    lives at ``/var/lib/evolve/network.json``. With the guard failing closed, a
    caller that passes no ``network_path`` and has no ``EVOLVE_NETWORK`` (every
    operator CLI — sudo env_resets it) would otherwise resolve NO roster there
    and refuse a legitimate write.

    The Linux path is asserted EXPLICITLY rather than via
    ``evolve_config.CANONICAL_NETWORK_JSON``: conftest pins the macOS platform
    profile process-wide, and that constant is resolved at import, so a
    profile-derived assertion is satisfied by ``_DEFAULT_NETWORK_PATHS[0]``
    alone and passes on origin/main. Adversarial-review round 2."""
    import oc_cli

    monkeypatch.delenv("EVOLVE_NETWORK", raising=False)
    monkeypatch.setattr(
        oc_cli, "_canonical_network_path",
        lambda: "/var/lib/evolve/network.json",
    )
    paths = oc_cli._candidate_network_paths()

    assert "/var/lib/evolve/network.json" in paths
    # The macOS default must survive alongside it, and nothing may duplicate.
    assert "/Users/Shared/evolve/network.json" in paths
    assert len(paths) == len(set(paths)), f"duplicate candidates: {paths}"


def test_candidate_network_paths_are_deduped_on_macos(monkeypatch):
    """On macOS the canonical path IS the legacy default — the two must
    collapse, leaving the resolution order byte-identical to before."""
    import oc_cli

    monkeypatch.delenv("EVOLVE_NETWORK", raising=False)
    monkeypatch.setattr(
        oc_cli, "_canonical_network_path",
        lambda: "/Users/Shared/evolve/network.json",
    )
    paths = oc_cli._candidate_network_paths()

    assert paths.count("/Users/Shared/evolve/network.json") == 1
    assert paths[0] == "/Users/Shared/evolve/network.json"


def test_candidate_network_paths_keeps_explicit_and_env_first(monkeypatch):
    import oc_cli

    monkeypatch.setenv("EVOLVE_NETWORK", "/env/network.json")
    paths = oc_cli._candidate_network_paths("/explicit/network.json")

    assert paths[0] == "/explicit/network.json"
    assert paths[1] == "/env/network.json"


def test_validate_tiers_shape_accepts_unknown_keys_and_extra_fields():
    """Deliberately shallow — it checks the shape reconcile WALKS, no more."""
    from evolve_admin.model_catalog import validate_tiers_shape

    assert validate_tiers_shape({}) is None
    assert validate_tiers_shape({
        "tier1": {"models": ["a/b"], "label": "Power", "notes": {"x": 1}},
        "tier2": {"models": []},
        "tierUnknown": {"models": ["c/d"]},
        "tier9": {},
        "tier0": None,
    }) is None


# ── C-3 end-to-end: nothing reaches openclaw.json ────────────────────────────


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    """Flask app whose config-set seam runs the REAL oc_model writer.

    ``oc_model._preserve_write`` shells out to the ``openclaw`` binary for
    schema validation before replacing openclaw.json; a stub on PATH stands in
    for it so the write actually lands and the assertion is about persisted
    bytes rather than a mock.
    """
    from evolve_admin.web.server import create_app
    import oc_cli
    import oc_model

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    stub = fakebin / "openclaw"
    stub.write_text('#!/bin/sh\necho \'{"valid": true, "issues": []}\'\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fakebin}:{__import__('os').environ['PATH']}")

    home = tmp_path / "home-bot"
    (home / ".openclaw").mkdir(parents=True)
    oc_json = home / ".openclaw" / "openclaw.json"
    oc_json.write_text(json.dumps({"agents": {"defaults": {"model": {
        "primary": "anthropic/claude-haiku-4-5", "fallbacks": [],
    }}}}))
    monkeypatch.setenv("HOME", str(home))

    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps({
        "bots": {"evolve": {"user": "evolve"}},
        "sharedDir": str(tmp_path / "shared"),
    }))
    (tmp_path / "shared").mkdir()

    def real_get(bot_id, network_path=None):
        return oc_model.json_full_config(bot_id, oc_json)

    def real_set_err(bot_id, updates, network_path=None):
        try:
            return oc_model.json_full_config_set(
                bot_id, updates, oc_json_path=oc_json), None
        except (ValueError, TypeError) as exc:
            return None, str(exc)

    monkeypatch.setattr(oc_cli, "oc_full_config_get", real_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", real_set_err)
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {"keys": {"anthropic": {"api_key": True}}},
    )

    flask_app = create_app(network_path)
    flask_app.config["TESTING"] = True
    return {"app": flask_app, "oc_json": oc_json}


def _oc_models(oc_json: Path):
    return json.loads(oc_json.read_text())["agents"]["defaults"].get("models")


@pytest.mark.parametrize("models", ["anthropic/claude-opus-5", ""])
def test_string_models_never_reaches_openclaw_json(e2e, models):
    """Measured against the real writer on origin/main, both shapes:

      - ``"anthropic/claude-opus-5"`` — ``agents.defaults.models`` gained 17
        single-character keys (``{"a": {}, "n": {}, "t": {}, …}``) BEFORE the
        truthfulness guard ran, so the corruption persisted and the operator
        got a 500 reading "the change was not saved".
      - ``""`` — invisible to both reconcile and the guard, the writer's fold
        dropped the tier, and the endpoint returned 200 ``{"ok": true}`` on a
        write that wrote nothing.

    Both must now be a 400 with openclaw.json byte-identical."""
    before = e2e["oc_json"].read_text()

    with e2e["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/tiers", json={
            "tiers": {"tier1": {"models": models}},
        })

    assert resp.status_code == 400, resp.get_json()
    assert e2e["oc_json"].read_text() == before
    assert _oc_models(e2e["oc_json"]) is None


def test_a_dict_model_entry_no_longer_silently_empties_the_tier(e2e):
    """Adversarial-review round 2, measured against the real writer.

    ``_all_tier_models`` tolerates ``{"id": ...}`` entries, but the WRITER
    keeps only ``isinstance(m, str) and m`` and then calls ``_set_rung_models``
    with the survivors — so a tier whose models are all dicts sets that rung to
    ``[]``. The truthfulness guard counts only string entries, sees nothing
    missing, and reports success. On origin/main:

        PUT tier1 = ["anthropic/claude-opus-5"]        -> 200, rung = [that]
        PUT tier1 = [{"id": "anthropic/claude-sonnet-4-6"}]
                                                       -> 200 {"ok": true}
                                                       -> rung = []   ERASED

    The second PUT must now be a 400 that leaves the rung intact."""
    tiers_path = Path(e2e["oc_json"]).parent / "evolve-tiers.json"

    with e2e["app"].test_client() as c:
        seed = c.put("/api/admin/config/evolve/tiers", json={
            "tiers": {"tier1": {"models": ["anthropic/claude-opus-5"]}},
        })
        assert seed.status_code == 200, seed.get_json()
        rungs_before = json.loads(tiers_path.read_text())["rungs"]

        resp = c.put("/api/admin/config/evolve/tiers", json={
            "tiers": {"tier1": {"models": [{"id": "anthropic/claude-sonnet-4-6"}]}},
        })

    assert resp.status_code == 400, resp.get_json()
    assert "silently empties the tier" in resp.get_json()["error"]
    assert json.loads(tiers_path.read_text())["rungs"] == rungs_before
    assert any(r.get("models") for r in rungs_before), rungs_before


def test_a_valid_tiers_write_still_persists_end_to_end(e2e):
    """Control for the test above — the real writer still lands a good payload."""
    with e2e["app"].test_client() as c:
        resp = c.put("/api/admin/config/evolve/tiers", json={
            "tiers": {"tier1": {"models": ["anthropic/claude-opus-5"]}},
        })

    assert resp.status_code == 200, resp.get_json()
    models = _oc_models(e2e["oc_json"])
    assert models is not None
    assert "anthropic/claude-opus-5" in models
