"""tests/test_setup_wizard_embedding.py — Phase 3 wizard embedding wiring.

Covers the install-time hooks that flow embedding-provider defaults through
to the evolve user's openclaw.json, exercising:

  - _embedding_chain_for_credentials returns a usable chain for the
    typical anthropic-only first install (falls through to local).
  - Same helper produces a multi-provider chain when openai/google keys
    are present.
  - _evolve_openclaw_config writes agents.defaults.memorySearch with
    {provider, fallback} matching OpenClaw's runtime expectations.
  - The chain helper degrades gracefully when the analyzer module isn't
    importable (so the wizard never crashes on a path mishap).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin.setup_wizard import (  # noqa: E402
    _embedding_chain_for_credentials,
    _evolve_openclaw_config,
)


# ── _embedding_chain_for_credentials ─────────────────────────────────────────


def test_chain_anthropic_only_falls_through_to_local():
    """Most common first install: only anthropic key. anthropic has no embedding
    product, so the chain should be just ['local']."""
    chain = _embedding_chain_for_credentials({"anthropic"})
    assert chain == ["local"]


def test_chain_openai_only():
    chain = _embedding_chain_for_credentials({"openai"})
    assert "openai" in chain
    assert chain[-1] == "local"  # safety-net terminal


def test_chain_prefers_gemini_when_google_present():
    """A google credential resolves to the gemini embedding provider, and
    gemini is preferred over openai per DEFAULT_EMBEDDING_CHAIN order."""
    chain = _embedding_chain_for_credentials({"google", "openai"})
    assert chain.index("gemini") < chain.index("openai")
    assert chain[-1] == "local"


def test_chain_ignores_unknown_credentials():
    """Credentials for providers that don't do embeddings shouldn't appear."""
    chain = _embedding_chain_for_credentials({"xai"})
    assert "xai" not in chain
    assert chain == ["local"]


def test_chain_always_returns_at_least_local():
    """Empty credential set still gives ['local'] — never crashes a caller."""
    assert _embedding_chain_for_credentials(set()) == ["local"]


# ── _evolve_openclaw_config integration ─────────────────────────────────────


def _profile(provider: str, type_: str = "api_key") -> dict:
    return {f"{provider}:{type_}": {"type": type_, "provider": provider, "key": "k"}}


def test_openclaw_config_writes_memorysearch_block():
    """memorySearch must appear under agents.defaults with provider+fallback."""
    profiles = {**_profile("anthropic"), **_profile("openai"), **_profile("google")}
    cfg = _evolve_openclaw_config(
        network_id="testpod",
        shared_dir="/tmp/test",
        auth_profiles_flat=profiles,
    )
    ms = cfg["agents"]["defaults"]["memorySearch"]
    assert "provider" in ms
    # gemini preferred over openai → primary = gemini, fallback = openai
    assert ms["provider"] == "gemini"
    assert ms["fallback"] == "openai"


def test_openclaw_config_falls_back_to_local_when_no_remote_embeddings():
    """anthropic-only credentials → memorySearch points at local with no fallback."""
    profiles = _profile("anthropic")
    cfg = _evolve_openclaw_config(
        network_id="testpod",
        shared_dir="/tmp/test",
        auth_profiles_flat=profiles,
    )
    ms = cfg["agents"]["defaults"]["memorySearch"]
    assert ms == {"provider": "local"}


def test_openclaw_config_writes_provider_only_when_chain_is_one():
    """Single-entry chain → no fallback field (omit rather than dup the primary)."""
    cfg = _evolve_openclaw_config(
        network_id="testpod",
        shared_dir="/tmp/test",
        auth_profiles_flat=None,
    )
    ms = cfg["agents"]["defaults"]["memorySearch"]
    assert "provider" in ms
    assert "fallback" not in ms
