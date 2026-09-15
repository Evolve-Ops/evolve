"""tests/test_model_listings_routes.py — Phase 9 validated-picker endpoints.

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §Addendum 6.

Covers:
  GET  /api/models/listings          — returns the cached listings, filtered
                                        to credentialed providers + refreshed_at
  POST /api/models/listings/refresh  — re-enumerates (HTTP mocked) + rewrites
                                        the cache
  POST /api/models/validate-model    — free-text validation against the listing
                                        (accepts a listed id, rejects an unlisted
                                        one with a nearest-match suggestion)

All provider enumeration is mocked via an injected enumerator — CI makes zero
live API calls. No real bot/user names are used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app with two synthetic bots: bot_one has anthropic+openai keys,
    bot_two has anthropic only. No live config or HTTP."""
    from evolve_admin.web.server import create_app

    shared = tmp_path / "shared"
    shared.mkdir()
    network = {
        "bots": {
            "bot_one": {"user": "bot_one"},
            "bot_two": {"user": "bot_two"},
        },
        "sharedDir": str(shared),
    }
    network_path = tmp_path / "network.json"
    network_path.write_text(json.dumps(network))

    keys_state = {
        "bot_one": {"keys": {
            "anthropic": {"api_key": True},
            "openai": {"api_key": True},
        }},
        "bot_two": {"keys": {
            "anthropic": {"api_key": True},
        }},
    }

    import oc_cli  # noqa

    def fake_keys_get(bot_id, network_path=None):
        return keys_state.get(bot_id, {"keys": {}})

    def fake_full_config_get(bot_id, network_path=None):
        return {"bot": bot_id}

    monkeypatch.setattr(oc_cli, "oc_keys_get", fake_keys_get)
    monkeypatch.setattr(oc_cli, "oc_full_config_get", fake_full_config_get)

    app = create_app(network_path)
    app.config["TESTING"] = True
    return {"app": app, "shared": shared, "keys_state": keys_state,
            "network_path": network_path}


def _register_bot(app, bot_id: str) -> None:
    """Add a bot to the fixture's network.json roster on disk."""
    np = app["network_path"]
    net = json.loads(np.read_text())
    net["bots"][bot_id] = {"user": bot_id}
    np.write_text(json.dumps(net))


def _seed_cache(shared: Path, providers: dict, degraded=None):
    """Write a listings cache document directly (simulates a prior run)."""
    import model_discovery as md
    doc = {
        "refreshed_at": "2026-06-10T00:00:00Z",
        "providers": providers,
        "degraded": degraded or [],
    }
    (shared / md.LISTINGS_CACHE_NAME).write_text(json.dumps(doc))


def _model(provider, model_id, **kw):
    return {
        "provider": provider,
        "model_id": model_id,
        "qualified_id": f"{provider}/{model_id}",
        **kw,
    }


# ── GET /api/models/listings ──────────────────────────────────────────────────

def test_listings_empty_when_no_cache(app):
    with app["app"].test_client() as c:
        r = c.get("/api/models/listings")
        assert r.status_code == 200
        body = r.get_json()
        assert body["refreshed_at"] is None
        assert body["providers"] == {}


def test_listings_returns_cached_and_filters_to_credentialed(app):
    # Cache carries a provider the pod is NOT credentialed for (google) — it
    # must be filtered out of the picker's candidate source.
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
        "openai": [_model("openai", "gpt-7")],
        "google": [_model("google", "gemini-3")],
    })
    with app["app"].test_client() as c:
        body = c.get("/api/models/listings").get_json()
        assert set(body["providers"].keys()) == {"anthropic", "openai"}
        assert "google" not in body["providers"]
        assert body["refreshed_at"] == "2026-06-10T00:00:00Z"
        # The credentialed set is echoed for the UI grouping.
        assert set(body["credentialed_providers"]) == {"anthropic", "openai"}


