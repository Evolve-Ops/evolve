"""tests/test_easy_setup_endpoint.py — POST /api/models/easy-setup.

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 6 #2 — the
easy-setup wizard. A ranked provider-order preference reorders every role's
DEFAULT cluster and writes it through the existing safe paths: pod scope splices
``network.json::models.rungs/roles/roleCaps`` (embedding preserved); a bot scope
flips the bot to Custom and writes the wizard result wholesale.

Coverage:
  - preview=true computes + returns the reordered catalog WITHOUT writing.
  - pod scope writes the reordered models into network.json, embedding survives.
  - bot scope writes the bot's custom tiers via the safe config-set seam.
  - bad body (no scope / non-list order) → 400.
  - unknown bot → 404.
  - a single-provider order (judge can't be cross-vendor) → SAVES, with the
    runtime's soft same-vendor judge advisory (#3040), not a write-time error.

The compute draws clusters from the real DEFAULT_MODEL_CATALOG (the only literal
home); credentialed providers come from stubbed bot keys. No real bot/user names
appear; placeholders only.
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

# The default catalog ships anthropic/openai/google models across the tiers, so
# crediting these three exercises the reorder + judge-diversity paths fully.
_ALL_PROVIDERS = ("anthropic", "openai", "google")


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Flask app with a network.json carrying a models.embedding block, a single
    bot credentialed for the catalog's providers, and a stubbed config-set seam."""
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    embedding = {"provider": "anthropic", "model": "anthropic/embed-1", "dimensions": 256}
    network = {
        "members": ["a_bot"],
        "sharedDir": str(shared),
        "bots": {"a_bot": {"role": "member", "user": "a_bot"}},
        "models": {"embedding": dict(embedding)},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network, indent=2))

    # Credentialed-provider stub: the bot holds keys for all catalog providers.
    cred_state = {"a_bot": {"keys": {p: {"api_key": True} for p in _ALL_PROVIDERS}}}
    cfg_calls: list[dict] = []

    def fake_keys_get(bot_id, network_path=None):
        return cred_state.get(bot_id, {"keys": {}})

    def fake_cfg_set_err(bot_id, updates, **kw):
        cfg_calls.append({"bot_id": bot_id, "updates": updates})
        return {"ok": True, "bot": bot_id}, None

    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_keys_get", fake_keys_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", fake_cfg_set_err)
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {
        "app": app,
        "network_path": network_path,
        "cfg_calls": cfg_calls,
        "embedding": embedding,
        "cred_state": cred_state,
    }


def _read_models(network_path: Path) -> dict:
    return json.loads(network_path.read_text()).get("models", {})


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def _role_models(catalog: dict, role: str) -> list:
    roles = catalog.get("roles") or {}
    entry = roles.get(role)
    rung_id = entry if isinstance(entry, str) else (entry or {}).get("rung")
    for rung in catalog.get("rungs") or []:
        if rung.get("id") == rung_id:
            return rung.get("models") or []
    return []


# ── preview ─────────────────────────────────────────────────────────────────────


def test_preview_does_not_write(app_env):
    app, network_path = app_env["app"], app_env["network_path"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["openai", "anthropic", "google"],
            "preview": True,
        })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["preview"] is True
    catalog = body["catalog"]
    # Fast tier leads with the preferred provider (openai present in haiku-class).
    fast = _role_models(catalog, "fast")
    assert _provider_of(fast[0]) == "openai"
    # Nothing persisted — no rungs in network.json.
    assert "rungs" not in _read_models(network_path)


# ── pod scope write ──────────────────────────────────────────────────────────────


def test_pod_write_reorders_and_preserves_embedding(app_env):
    app, network_path = app_env["app"], app_env["network_path"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["google", "openai", "anthropic"],
        })
    assert resp.status_code == 200, resp.get_json()
    models = _read_models(network_path)
    assert models["rungs"] and models["roles"]
    # Fast (haiku-class) holds google — it must lead after a google-first order.
    fast = _role_models(models, "fast")
    assert _provider_of(fast[0]) == "google"
    # The sibling embedding block survives the splice.
    assert models["embedding"] == app_env["embedding"]


# ── bot scope write ──────────────────────────────────────────────────────────────


