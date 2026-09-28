"""Provider-neutral primary-bot seed (#3466 PR-1).

A pod credentialed with ONLY a non-Anthropic provider must come out of
``setup --fresh`` with a WORKING primary bot. These tests pin:

  1. ``_evolve_openclaw_config`` derives evo's ``agents.defaults.model.primary``
     and the evolve plugin's ``classifierModel`` from the credentialed
     providers (``models.derive_seed_models``) instead of hardcoding
     Anthropic ids.
  2. The model catalog contains only credentialed providers' models —
     Anthropic is no longer "always present".
  3. When no credentialed provider has a known pick (free-text provider,
     or no credentials), the fields are left UNSEEDED — loud (wizard_verify
     "primary not set") rather than silently Anthropic.
  4. The cold-start key prompt in ``_select_api_keys_for_evolve`` offers a
     provider choice (mirroring the add-bot wizard) instead of assuming
     Anthropic; Enter still skips.
  5. ``openclaw_materializer.plugin_defaults_for_bot`` derives the deploy-time
     classifierModel default from the bot's own credentials.

Principle: docs/principle-llm-provider-agnostic.md.
"""

from __future__ import annotations

import pytest

from evolve_admin import setup_wizard
from evolve_admin.openclaw_materializer import plugin_defaults_for_bot


def _flat_profiles(*providers: str) -> dict:
    return {
        f"{p}:api": {"type": "api_key", "provider": p, "key": f"{p}-secret"}
        for p in providers
    }


def _seed_cfg(*providers: str) -> dict:
    return setup_wizard._evolve_openclaw_config(
        "podname", "/tmp/shared", "", auth_profiles_flat=_flat_profiles(*providers),
    )


# ── _evolve_openclaw_config: derived primary + classifier ───────────────────


def test_anthropic_pod_seeds_exactly_todays_models():
    """Regression pin: behavior unchanged when anthropic is credentialed."""
    cfg = _seed_cfg("anthropic")
    assert cfg["agents"]["defaults"]["model"] == {
        "primary": "anthropic/claude-sonnet-4-6", "fallbacks": [],
    }
    plug = cfg["plugins"]["entries"]["evolve"]["config"]
    assert plug["classifierModel"] == "anthropic/claude-haiku-4-5"
    assert sorted(cfg["agents"]["defaults"]["models"]) == [
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-opus-4-6",
        "anthropic/claude-sonnet-4-6",
    ]


def test_openai_only_pod_seeds_openai_primary_and_classifier():
    cfg = _seed_cfg("openai")
    assert cfg["agents"]["defaults"]["model"] == {
        "primary": "openai/gpt-4o", "fallbacks": [],
    }
    plug = cfg["plugins"]["entries"]["evolve"]["config"]
    assert plug["classifierModel"] == "openai/gpt-4o-mini"
    # Catalog: openai models only — no presumed-Anthropic trio.
    models = cfg["agents"]["defaults"]["models"]
    assert models and all(m.startswith("openai/") for m in models)


def test_unknown_provider_only_pod_leaves_fields_unseeded():
    """A free-text provider with no _PROVIDER_PICKS entry must NOT be
    silently replaced with Anthropic — leave primary unseeded so
    wizard_verify's "primary not set" check fires."""
    cfg = _seed_cfg("mistral")
    assert "model" not in cfg["agents"]["defaults"]
    assert "classifierModel" not in cfg["plugins"]["entries"]["evolve"]["config"]
    assert cfg["agents"]["defaults"]["models"] == {}


def test_no_credentials_pod_leaves_fields_unseeded():
    cfg = setup_wizard._evolve_openclaw_config(
        "podname", "/tmp/shared", "", auth_profiles_flat={},
    )
    assert "model" not in cfg["agents"]["defaults"]
    assert "classifierModel" not in cfg["plugins"]["entries"]["evolve"]["config"]
    assert cfg["agents"]["defaults"]["models"] == {}


def test_mixed_pod_keeps_anthropic_first_and_both_catalogs():
    cfg = _seed_cfg("anthropic", "openai")
    assert cfg["agents"]["defaults"]["model"]["primary"] == "anthropic/claude-sonnet-4-6"
    models = set(cfg["agents"]["defaults"]["models"])
    assert "anthropic/claude-sonnet-4-6" in models
    assert "openai/gpt-4o" in models


# ── _select_api_keys_for_evolve: cold-start provider choice ─────────────────


def _run_cold_start(monkeypatch, answers: list[str], secret: str = ""):
    """Drive the no-existing-keys branch with seam-injected input."""
    import evolve_admin.wizard as wizard

    monkeypatch.setattr(wizard, "_find_existing_keys", lambda: [])
    answer_iter = iter(answers)
    monkeypatch.setattr(
        setup_wizard, "_ask",
        lambda prompt, default="", non_interactive=False: next(answer_iter),
    )
    monkeypatch.setattr(
        setup_wizard, "_ask_secret",
        lambda prompt, non_interactive=False: secret,
    )
    return setup_wizard._select_api_keys_for_evolve(non_interactive=False)