def test_listings_passes_through_is_family_latest(app):
    # Phase 10b: discovery annotates each cached model with is_family_latest +
    # family; the route serves per-provider model dicts as-is, so those fields
    # must reach the client (the catalog picker bolds the latest-in-family).
    _seed_cache(app["shared"], {
        "anthropic": [
            _model("anthropic", "claude-opus-4-6",
                   family="claude-opus", is_family_latest=False),
            _model("anthropic", "claude-opus-4-8",
                   family="claude-opus", is_family_latest=True),
        ],
    })
    with app["app"].test_client() as c:
        body = c.get("/api/models/listings").get_json()
        flag = {
            m["model_id"]: m.get("is_family_latest")
            for m in body["providers"]["anthropic"]
        }
        assert flag["claude-opus-4-8"] is True
        assert flag["claude-opus-4-6"] is False
        fam = {m["model_id"]: m.get("family") for m in body["providers"]["anthropic"]}
        assert fam["claude-opus-4-8"] == "claude-opus"


def test_listings_filters_degraded_to_credentialed(app):
    _seed_cache(
        app["shared"],
        {"anthropic": [_model("anthropic", "claude-opus-4-8")]},
        degraded=[
            {"provider": "openai", "reason": "HTTP 401"},
            {"provider": "google", "reason": "HTTP 403"},  # not credentialed
        ],
    )
    with app["app"].test_client() as c:
        body = c.get("/api/models/listings").get_json()
        degraded = {d["provider"] for d in body["degraded"]}
        assert "openai" in degraded
        assert "google" not in degraded


def test_listings_scoped_to_the_viewing_bots_own_credentials(app):
    # The cache is pod-wide, but the picker is per-bot: bot_two holds no openai
    # key, so its tab must not be offered openai models even though bot_one's
    # key put them in the shared cache.
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
        "openai": [_model("openai", "gpt-7")],
    })
    with app["app"].test_client() as c:
        pod = c.get("/api/models/listings").get_json()
        one = c.get("/api/models/listings?bot_id=bot_one").get_json()
        two = c.get("/api/models/listings?bot_id=bot_two").get_json()

    # No bot_id — the pod default editor — keeps the union.
    assert set(pod["providers"].keys()) == {"anthropic", "openai"}
    assert set(one["providers"].keys()) == {"anthropic", "openai"}
    assert set(two["providers"].keys()) == {"anthropic"}
    assert set(two["credentialed_providers"]) == {"anthropic"}


def test_listings_scope_filters_degraded_entries_too(app):
    _seed_cache(
        app["shared"],
        {"anthropic": [_model("anthropic", "claude-opus-4-8")]},
        degraded=[{"provider": "openai", "reason": "HTTP 401"}],
    )
    with app["app"].test_client() as c:
        two = c.get("/api/models/listings?bot_id=bot_two").get_json()
    # bot_two can't reach openai at all, so its outage is not bot_two's problem.
    assert [d["provider"] for d in two["degraded"]] == []


def test_listings_unknown_bot_falls_open_to_pod_union(app):
    # Deliberate: an unreadable/unknown scope shows MORE than it should rather
    # than blanking the picker (scope_credentialed_to_bot's documented
    # fail-open, matching /api/models/validate-model).
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
        "openai": [_model("openai", "gpt-7")],
    })
    with app["app"].test_client() as c:
        body = c.get("/api/models/listings?bot_id=no_such_bot").get_json()
    assert set(body["providers"].keys()) == {"anthropic", "openai"}


def test_unknown_bot_id_never_reaches_keys_get(app, monkeypatch):
    # Negative half of the fail-open contract (#3584 review B1/B3): the union
    # response above is identical with and without the roster guard, so this
    # asserts the ID FLOW instead — a request-derived id absent from the
    # roster must never reach ``oc_keys_get`` (the ``sudo -H -u <bot_id>``
    # sink). Deleting the guard keeps the sibling test green and fails this.
    import model_discovery as md
    import oc_cli

    seen: list[object] = []
    real_keys_get = oc_cli.oc_keys_get

    def recording_keys_get(bot_id, network_path=None):
        seen.append(bot_id)
        return real_keys_get(bot_id, network_path=network_path)

    monkeypatch.setattr(oc_cli, "oc_keys_get", recording_keys_get)
    monkeypatch.setattr(
        md, "run_discovery",
        lambda **kw: None,  # refresh path: no enumeration in this test
    )
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
    })
    with app["app"].test_client() as c:
        r1 = c.get("/api/models/listings?bot_id=no_such_bot")
        r2 = c.post("/api/models/listings/refresh",
                    json={"bot_id": "../../etc/passwd"})
        # B2: a non-string bot_id falls open instead of 500ing on .strip().
        r3 = c.post("/api/models/listings/refresh", json={"bot_id": 7})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert "no_such_bot" not in seen
    assert "../../etc/passwd" not in seen
    assert 7 not in seen