def test_bot_write_flips_to_custom_via_safe_seam(app_env):
    app = app_env["app"]
    cfg_calls = app_env["cfg_calls"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "a_bot", "provider_order": ["openai", "anthropic", "google"],
        })
    assert resp.status_code == 200, resp.get_json()
    # The wizard result was written wholesale through the config-set seam.
    assert len(cfg_calls) == 1
    updates = cfg_calls[0]["updates"]
    assert cfg_calls[0]["bot_id"] == "a_bot"
    assert updates["rungs"] and updates["roles"]
    fast = _role_models(updates, "fast")
    assert _provider_of(fast[0]) == "openai"


# ── validation / errors ──────────────────────────────────────────────────────────


def test_missing_scope_rejected(app_env):
    app = app_env["app"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={"provider_order": ["openai"]})
    assert resp.status_code == 400


def test_bad_provider_order_rejected(app_env):
    app = app_env["app"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": "openai",
        })
    assert resp.status_code == 400


def test_unknown_bot_rejected(app_env):
    app = app_env["app"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "no_such_bot", "provider_order": ["openai"],
        })
    assert resp.status_code == 404


def test_single_provider_easy_setup_saves(app_env):
    """Crediting only anthropic collapses every tier to one provider, so judge
    cross-vendor diversity is unsatisfiable. Per the soft runtime stance (#3040)
    that is NOT a write-time error — the single-provider catalog SAVES and the
    gateway routes the judge with a same-vendor advisory. (Previously rejected
    400; the credential-matched default view produces single-provider candidates
    on single-provider pods, so the save must succeed.)"""
    import primary_bot
    app, network_path = app_env["app"], app_env["network_path"]
    # Re-credit the bot for anthropic only.
    app_env["cred_state"]["a_bot"] = {"keys": {"anthropic": {"api_key": True}}}
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["anthropic"],
        })
    assert resp.status_code == 200, resp.get_json()
    # The single-provider catalog persisted (pod scope splices network.json).
    models = _read_models(network_path)
    assert "rungs" in models
    provs = {
        primary_bot.provider_of(m)
        for r in models["rungs"] for m in (r.get("models") or [])
    }
    provs.discard(None)
    assert provs == {"anthropic"}


# ── item 12: the wizard's provider source is LLM-filtered ────────────────────────


def test_listings_llm_providers_excludes_non_llm_includes_chat_lister(app_env, tmp_path):
    """The /api/models/listings response (the wizard's provider source) carries
    an llm_providers set that EXCLUDES non-LLM providers (brave, runway) and
    INCLUDES a credentialed provider that lists chat models (deepseek), even
    though deepseek is not in any catalog cluster. Addendum 7 item 12.

    Brave/runway are excluded with no provider-name literal: they list no
    chat-capable model, and they are absent from the catalog clusters.
    """
    import json as _json
    from pathlib import Path as _Path

    app = app_env["app"]
    network_path = app_env["network_path"]
    shared = _Path(_json.loads(network_path.read_text())["sharedDir"])

    # Credit the bot for two catalog providers + deepseek (a chat lister not in
    # any cluster) + brave + runway (non-LLM). All "credentialed".
    app_env["cred_state"]["a_bot"] = {"keys": {p: {"api_key": True} for p in (
        "anthropic", "openai", "deepseek", "brave", "runway",
    )}}

    # Seed a listings cache: deepseek lists a chat model; brave/runway list
    # non-chat entries (so the chat-capability derivation drops them).
    cache = {
        "refreshed_at": "2026-06-11T00:00:00Z",
        "providers": {
            "anthropic": [{"model_id": "claude-sonnet-4-6", "qualified_id": "anthropic/claude-sonnet-4-6", "capabilities": ["chat"]}],
            "openai": [{"model_id": "gpt-5.5", "qualified_id": "openai/gpt-5.5", "capabilities": ["chat"]}],
            "deepseek": [{"model_id": "deepseek-chat", "qualified_id": "deepseek/deepseek-chat", "capabilities": ["chat"]}],
            "brave": [{"model_id": "brave-search", "qualified_id": "brave/brave-search", "capabilities": ["non-chat"]}],
            "runway": [{"model_id": "gen-3-image", "qualified_id": "runway/gen-3-image", "capabilities": ["non-chat"]}],
        },
        "degraded": [],
    }
    (shared / "model-listings.json").write_text(_json.dumps(cache))

    with app.test_client() as c:
        resp = c.get("/api/models/listings")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    llm = set(body.get("llm_providers") or [])

    # DeepSeek (chat lister) is in; brave/runway (non-LLM) are out.
    assert "deepseek" in llm
    assert "brave" not in llm
    assert "runway" not in llm
    # Catalog-cluster providers the pod credentials are in too.
    assert {"anthropic", "openai"}.issubset(llm)
    # The raw credentialed set still carries everything (filter is additive).
    cred = set(body.get("credentialed_providers") or [])
    assert {"brave", "runway"}.issubset(cred)


