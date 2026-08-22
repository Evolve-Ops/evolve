"""tests/test_oc_model_provider_transport.py — provider blocks carry a transport.

Regression lock for the 2026-07-31 provider misrouting found on the
reference pod. Evolve registered bundled providers as::

    "models": {"providers": {"google": {"models": [{"id": ..., "name": ...}]}}}

with no ``api``. OC never derives ``api`` from the provider id —
``resolveProviderRequestPolicyConfig`` returns ``api: params.api``
unchanged and every resolution site ends in ``?? "openai-responses"`` — so
every model under those blocks was dispatched to the OpenAI Responses
transport. Verified live before the fix::

    [model-fetch] start provider=google api=openai-responses
        model=gemini-3-flash-preview POST https://api.openai.com/v1/responses
    -> 401 Incorrect API key provided: AIzaSy...

A Google key, and separately an ``xai-`` key, were being sent to
api.openai.com. The 401 reads as an expired credential; it is not. Both
providers' rungs were dead fleet-wide, which is how a fallback chain
walked past two cheap rungs and terminated on Opus.

After the fix, verified live on the same pod::

    google -> api=google-generative-ai
              https://generativelanguage.googleapis.com/v1beta/... 200
    xai    -> api=openai-responses  https://api.x.ai/v1/responses    200

Locked here:
  1. Broken-and-verified providers get their transport backfilled.
  2. anthropic / openai are NOT touched (both already work; openai routes
     via the codex app-server and pinning it would likely break that).
  3. Backfill is additive — an operator's self-hosted baseUrl survives.
  4. Pre-existing provider blocks self-heal without a migration.
  5. baseUrl is written only where OC's catalog does not supply one.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import oc_model  # noqa: E402


def _config(*model_keys: str) -> dict:
    """Build the config shape Evolve writes: a catalog plus provider blocks
    carrying only ``models`` — i.e. the pre-fix fleet state."""
    providers: dict[str, dict] = {}
    for key in model_keys:
        provider, _, model_id = key.partition("/")
        providers.setdefault(provider, {"models": []})["models"].append(
            {"id": model_id, "name": model_id}
        )
    return {
        "agents": {"defaults": {"models": {k: {} for k in model_keys}}},
        "models": {"providers": providers},
    }


def test_google_gets_generative_ai_transport():
    """The exact block that was shipping an AIza key to api.openai.com."""
    data = _config("google/gemini-3-flash-preview")
    oc_model.sync_provider_models_from_catalog(data)

    assert data["models"]["providers"]["google"]["api"] == "google-generative-ai"


def test_google_baseurl_is_left_to_the_bundled_catalog():
    """OC's bundled Google catalog supplies the correct baseUrl, and the
    live fix verified 200 without one. Writing a redundant literal here
    would be a second thing to keep in sync for no benefit."""
    data = _config("google/gemini-3-flash-preview")
    oc_model.sync_provider_models_from_catalog(data)

    assert "baseUrl" not in data["models"]["providers"]["google"]


def test_xai_gets_both_api_and_baseurl():
    """xai needs baseUrl too: with the right ``api`` but no baseUrl the
    request still goes to api.openai.com."""
    data = _config("xai/grok-4")
    oc_model.sync_provider_models_from_catalog(data)

    xai = data["models"]["providers"]["xai"]
    assert xai["api"] == "openai-responses"
    assert xai["baseUrl"] == "https://api.x.ai/v1"


def test_anthropic_is_not_touched():
    """anthropic already resolves anthropic-messages → api.anthropic.com
    (verified 200). Absence from the table must mean 'leave OC alone'."""
    data = _config("anthropic/claude-sonnet-4-6")
    oc_model.sync_provider_models_from_catalog(data)

    block = data["models"]["providers"]["anthropic"]
    assert "api" not in block
    assert "baseUrl" not in block


def test_openai_is_not_touched():
    """openai routes through the codex app-server rather than the HTTP
    transport — a successful run emits no model-fetch line at all. Pinning
    api/baseUrl risks breaking that routing, and the generic default is
    already correct for it."""
    data = _config("openai/gpt-5.5")
    oc_model.sync_provider_models_from_catalog(data)

    block = data["models"]["providers"]["openai"]
    assert "api" not in block
    assert "baseUrl" not in block


def test_operator_baseurl_is_never_overridden():
    """Self-hosted / region-pinned deployments set their own endpoint. We
    are correcting an ABSENT field, never overriding a chosen one."""
    data = _config("xai/grok-4")
    data["models"]["providers"]["xai"]["baseUrl"] = "https://self-hosted.internal/v1"
    oc_model.sync_provider_models_from_catalog(data)

    xai = data["models"]["providers"]["xai"]
    assert xai["baseUrl"] == "https://self-hosted.internal/v1"
    assert xai["api"] == "openai-responses"  # absent field still backfilled


def test_operator_api_is_never_overridden():
    data = _config("google/gemini-3-flash-preview")
    data["models"]["providers"]["google"]["api"] = "google-gemini-cli"
    oc_model.sync_provider_models_from_catalog(data)

    assert data["models"]["providers"]["google"]["api"] == "google-gemini-cli"


def test_blank_api_is_treated_as_absent():
    """An empty string is not a chosen value — it is a broken one, and OC
    would fall through to openai-responses on it just the same."""
    data = _config("google/gemini-3-flash-preview")
    data["models"]["providers"]["google"]["api"] = "   "
    oc_model.sync_provider_models_from_catalog(data)

    assert data["models"]["providers"]["google"]["api"] == "google-generative-ai"


def test_preexisting_block_self_heals_without_migration():
    """Bots registered by an earlier Evolve version must repair on their
    next deploy — the transport pass runs over every provider block, not
    only ones created during this call."""
    data = _config("anthropic/claude-sonnet-4-6")
    # google block already present from an older deploy, model already
    # registered, so the add-missing loop short-circuits on it.
    data["models"]["providers"]["google"] = {
        "models": [{"id": "gemini-2.5-pro", "name": "gemini-2.5-pro"}]
    }
    data["agents"]["defaults"]["models"]["google/gemini-2.5-pro"] = {}
    oc_model.sync_provider_models_from_catalog(data)

    assert data["models"]["providers"]["google"]["api"] == "google-generative-ai"


def test_unknown_provider_is_left_alone():
    """Absence from the table is the fail-safe direction: no guess."""
    data = {
        "agents": {"defaults": {"models": {"ollama/llama3": {}}}},
        "models": {"providers": {"ollama": {"models": [
            {"id": "llama3", "name": "llama3"}]}}},
    }
    oc_model.sync_provider_models_from_catalog(data)

    assert "api" not in data["models"]["providers"]["ollama"]


def test_non_dict_provider_block_does_not_crash():
    """This runs on every deploy for every bot — an operator-set scalar
    must not raise."""
    data = _config("google/gemini-3-flash-preview")
    data["models"]["providers"]["weird"] = "not-a-dict"
    oc_model.sync_provider_models_from_catalog(data)

    assert data["models"]["providers"]["google"]["api"] == "google-generative-ai"
    assert data["models"]["providers"]["weird"] == "not-a-dict"


def test_idempotent_across_repeated_deploys():
    data = _config("google/gemini-3-flash-preview", "xai/grok-4")
    oc_model.sync_provider_models_from_catalog(data)
    first = {k: dict(v) for k, v in data["models"]["providers"].items()}
    oc_model.sync_provider_models_from_catalog(data)

    assert {k: dict(v) for k, v in data["models"]["providers"].items()} == first


def test_every_table_entry_declares_an_api():
    """A baseUrl-only entry would leave the openai-responses default in
    place — the exact bug — while looking like it had been handled."""
    for provider, transport in oc_model._OC_PROVIDER_TRANSPORT.items():
        assert transport.get("api"), f"{provider} has no api"


def test_table_providers_are_all_oc_bundled():
    """A non-bundled provider never reaches the transport pass through the
    registration loop, so an entry for one would be dead config."""
    for provider in oc_model._OC_PROVIDER_TRANSPORT:
        assert provider in oc_model._OC_BUNDLED_PROVIDERS