# ── POST /api/models/listings/refresh ─────────────────────────────────────────

def test_refresh_rewrites_cache(app, monkeypatch):
    import model_discovery as md

    # Mock the real enumerator so refresh makes no live HTTP calls.
    def fake_enumerate(provider, key):  # noqa: ARG001
        models = {
            "anthropic": [md.ListedModel(
                provider="anthropic", model_id="claude-opus-4-8",
                qualified_id="anthropic/claude-opus-4-8",
            )],
            "openai": [md.ListedModel(
                provider="openai", model_id="gpt-7",
                qualified_id="openai/gpt-7",
            )],
        }
        return md.ProviderEnumeration(
            provider=provider, ok=True, models=models.get(provider, []),
        )

    monkeypatch.setattr(md, "enumerate_provider", fake_enumerate)
    # Pre-resolve keys so discover_provider_keys (which reads auth-profiles
    # files) is bypassed — keys come from the credentialed fixture providers.
    monkeypatch.setattr(
        md, "discover_provider_keys",
        lambda bot_users: {"anthropic": "k", "openai": "k"},
    )

    with app["app"].test_client() as c:
        r = c.post("/api/models/listings/refresh", json={})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert set(body["providers"].keys()) == {"anthropic", "openai"}
        # Cache file was rewritten on disk.
        cache = json.loads((app["shared"] / md.LISTINGS_CACHE_NAME).read_text())
        assert "anthropic" in cache["providers"]
        assert cache["refreshed_at"]


def test_refresh_narrows_the_response_but_keeps_the_cache_pod_wide(app, monkeypatch):
    import model_discovery as md

    def fake_enumerate(provider, key):  # noqa: ARG001
        models = {
            "anthropic": [md.ListedModel(
                provider="anthropic", model_id="claude-opus-4-8",
                qualified_id="anthropic/claude-opus-4-8",
            )],
            "openai": [md.ListedModel(
                provider="openai", model_id="gpt-7",
                qualified_id="openai/gpt-7",
            )],
        }
        return md.ProviderEnumeration(
            provider=provider, ok=True, models=models.get(provider, []),
        )

    monkeypatch.setattr(md, "enumerate_provider", fake_enumerate)
    monkeypatch.setattr(
        md, "discover_provider_keys",
        lambda bot_users: {"anthropic": "k", "openai": "k"},
    )

    with app["app"].test_client() as c:
        body = c.post("/api/models/listings/refresh",
                      json={"bot_id": "bot_two"}).get_json()

    # Response is scoped to bot_two...
    assert body["ok"] is True
    assert set(body["providers"].keys()) == {"anthropic"}
    # ...but enumeration stayed pod-wide, so the shared cache still holds
    # openai for bot_one's tab (a bot-scoped refresh must not starve siblings).
    cache = json.loads((app["shared"] / md.LISTINGS_CACHE_NAME).read_text())
    assert set(cache["providers"].keys()) == {"anthropic", "openai"}


# ── POST /api/models/validate-model ───────────────────────────────────────────

def test_validate_accepts_listed_model(app):
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
    })
    with app["app"].test_client() as c:
        r = c.post("/api/models/validate-model",
                   json={"model": "claude-opus-4-8"})
        body = r.get_json()
        assert body["ok"] is True
        assert body["canonical"] == "anthropic/claude-opus-4-8"


def test_validate_rejects_unlisted_with_suggestion(app):
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
    })
    with app["app"].test_client() as c:
        r = c.post("/api/models/validate-model",
                   json={"model": "claude-opus-4"})  # typo, missing -8
        body = r.get_json()
        assert body["ok"] is False
        assert body["suggestion"] == "anthropic/claude-opus-4-8"


def test_validate_excludes_uncredentialed_provider(app):
    # google is in the cache but the pod has no google key → not a valid add.
    _seed_cache(app["shared"], {
        "google": [_model("google", "gemini-3")],
    })
    with app["app"].test_client() as c:
        r = c.post("/api/models/validate-model",
                   json={"model": "google/gemini-3"})
        body = r.get_json()
        assert body["ok"] is False