# ── per-bot credential scoping (pod=template union, bot=its own keys) ─────────────
#
# The single-bot ``app_env`` fixture can't distinguish pod-scope from bot-scope:
# its one bot holds every provider's key, so the pod-wide union and the bot's own
# set are identical. The bug — a bot-scoped easy-setup writing a model from a
# provider the bot has NO key for — only surfaces when a bot's keys are a strict
# SUBSET of the pod union. This fixture builds that: two bots with disjoint keys.


@pytest.fixture
def scoped_env(tmp_path, monkeypatch):
    """App with TWO bots holding DIFFERENT provider keys, so the pod-wide
    credentialed union is a strict superset of any single bot's set.

    ``target_bot`` authenticates anthropic+openai (two providers → judge
    diversity satisfiable); ``peer_bot`` alone holds the google key. The pod
    union is therefore {anthropic, openai, google}, but target_bot's own set is
    the {anthropic, openai} subset — so a google model is pod-credentialed yet
    NOT target_bot-credentialed.
    """
    from evolve_admin.web.server import create_app
    import evolve_admin.web.server as srv

    shared = tmp_path / "evolve"
    shared.mkdir()
    network = {
        "members": ["target_bot", "peer_bot"],
        "sharedDir": str(shared),
        "bots": {
            "target_bot": {"role": "member", "user": "target_bot"},
            "peer_bot": {"role": "member", "user": "peer_bot"},
        },
        "models": {},
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network, indent=2))

    cred_state = {
        "target_bot": {"keys": {"anthropic": {"api_key": True}, "openai": {"api_key": True}}},
        "peer_bot": {"keys": {"google": {"api_key": True}}},
    }
    cfg_calls: list[dict] = []

    def fake_keys_get(bot_id, network_path=None):
        return cred_state.get(bot_id, {"keys": {}})

    def fake_cfg_set_err(bot_id, updates, **kw):
        cfg_calls.append({"bot_id": bot_id, "updates": updates})
        return {"ok": True, "bot": bot_id}, None

    import oc_cli
    monkeypatch.setattr(oc_cli, "oc_keys_get", fake_keys_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_set_with_error", fake_cfg_set_err)
    monkeypatch.setattr(srv, "_audit_log_entry", lambda *a, **kw: None)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {
        "app": app,
        "network_path": network_path,
        "cfg_calls": cfg_calls,
        "cred_state": cred_state,
    }


def _all_models(catalog: dict) -> list:
    out: list = []
    for rung in catalog.get("rungs") or []:
        out.extend(rung.get("models") or [])
    return out


def test_bot_scope_excludes_uncredentialed_provider(scoped_env):
    """Bot-scoped easy-setup filters to THAT bot's credentialed providers.
    target_bot holds anthropic+openai but NOT google (only peer_bot does), so no
    google model may land in target_bot's written tiers — the cross-provider
    seed bug (e.g. a stale xai/grok in a non-xAI bot). With the old code (which
    passed the pod union for bot scope) google would leak in."""
    app, cfg_calls = scoped_env["app"], scoped_env["cfg_calls"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "target_bot",
            "provider_order": ["anthropic", "openai", "google"],
        })
    assert resp.status_code == 200, resp.get_json()
    assert len(cfg_calls) == 1 and cfg_calls[0]["bot_id"] == "target_bot"
    provs = {_provider_of(m) for m in _all_models(cfg_calls[0]["updates"])}
    assert "google" not in provs, f"uncredentialed provider leaked: {provs}"
    assert provs and provs <= {"anthropic", "openai"}