def test_cold_start_offers_openai_choice(monkeypatch):
    out = _run_cold_start(monkeypatch, ["2"], secret="sk-openai-test")
    assert out["profiles"] == {
        "openai:api": {"type": "api_key", "provider": "openai", "key": "sk-openai-test"},
    }
    assert out["lastGood"] == {"openai": "openai:api"}


def test_cold_start_anthropic_infers_token_type(monkeypatch):
    out = _run_cold_start(monkeypatch, ["1"], secret="sk-ant-oat-abc")
    (profile,) = out["profiles"].values()
    assert profile["provider"] == "anthropic"
    assert profile["type"] == "token"
    assert profile["token"] == "sk-ant-oat-abc"


def test_cold_start_token_prefix_is_anthropic_only(monkeypatch):
    """sk-ant-oat inference must not apply to other providers."""
    out = _run_cold_start(monkeypatch, ["2"], secret="sk-ant-oat-weird")
    (profile,) = out["profiles"].values()
    assert profile["provider"] == "openai"
    assert profile["type"] == "api_key"


def test_cold_start_other_provider_free_text(monkeypatch):
    out = _run_cold_start(monkeypatch, ["3", "Mistral"], secret="mk-1")
    (pid, profile), = out["profiles"].items()
    assert pid == "mistral:api"
    assert profile == {"type": "api_key", "provider": "mistral", "key": "mk-1"}


def test_cold_start_enter_skips(monkeypatch):
    out = _run_cold_start(monkeypatch, [""])
    assert out == {"version": 1, "profiles": {}, "lastGood": {}}


def test_cold_start_rejects_invalid_choice_then_accepts(monkeypatch):
    out = _run_cold_start(monkeypatch, ["9", "2"], secret="sk-x")
    (profile,) = out["profiles"].values()
    assert profile["provider"] == "openai"


def test_cold_start_blank_key_returns_empty(monkeypatch):
    out = _run_cold_start(monkeypatch, ["2"], secret="")
    assert out["profiles"] == {}


def test_cold_start_non_interactive_unchanged(monkeypatch):
    import evolve_admin.wizard as wizard

    monkeypatch.setattr(wizard, "_find_existing_keys", lambda: [])
    out = setup_wizard._select_api_keys_for_evolve(non_interactive=True)
    assert out == {"version": 1, "profiles": {}, "lastGood": {}}


# ── plugin_defaults_for_bot: deploy-time classifier derivation ──────────────

_STATIC = {"classifierModel": "anthropic/claude-haiku-4-5", "tier": "full"}


def test_plugin_defaults_derives_openai_classifier(monkeypatch):
    import evolve_admin.provisioning as provisioning

    monkeypatch.setattr(
        provisioning, "_read_auth_profile_providers", lambda user: ["openai"],
    )
    out = plugin_defaults_for_bot("somebot", _STATIC)
    assert out["classifierModel"] == "openai/gpt-4o-mini"
    assert out["tier"] == "full"
    # Input registry untouched (deploy's module-level constant).
    assert _STATIC["classifierModel"] == "anthropic/claude-haiku-4-5"


def test_plugin_defaults_anthropic_pod_unchanged(monkeypatch):
    import evolve_admin.provisioning as provisioning

    monkeypatch.setattr(
        provisioning, "_read_auth_profile_providers",
        lambda user: ["anthropic", "openai"],
    )
    assert plugin_defaults_for_bot("somebot", _STATIC) is _STATIC


@pytest.mark.parametrize("providers", [[], ["mistral"]])
def test_plugin_defaults_underivable_falls_back_to_static(monkeypatch, providers):
    import evolve_admin.provisioning as provisioning

    monkeypatch.setattr(
        provisioning, "_read_auth_profile_providers", lambda user: providers,
    )
    assert plugin_defaults_for_bot("somebot", _STATIC) is _STATIC


def test_plugin_defaults_read_error_falls_back_to_static(monkeypatch):
    import evolve_admin.provisioning as provisioning

    def _boom(user):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(provisioning, "_read_auth_profile_providers", _boom)
    assert plugin_defaults_for_bot("somebot", _STATIC) is _STATIC


# ─────────────────────────────────────────────────────────────────────────────
# Catalog / picker skew guard (#3466 PR-4: xai grok-4 + grok-3-mini were
# missing from _PROVIDER_CATALOG_MODELS, so an xai-only pod's derived seed
# models weren't in its own catalog)
# ─────────────────────────────────────────────────────────────────────────────


def test_provider_picks_are_in_the_provider_catalog():
    """Every model models._PROVIDER_PICKS can pick for a provider must be in
    that provider's _PROVIDER_CATALOG_MODELS list — otherwise a pod
    credentialed only with that provider gets a seeded primary/classifier
    model absent from its own catalog."""
    import models

    for tier, picks in models._PROVIDER_PICKS.items():
        for provider, model in picks:
            catalog = setup_wizard._PROVIDER_CATALOG_MODELS.get(provider, [])
            assert model in catalog, (
                f"{tier} pick {model!r} missing from "
                f"_PROVIDER_CATALOG_MODELS[{provider!r}]"
            )


def test_xai_only_pod_seeds_models_present_in_catalog():
    cfg = _seed_cfg("xai")
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    catalog = cfg["agents"]["defaults"]["models"]
    assert primary in catalog, (primary, sorted(catalog))