def test_validate_no_cache_yet(app):
    with app["app"].test_client() as c:
        r = c.post("/api/models/validate-model",
                   json={"model": "anything"})
        body = r.get_json()
        assert body["ok"] is False
        assert "refresh" in (body.get("reason") or "")


# ── POST /api/models/validate-model — per-bot credential scoping ──────────────
#
# The free-text "type a model ID" path on a per-bot catalog must be scoped to
# THAT bot's credentialed providers: a bot can't add a model from a provider it
# holds no key for, even though another bot in the pod does (the cache is the
# pod union). Omitting bot_id, or a bot whose creds are unknown, falls open to
# the pod-wide union. Provider names below are illustrative cache data, not
# logic literals.


def test_validate_scoped_to_bot_excludes_uncredentialed_provider(app):
    # bot_two holds only an anthropic key. An openai id is in the pod-wide cache
    # (bot_one keys openai), but bot_two must NOT be able to free-type it.
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
        "openai": [_model("openai", "gpt-7")],
    })
    with app["app"].test_client() as c:
        # Its own provider's model validates.
        own = c.post("/api/models/validate-model",
                     json={"model": "anthropic/claude-opus-4-8",
                           "bot_id": "bot_two"}).get_json()
        assert own["ok"] is True
        # A pod-credentialed-but-not-its-own provider is rejected.
        other = c.post("/api/models/validate-model",
                       json={"model": "openai/gpt-7",
                             "bot_id": "bot_two"}).get_json()
        assert other["ok"] is False


def test_validate_scoped_to_bot_includes_its_own_provider(app, monkeypatch):
    # A bot with {anthropic, xai} can free-type an xai id; a bot with only
    # {anthropic} cannot. Inject an xai-keyed bot for this test only (keeping it
    # out of the pod-wide fixture so the union-echo tests stay stable).
    keys_state = app["keys_state"]
    keys_state["bot_xai"] = {"keys": {
        "anthropic": {"api_key": True},
        "xai": {"api_key": True},
    }}
    # Register it in the roster too. The endpoint now drops an unregistered
    # scope bot_id rather than reading its keys, because doing so ran
    # `sudo -H -u <bot_id> ... oc_keys.py` for an arbitrary unix name (#3566
    # audit C-2). A keys-only bot was a fixture shortcut; the UI only ever
    # sends a registered bot. The assertions below are unchanged.
    _register_bot(app, "bot_xai")
    _seed_cache(app["shared"], {
        "anthropic": [_model("anthropic", "claude-opus-4-8")],
        "xai": [_model("xai", "grok-9")],
    })
    with app["app"].test_client() as c:
        # bot_xai keys xai → accepted.
        ok = c.post("/api/models/validate-model",
                    json={"model": "xai/grok-9",
                          "bot_id": "bot_xai"}).get_json()
        assert ok["ok"] is True
        # bot_two ({anthropic}) does NOT key xai → rejected.
        no = c.post("/api/models/validate-model",
                    json={"model": "xai/grok-9",
                          "bot_id": "bot_two"}).get_json()
        assert no["ok"] is False


def test_validate_unknown_bot_falls_open_to_pod_wide(app):
    # An unknown/unreadable bot_id must fall open to the pod-wide credentialed
    # union (bot_one keys openai), not blank the candidate set.
    _seed_cache(app["shared"], {
        "openai": [_model("openai", "gpt-7")],
    })
    with app["app"].test_client() as c:
        body = c.post("/api/models/validate-model",
                      json={"model": "openai/gpt-7",
                            "bot_id": "no_such_bot"}).get_json()
        assert body["ok"] is True


def test_validate_no_bot_id_stays_pod_wide(app):
    # Omitting bot_id (the POD-level / legacy caller) keeps the pod-wide union.
    _seed_cache(app["shared"], {
        "openai": [_model("openai", "gpt-7")],
    })
    with app["app"].test_client() as c:
        body = c.post("/api/models/validate-model",
                      json={"model": "openai/gpt-7"}).get_json()
        assert body["ok"] is True