def test_bot_scope_preview_matches_filtered_write(scoped_env):
    """preview=true must render the SAME filtered tiers the write produces — the
    preview is bot-scoped too, so google is absent and nothing is persisted."""
    app = scoped_env["app"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "target_bot",
            "provider_order": ["anthropic", "openai", "google"],
            "preview": True,
        })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["preview"] is True
    provs = {_provider_of(m) for m in _all_models(body["catalog"])}
    assert "google" not in provs and provs <= {"anthropic", "openai"}
    assert len(scoped_env["cfg_calls"]) == 0  # preview never writes


def test_pod_scope_keeps_full_union(scoped_env):
    """Pod scope is a template (spec Addendum 5 / 9 §D): it stays the full
    pod-wide credentialed union, so google (held only by peer_bot) IS present
    even though no single bot holds every provider. The fix must not narrow the
    pod path."""
    app, network_path = scoped_env["app"], scoped_env["network_path"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod",
            "provider_order": ["anthropic", "openai", "google"],
        })
    assert resp.status_code == 200, resp.get_json()
    provs = {_provider_of(m) for m in _all_models(_read_models(network_path))}
    assert "google" in provs, f"pod template dropped a credentialed provider: {provs}"


# ── auto-upgrade toggle (spec-model-auto-upgrade-2026-07-30 §Config shape) ──────


def test_preview_carries_auto_upgrade_prefill_families_and_governance(app_env):
    """The preview response includes the scope's current resolved auto-upgrade
    policy (toggle prefill), a qualified-id → family-stem map (version-less
    auto-mode chips), and the pod-scope governance split (which bots the pod
    toggle actually reaches)."""
    app = app_env["app"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["anthropic", "openai", "google"],
            "preview": True,
        })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # Code default: off until an operator turns it on.
    assert body["auto_upgrade"]["enabled"] is False
    assert body["auto_upgrade"]["enabledSource"] == "code-default"
    # Family stems for every previewed model; version tokens stripped.
    fast = _role_models(body["catalog"], "fast")
    assert fast, "fast tier must not be empty"
    assert all(m in body["families"] for m in fast)
    anthropic_fast = [m for m in fast if _provider_of(m) == "anthropic"]
    assert body["families"][anthropic_fast[0]] == "claude-haiku"
    # a_bot has no custom tiers → governed by the pod toggle, none excluded.
    assert body["auto_upgrade_governed"] == ["a_bot"]
    assert body["auto_upgrade_excluded"] == []


def test_pod_write_sets_auto_upgrade_enabled(app_env):
    """auto_upgrade_enabled=true on a pod apply writes models.autoUpgrade,
    merging into an existing block so hand-set subordinate knobs survive."""
    app, network_path = app_env["app"], app_env["network_path"]
    # Pre-seed a hand-set knob that must survive the enabled-only flip.
    net = json.loads(network_path.read_text())
    net["models"]["autoUpgrade"] = {"applyDay": "friday"}
    network_path.write_text(json.dumps(net))

    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["anthropic", "openai", "google"],
            "auto_upgrade_enabled": True,
        })
    assert resp.status_code == 200, resp.get_json()
    au = _read_models(network_path)["autoUpgrade"]
    assert au == {"applyDay": "friday", "enabled": True}


def test_pod_write_without_toggle_leaves_auto_upgrade_alone(app_env):
    """Omitted auto_upgrade_enabled (null) must not touch the block."""
    app, network_path = app_env["app"], app_env["network_path"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["anthropic", "openai", "google"],
        })
    assert resp.status_code == 200, resp.get_json()
    assert "autoUpgrade" not in _read_models(network_path)


def test_bot_write_includes_auto_upgrade_update(app_env):
    """auto_upgrade_enabled on a bot apply rides the same config-set seam as
    the wizard tiers (oc_model partial-merges it into evolve-tiers.json)."""
    app, cfg_calls = app_env["app"], app_env["cfg_calls"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "a_bot", "provider_order": ["anthropic", "openai", "google"],
            "auto_upgrade_enabled": True,
        })
    assert resp.status_code == 200, resp.get_json()
    assert cfg_calls[0]["updates"]["autoUpgrade"] == {"enabled": True}


def test_non_bool_auto_upgrade_enabled_rejected(app_env):
    """A string "true" is not a decision to change the pod's model config."""
    app = app_env["app"]
    with app.test_client() as c:
        resp = c.post("/api/models/easy-setup", json={
            "scope": "pod", "provider_order": ["anthropic"],
            "auto_upgrade_enabled": "true",
        })
    assert resp.status_code == 400
