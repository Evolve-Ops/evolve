"""tests/test_default_catalog_real_ids.py — guard against fictional model IDs.

Regression guard for the 10d fast-follow (PR #2711 shipped non-existent OpenAI
IDs ``gpt-5.5`` / ``gpt-5.4-mini``; OpenAI's live ``/v1/models`` listing is only
``{gpt-4o, gpt-4.1, gpt-4.1-mini}``). On an OpenAI-primary pod (or via easy-setup)
those resolve to a model OpenClaw silently drops, so the pull fails.

This test cross-checks the code-shipped defaults — ``DEFAULT_MODEL_CATALOG``
(the blessed ladder) and ``model_registry.RECOMMENDED`` (the offline fallback) —
against a SMALL checked-in reference of REAL, live-verified provider model IDs.
For every provider that is real/credentialed-class (anthropic, openai, google),
every model named in the defaults for that provider must appear in the reference
set. A future hand-typed fictional ID for a real provider fails CI here.

xAI is exempt: it is dormant/uncredentialed in this fleet (no alias surface in
OC, no live verification path), so its catalog entries are not pinned to a live
listing.

This is DATA, not logic — the reference set below is a checked-in fixture, the
same shape as the catalog itself (three-homes rule: catalog data + test fixture
are allowed homes for model literals; logic stays literal-free).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import pytest  # noqa: E402

from model_registry import RECOMMENDED  # noqa: E402
from primary_bot import DEFAULT_MODEL_CATALOG  # noqa: E402

# ── Reference: REAL, live-verified model IDs per provider ────────────────────
#
# Update this set ONLY against a provider's live model listing (e.g. OpenAI's
# ``/v1/models``, Anthropic's model list). It is the source of truth a fictional
# hand-typed ID is checked against. Keep it minimal — only the IDs the blessed
# ladder + offline fallback are allowed to name.
#
# provider-literal-allow-begin: reference DATA (test fixture, three-homes rule)
REAL_MODEL_IDS: dict[str, set[str]] = {
    "anthropic": {
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "claude-fable-5",
        # Real but older than the blessed ladder: the offline RECOMMENDED
        # anthropic tier1 fallback still names opus-4-7 (its OC retirement
        # edge upgrades 4-7 -> current). Listed so the guard accepts the
        # real historical ID without forcing a RECOMMENDED bump in this fix.
        "claude-opus-4-7",
    },
    "openai": {
        "gpt-4o",
        "gpt-4.1",
        "gpt-4.1-mini",
    },
    "google": {
        "gemini-2.5-pro",
        "gemini-2.0-flash",
    },
}

# Providers NOT pinned to a live listing (dormant / uncredentialed in-fleet).
EXEMPT_PROVIDERS: frozenset[str] = frozenset({"xai"})
# provider-literal-allow-end


def _split(qualified: str) -> tuple[str, str]:
    """``"openai/gpt-4.1"`` -> ``("openai", "gpt-4.1")``."""
    provider, _, model = qualified.partition("/")
    return provider, model


def _catalog_model_ids() -> list[str]:
    """Every ``provider/model`` string in the default ladder's rung clusters."""
    out: list[str] = []
    for rung in DEFAULT_MODEL_CATALOG["rungs"]:
        out.extend(rung.get("models", []))
    return out


def _recommended_model_ids() -> list[str]:
    """Every ``provider/model`` string in the offline RECOMMENDED fallback."""
    out: list[str] = []
    for tiers in RECOMMENDED.values():
        for cell in tiers.values():
            out.append(cell["model"])
    return out


@pytest.mark.parametrize("qualified", _catalog_model_ids())
def test_default_catalog_names_only_real_models(qualified: str) -> None:
    """Every real-provider entry in DEFAULT_MODEL_CATALOG is a live model ID."""
    provider, model = _split(qualified)
    if provider in EXEMPT_PROVIDERS:
        pytest.skip(f"{provider} is dormant/uncredentialed — not live-pinned")
    assert provider in REAL_MODEL_IDS, (
        f"{qualified!r}: provider {provider!r} is not in the real-ID reference "
        f"and not exempt — add it to REAL_MODEL_IDS (with live-listing proof) "
        f"or EXEMPT_PROVIDERS"
    )
    assert model in REAL_MODEL_IDS[provider], (
        f"{qualified!r} is not a real {provider} model. Live listing has "
        f"{sorted(REAL_MODEL_IDS[provider])}. A fictional/typo'd ID in the "
        f"default catalog resolves to a model OpenClaw silently drops -> the "
        f"pull fails on a {provider}-primary pod."
    )


@pytest.mark.parametrize("qualified", _recommended_model_ids())
def test_recommended_fallback_names_only_real_models(qualified: str) -> None:
    """Every real-provider entry in RECOMMENDED is a live model ID."""
    provider, model = _split(qualified)
    if provider in EXEMPT_PROVIDERS:
        pytest.skip(f"{provider} is dormant/uncredentialed — not live-pinned")
    assert provider in REAL_MODEL_IDS, (
        f"{qualified!r}: provider {provider!r} is not in the real-ID reference "
        f"and not exempt — add it to REAL_MODEL_IDS or EXEMPT_PROVIDERS"
    )
    assert model in REAL_MODEL_IDS[provider], (
        f"{qualified!r} is not a real {provider} model. Live listing has "
        f"{sorted(REAL_MODEL_IDS[provider])}."
    )


def test_guard_covers_every_real_catalog_provider() -> None:
    """Sanity: every non-exempt provider that appears in the catalog is in the
    reference set — so the guard can never silently pass by simply omitting a
    provider from REAL_MODEL_IDS."""
    catalog_providers = {_split(q)[0] for q in _catalog_model_ids()}
    real_providers = catalog_providers - EXEMPT_PROVIDERS
    missing = real_providers - set(REAL_MODEL_IDS)
    assert not missing, (
        f"catalog providers {sorted(missing)} have no real-ID reference and are "
        f"not exempt — add them to REAL_MODEL_IDS or EXEMPT_PROVIDERS"
    )
