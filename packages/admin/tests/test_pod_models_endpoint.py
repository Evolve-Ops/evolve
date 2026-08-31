"""tests/test_pod_models_endpoint.py — GET/PUT /api/admin/config/pod/models.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 5 — the POD-tab
"Default tier definitions" editor. The pod layer
(``network.json::models.rungs/roles``) is edited like the bot tier editor and
written via ``evolve_config._patch_network_json``, which splices ONLY the given
key path so a sibling ``models.embedding`` block survives.

Coverage:
  - GET returns the effective default cluster + per-role layer + podConfigured.
  - PUT (set) validates the judge provider-diversity invariant, writes
    rungs/roles into network.json, and PRESERVES models.embedding.
  - PUT (set) of an undiverse judge is rejected (400) with no write.
  - PUT (reset) clears rungs/roles but keeps models.embedding.
  - A Use-defaults bot's resolution reflects the pod override after a set.

No real bot/user names appear; placeholders only.
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


# A valid candidate pod default: standard spans two providers so judge
# (pointing at the standard rung) can pick a provider distinct from standard's.
_VALID = {
    "rungs": [
        {"id": "fast-default", "models": ["prov_a/fast"]},
        {"id": "standard-default", "models": ["prov_a/std", "prov_b/std"]},
        {"id": "power-default", "models": ["prov_a/power"]},
        {"id": "max-default", "models": ["prov_a/max"]},
        {"id": "judge-default", "models": ["prov_a/std", "prov_b/std"]},
    ],
    "roles": {
        "fast": "fast-default",
        "standard": "standard-default",
        "power": "power-default",
        "max": "max-default",
        "judge": {"rung": "judge-default", "provider": "not-standard"},
    },
}

# An embedding block that must survive every pod-tier write/reset.
_EMBEDDING = {"provider": "prov_a", "model": "prov_a/embed-1", "dimensions": 256}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Flask app with a real tmp network.json carrying a models.embedding block."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["a_bot"],
        "sharedDir": str(shared),
        "bots": {"a_bot": {"role": "member"}},
        "models": {"embedding": dict(_EMBEDDING)},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network, indent=2))

    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return app, network_path


def _read_models(network_path: Path) -> dict:
    return json.loads(network_path.read_text()).get("models", {})


# ── GET ───────────────────────────────────────────────────────────────────────


def test_get_no_pod_layer_all_default(app_env):
    app, _ = app_env
    with app.test_client() as c:
        resp = c.get("/api/admin/config/pod/models")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["podConfigured"] is False
    for role in ("fast", "standard", "power", "max", "judge"):
        assert body["roleLayers"][role] == "default", role
    assert body["rungs"] and body["roles"]


def test_get_payload_carries_role_default_models(app_env, monkeypatch):
    """The GET payload surfaces the per-role RECOMMENDED set (code-default
    cluster) the POD picker stars (spec §Addendum 6). With a pod override on
    standard, the recommended set for standard is the CODE default cluster (the
    'Reset to Evolve defaults' target), NOT the pod-merged cluster already in
    the editor. The cluster is credential-matched to the pod's credentialed
    providers."""
    app, network_path = app_env

    import primary_bot
    cat_default = primary_bot.merge_model_catalog({}, {})
    std_rung = primary_bot._resolve_role_to_rung(cat_default, "standard")
    code_default_std = primary_bot._rung_models(cat_default, std_rung)

    # Credential the pod for every provider named in the code-default catalog
    # (derived from the catalog, no provider-name literals) so the credential
    # filter is a no-op and the recommended set is the full code-default cluster.
    cred_providers = sorted({
        p for r in (cat_default.get("rungs") or [])
        for m in (r.get("models") or [])
        if (p := primary_bot.provider_of(m))
    })
    import oc_cli
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {
            "keys": {p: {"api_key": True} for p in cred_providers}
        },
    )

    # Materialize a pod override that changes standard's cluster.
    with app.test_client() as c:
        c.put("/api/admin/config/pod/models", json=_VALID)
        resp = c.get("/api/admin/config/pod/models")
    assert resp.status_code == 200
    body = resp.get_json()
    # Every role carries a recommended list.
    rdm = body["roleDefaultModels"]
    for role in ("fast", "standard", "power", "max", "judge"):
        assert isinstance(rdm[role], list), role
    # The recommended set for standard is the CODE default cluster, NOT the
    # pod-overridden cluster now in the editor.
    assert rdm["standard"] == code_default_std
    # The pod cluster (what's already in the editor) is the placeholder set;
    # the recommendation must NOT echo it.
    pod_std_rung = body["roles"]["standard"]
    pod_std_models = next(
        (r["models"] for r in body["rungs"] if r["id"] == pod_std_rung), [],
    )
    assert rdm["standard"] != pod_std_models


def test_get_role_default_models_credential_matched(app_env, monkeypatch):
    """The recommended (code-default) cluster is filtered to the pod's
    credentialed providers — the picker never stars a model no bot can run."""
    app, _ = app_env

    import primary_bot
    cat_default = primary_bot.merge_model_catalog({}, {})
    # Find a role whose code-default cluster spans >1 provider; keep only one.
    target_role, keep_provider = None, None
    for role in ("fast", "standard", "power", "max", "judge"):
        rung = primary_bot._resolve_role_to_rung(cat_default, role)
        models = primary_bot._rung_models(cat_default, rung) if rung else []
        provs = sorted({p for m in models if (p := primary_bot.provider_of(m))})
        if len(provs) >= 2:
            target_role, keep_provider = role, provs[0]
            break
    assert target_role, "expected a code-default role cluster spanning >1 provider"

    import oc_cli
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {"keys": {keep_provider: {"api_key": True}}},
    )

    with app.test_client() as c:
        resp = c.get("/api/admin/config/pod/models")
    assert resp.status_code == 200
    rec = resp.get_json()["roleDefaultModels"][target_role]
    assert rec, "the credentialed provider should keep at least one recommendation"
    assert all(primary_bot.provider_of(m) == keep_provider for m in rec)


def test_get_displayed_rungs_credential_matched(app_env, monkeypatch):
    """The DISPLAYED tier rows are credential-matched too — given a pod
    credentialed for one provider, each tier shows only that provider's models
    (the operator-facing bug this fix closes). No provider literals — the target
    provider is derived from the code catalog."""
    app, _ = app_env

    import primary_bot
    cat_default = primary_bot.merge_model_catalog({}, {})
    # A ladder role whose code-default cluster spans >1 provider; keep one.
    target_role, keep_provider = None, None
    for role in ("fast", "standard", "power", "max"):
        rung = primary_bot._resolve_role_to_rung(cat_default, role)
        models = primary_bot._rung_models(cat_default, rung) if rung else []
        provs = sorted({p for m in models if (p := primary_bot.provider_of(m))})
        if len(provs) >= 2:
            target_role, keep_provider = role, provs[0]
            break
    assert target_role, "expected a code-default role cluster spanning >1 provider"

    import oc_cli
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {"keys": {keep_provider: {"api_key": True}}},
    )

    with app.test_client() as c:
        resp = c.get("/api/admin/config/pod/models")
    assert resp.status_code == 200
    body = resp.get_json()
    rung_id = body["roles"][target_role]
    shown = next((r["models"] for r in body["rungs"] if r["id"] == rung_id), None)
    assert shown, "the credentialed provider should keep at least one model in the tier"
    assert all(primary_bot.provider_of(m) == keep_provider for m in shown)


# ── PUT set ────────────────────────────────────────────────────────────────────


def test_set_writes_rungs_roles_and_preserves_embedding(app_env):
    app, network_path = app_env
    with app.test_client() as c:
        resp = c.put("/api/admin/config/pod/models", json=_VALID)
    assert resp.status_code == 200, resp.get_json()
    models = _read_models(network_path)
    # The write path CANONICALIZES synthetic ``*-default`` rung ids → canonical
    # ids + backfills costClass (spec Addendum 8 §D), so the saved rungs OVERLAY
    # the code default instead of accumulating. judge owns its own judge-class
    # rung (spec §Addendum 16), cost-ordered next to sonnet-class, so the saved
    # catalog has 5 canonical rungs.
    saved_ids = [r["id"] for r in models["rungs"]]
    assert saved_ids == [
        "haiku-class", "sonnet-class", "judge-class", "opus-class", "fable-class",
    ], saved_ids
    assert all(r.get("costClass") for r in models["rungs"]), models["rungs"]
    # Each role points at its canonical rung; judge keeps the {rung, provider} form.
    assert models["roles"]["fast"] == "haiku-class"
    assert models["roles"]["standard"] == "sonnet-class"
    assert models["roles"]["power"] == "opus-class"
    assert models["roles"]["max"] == "fable-class"
    assert models["roles"]["judge"] == {"rung": "judge-class", "provider": "not-standard"}
    # The clusters' members survive the rename; judge's cluster is its OWN rung,
    # independent of standard's sonnet-class.
    sonnet = next(r for r in models["rungs"] if r["id"] == "sonnet-class")
    assert "prov_a/std" in sonnet["models"] and "prov_b/std" in sonnet["models"]
    judge = next(r for r in models["rungs"] if r["id"] == "judge-class")
    assert judge["models"] == ["prov_a/std", "prov_b/std"]
    # The sibling embedding block is untouched (the splice invariant).
    assert models["embedding"] == _EMBEDDING


def test_set_undiverse_judge_rejected_no_write(app_env):
    app, network_path = app_env
    bad = json.loads(json.dumps(_VALID))
    # Collapse standard + judge to a single provider so judge shares standard's
    # provider, but keep a SECOND provider in another rung so the candidate still
    # spans ≥2 providers — diversity is satisfiable, so the hard judge check
    # fires (a single-provider candidate would instead save; see
    # test_set_single_provider_candidate_saves).
    for rung in bad["rungs"]:
        if rung["id"] in ("standard-default", "judge-default"):
            rung["models"] = ["prov_a/std"]
        if rung["id"] == "power-default":
            rung["models"] = ["prov_a/power", "prov_b/power"]
    with app.test_client() as c:
        resp = c.put("/api/admin/config/pod/models", json=bad)
    assert resp.status_code == 400
    assert "diversity" in resp.get_json()["error"].lower()
    # Nothing written — no rungs/roles, embedding still intact.
    models = _read_models(network_path)
    assert "rungs" not in models
    assert models["embedding"] == _EMBEDDING


def test_set_single_provider_candidate_saves(app_env):
    """A single-provider candidate (every rung from one provider, the shape a
    credential-matched anthropic-only pod produces) is NOT rejected for judge
    diversity — diversity is unsatisfiable and the runtime routes such a judge
    with the soft advisory (#3040). It must persist."""
    app, network_path = app_env
    single = json.loads(json.dumps(_VALID))
    for rung in single["rungs"]:
        rung["models"] = ["prov_a/" + rung["id"]]
    with app.test_client() as c:
        resp = c.put("/api/admin/config/pod/models", json=single)
    assert resp.status_code == 200, resp.get_json()
    models = _read_models(network_path)
    assert models.get("rungs"), "single-provider candidate should persist"
    assert models["embedding"] == _EMBEDDING


def test_set_bad_body_rejected(app_env):
    app, _ = app_env
    with app.test_client() as c:
        resp = c.put("/api/admin/config/pod/models", json={"rungs": "nope"})
    assert resp.status_code == 400


# ── PUT reset ──────────────────────────────────────────────────────────────────


def test_reset_clears_pod_layer_keeps_embedding(app_env):
    app, network_path = app_env
    with app.test_client() as c:
        # First materialize a pod override...
        c.put("/api/admin/config/pod/models", json=_VALID)
        assert "rungs" in _read_models(network_path)
        # ...then reset it.
        resp = c.put("/api/admin/config/pod/models", json={"mode": "reset"})
    assert resp.status_code == 200
    models = _read_models(network_path)
    assert "rungs" not in models and "roles" not in models
    # Embedding survives the reset.
    assert models["embedding"] == _EMBEDDING


# ── Resolution reflects the pod override ───────────────────────────────────────


def test_use_defaults_bot_resolves_pod_override(app_env):
    """After a pod set, a Use-defaults bot's merged resolution reflects the new
    pod cluster (resolution logic unchanged — this proves the write lands in the
    layer the resolver reads)."""
    app, network_path = app_env
    with app.test_client() as c:
        c.put("/api/admin/config/pod/models", json=_VALID)

    import primary_bot
    net = json.loads(network_path.read_text())
    # The bot has no per-bot tiers, so defaults ← pod resolves the pod cluster.
    view = primary_bot.pod_default_catalog_view(net)
    assert view["podConfigured"] is True
    assert view["roleLayers"]["standard"] == "pod"
    # Standard resolves to the pod-set cluster (two providers, pod-authored).
    std_rung = view["roles"]["standard"]
    rung_models = next(
        (r["models"] for r in view["rungs"] if r["id"] == std_rung), []
    )
    assert rung_models == ["prov_a/std", "prov_b/std"]


def test_get_tier_view_populated_from_sqlite_store_source(app_env, monkeypatch):
    """Regression guard for the sqlite-migration outage: when the presence
    reader returns the new sqlite-store shape (``source="sqlite"`` with a
    configured provider), the credential-matched tier view is POPULATED — not
    the empty "No credentialed model for this role" state the broken reader
    produced. The contrast case (empty providers) leaves the tier empty, proving
    the populated state is driven by the credentialed provider, not by chance."""
    app, _ = app_env

    import primary_bot
    cat_default = primary_bot.merge_model_catalog({}, {})
    target_role, keep_provider = None, None
    for role in ("fast", "standard", "power", "max"):
        rung = primary_bot._resolve_role_to_rung(cat_default, role)
        models = primary_bot._rung_models(cat_default, rung) if rung else []
        provs = sorted({p for m in models if (p := primary_bot.provider_of(m))})
        if provs:
            target_role, keep_provider = role, provs[0]
            break
    assert target_role, "expected a code-default role cluster with a provider"

    import oc_cli

    def _tier_models():
        with app.test_client() as c:
            body = c.get("/api/admin/config/pod/models").get_json()
        rung_id = body["roles"][target_role]
        return next((r["models"] for r in body["rungs"] if r["id"] == rung_id), None)

    # Migrated sqlite store reports the provider present (secrets not file-stored,
    # so a configured profile = credentialed): tier must show its models.
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {
            "keys": {keep_provider: {"api_key": True, "token": False}},
            "source": "sqlite",
        },
    )
    assert _tier_models(), "sqlite-credentialed provider should populate the tier"

    # The broken reader returned no providers → the bug's empty tier.
    monkeypatch.setattr(
        oc_cli, "oc_keys_get",
        lambda bot_id, network_path=None: {"keys": {}, "source": "none"},
    )
    assert not _tier_models(), "no credentialed provider should empty the tier"
