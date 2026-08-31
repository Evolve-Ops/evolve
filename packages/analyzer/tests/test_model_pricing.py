"""tests/test_model_pricing.py — normalized pricing catalog (Addendum 8 §B).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §"Addendum 8 §B".

The pricing data layer: normalize LiteLLM ($/token, primary) + models.dev
($/M, cross-check + family) into a common per-model record, cache it, read it
back. Covers the required matrix:
  - dedup-by-``litellm_provider`` (native row kept, bedrock/vertex re-host skipped)
  - $/token (LiteLLM) vs $/M (models.dev ÷1e6) normalization
  - ``family`` enrichment of a LiteLLM row from the models.dev cross-check
  - the reader lookup by (provider, model_id)
  - graceful degrade when a source fetch raises (other source still contributes;
    both-fail keeps a stale cache and writes nothing new)

ALL data is fixtures — no network. ``fetch_pricing_catalog`` is exercised with
an injected fetcher (stub / raising), so CI makes zero live pricing calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_pricing as mp  # noqa: E402


# ── Trimmed fixtures (a few rows each, shaped like the real catalogs) ──────────

# LiteLLM: FLAT dict keyed by model id; pricing in $/token; ``litellm_provider``
# spells the serving surface. The same logical model appears once per surface —
# here the native anthropic row AND a bedrock re-host of the same model, plus a
# ``sample_spec`` sentinel and an unpriced row that must both be skipped.
LITELLM_FIXTURE: dict = {
    "sample_spec": {
        "litellm_provider": "openai",
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "notes": "sentinel row — present in the real file, must be ignored",
    },
    "claude-opus-4-8": {
        "litellm_provider": "anthropic",
        "input_cost_per_token": 0.000015,
        "output_cost_per_token": 0.000075,
        "max_input_tokens": 200000,
    },
    "anthropic.claude-opus-4-8-v1:0": {
        # Bedrock RE-HOST of the same logical model — must be skipped so the
        # model is priced once (dedup-by-litellm_provider).
        "litellm_provider": "bedrock",
        "input_cost_per_token": 0.000018,
        "output_cost_per_token": 0.00009,
    },
    "gpt-7": {
        "litellm_provider": "openai",
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
        "max_tokens": 128000,
    },
    "embed-only-model": {
        # No pricing at all → nothing to contribute to the cost layer; skipped.
        "litellm_provider": "openai",
    },
}

# models.dev: keyed by PROVIDER, each provider carrying a ``models`` map; pricing
# in $/M tokens; uniquely carries ``family`` + ``release_date``. Here it prices
# the SAME opus model (cross-check, and carries the family LiteLLM lacks) plus a
# model LiteLLM does NOT price (union coverage).
MODELSDEV_FIXTURE: dict = {
    "anthropic": {
        "models": {
            "claude-opus-4-8": {
                "cost": {"input": 15.0, "output": 75.0},  # $/M → /1e6 = $/token
                "limit": {"context": 200000, "output": 64000},
                "family": "claude-opus",
                "release_date": "2026-05-01",
            },
            "claude-sonnet-4-6": {
                # LiteLLM does NOT price this in the fixture — union adds it.
                "cost": {"input": 3.0, "output": 15.0},
                "limit": {"context": 200000},
                "family": "claude-sonnet",
                "release_date": "2026-04-01",
            },
        },
    },
}


# ── normalize_litellm: dedup + $/token + sentinel/unpriced skipping ───────────

def test_normalize_litellm_dedups_by_provider_and_skips_rehosts():
    records = mp.normalize_litellm(LITELLM_FIXTURE)
    by_key = {(r.provider, r.model_id): r for r in records}
    # Native anthropic opus kept; bedrock re-host of the same model dropped.
    assert ("anthropic", "claude-opus-4-8") in by_key
    assert all(r.source == "litellm" for r in records)
    # The bedrock surface contributes nothing (re-host skip) — only one opus row.
    opus_rows = [r for r in records if r.model_id == "claude-opus-4-8"]
    assert len(opus_rows) == 1
    assert opus_rows[0].provider == "anthropic"
    # The sentinel ``sample_spec`` and the unpriced ``embed-only-model`` are gone.
    assert "sample_spec" not in {r.model_id for r in records}
    assert "embed-only-model" not in {r.model_id for r in records}


def test_normalize_litellm_keeps_per_token_pricing_as_is():
    """LiteLLM pricing is ALREADY $/token — passed through unchanged (no ÷1e6)."""
    records = mp.normalize_litellm(LITELLM_FIXTURE)
    opus = next(r for r in records if r.model_id == "claude-opus-4-8")
    assert opus.input_cost_per_token == 0.000015
    assert opus.output_cost_per_token == 0.000075
    assert opus.context_window == 200000
    # LiteLLM carries no family.
    assert opus.family is None


# ── normalize_modelsdev: $/M → $/token + family ───────────────────────────────

def test_normalize_modelsdev_divides_per_million_to_per_token():
    records = mp.normalize_modelsdev(MODELSDEV_FIXTURE)
    opus = next(r for r in records if r.model_id == "claude-opus-4-8")
    # $/M ÷ 1e6 = $/token: 15.0 / 1e6 == 1.5e-5.
    assert opus.input_cost_per_token == 15.0 / 1e6
    assert opus.output_cost_per_token == 75.0 / 1e6
    assert opus.context_window == 200000
    assert opus.family == "claude-opus"
    assert opus.source == "models.dev"


# ── build_pricing_cache: family enrichment + union coverage ───────────────────

def test_build_cache_enriches_litellm_family_from_modelsdev():
    """A LiteLLM row (no family) borrows the family from the models.dev row for
    the same (provider, model) — the cross-check's unique contribution. Pricing
    still comes from LiteLLM (primary)."""
    doc = mp.build_pricing_cache(
        litellm_raw=LITELLM_FIXTURE,
        modelsdev_raw=MODELSDEV_FIXTURE,
        refreshed_at="2026-06-11T00:00:00Z",
    )
    by_key = {(m["provider"], m["model_id"]): m for m in doc["models"]}
    opus = by_key[("anthropic", "claude-opus-4-8")]
    # Family enriched from models.dev...
    assert opus["family"] == "claude-opus"
    # ...but pricing is LiteLLM's (primary), the $/token value, NOT the bedrock
    # re-host's higher number.
    assert opus["source"] == "litellm"
    assert opus["input_cost_per_token"] == 0.000015


def test_build_cache_unions_modelsdev_only_models():
    """A model models.dev prices but LiteLLM does not is still in the catalog —
    coverage is the union of both sources."""
    doc = mp.build_pricing_cache(
        litellm_raw=LITELLM_FIXTURE,
        modelsdev_raw=MODELSDEV_FIXTURE,
        refreshed_at="2026-06-11T00:00:00Z",
    )
    by_key = {(m["provider"], m["model_id"]): m for m in doc["models"]}
    sonnet = by_key[("anthropic", "claude-sonnet-4-6")]
    assert sonnet["source"] == "models.dev"
    assert sonnet["input_cost_per_token"] == 3.0 / 1e6
    assert sonnet["family"] == "claude-sonnet"
    # The bedrock re-host id never made it into the catalog under any provider.
    ids = {m["model_id"] for m in doc["models"]}
    assert "anthropic.claude-opus-4-8-v1:0" not in ids


# ── Reader: lookup by (provider, model_id) ────────────────────────────────────

def test_write_read_and_lookup_roundtrip(tmp_path):
    doc = mp.build_pricing_cache(
        litellm_raw=LITELLM_FIXTURE,
        modelsdev_raw=MODELSDEV_FIXTURE,
        refreshed_at="2026-06-11T00:00:00Z",
    )
    path = mp.write_pricing_cache(tmp_path, doc)
    assert path == tmp_path / "model-pricing.json"
    # Atomic write leaves no temp debris.
    assert list(tmp_path.glob("model-pricing.json.tmp*")) == []

    cache = mp.read_pricing_cache(tmp_path)
    assert cache is not None and cache["refreshed_at"] == "2026-06-11T00:00:00Z"

    # Bare id lookup.
    rec = mp.lookup_price(cache, "anthropic", "claude-opus-4-8")
    assert rec is not None
    assert rec["input_cost_per_token"] == 0.000015
    # Qualified id lookup resolves to the same record.
    rec_q = mp.lookup_price(cache, "anthropic", "anthropic/claude-opus-4-8")
    assert rec_q == rec
    # A model absent from the catalog → None (no fabricated price).
    assert mp.lookup_price(cache, "anthropic", "claude-nonexistent-9") is None
    # None cache (no catalog yet) → None, not a crash.
    assert mp.lookup_price(None, "anthropic", "claude-opus-4-8") is None


def test_read_pricing_cache_missing_returns_none(tmp_path):
    assert mp.read_pricing_cache(tmp_path) is None


def test_read_pricing_cache_corrupt_returns_none(tmp_path):
    (tmp_path / "model-pricing.json").write_text("{not json")
    assert mp.read_pricing_cache(tmp_path) is None


# ── Graceful degrade: a source fetch that raises ──────────────────────────────

def _fetcher(mapping: dict, *, fail: set[str] | None = None):
    """Build an injectable fetcher over fixtures — no network. ``fail`` URLs
    raise to exercise the degrade path."""
    fail = fail or set()

    def _get(url: str) -> dict:
        if url in fail:
            raise RuntimeError(f"simulated fetch failure for {url}")
        return mapping[url]

    return _get


def test_fetch_degrades_when_one_source_raises():
    """LiteLLM fetch raises → recorded degraded WITH a reason; models.dev still
    contributes its models (never a silent 'fresh' on a partial fetch)."""
    fetcher = _fetcher(
        {
            mp.LITELLM_PRICING_URL: LITELLM_FIXTURE,
            mp.MODELSDEV_API_URL: MODELSDEV_FIXTURE,
        },
        fail={mp.LITELLM_PRICING_URL},
    )
    doc = mp.fetch_pricing_catalog(
        refreshed_at="2026-06-11T00:00:00Z", fetcher=fetcher,
    )
    degraded = {d["source"]: d["reason"] for d in doc["degraded"]}
    assert "litellm" in degraded
    assert "simulated fetch failure" in degraded["litellm"]
    # models.dev models are present despite the LiteLLM failure.
    ids = {(m["provider"], m["model_id"]) for m in doc["models"]}
    assert ("anthropic", "claude-opus-4-8") in ids
    # The surviving opus row is models.dev-sourced (LiteLLM contributed nothing).
    opus = next(
        m for m in doc["models"]
        if (m["provider"], m["model_id"]) == ("anthropic", "claude-opus-4-8")
    )
    assert opus["source"] == "models.dev"


def test_refresh_keeps_stale_cache_when_both_sources_fail(tmp_path):
    """Both fetches raise → no fresh models. A pre-existing cache is KEPT (a
    degraded fetch never blows away good data); refresh writes nothing new."""
    # Seed a good cache.
    good = mp.build_pricing_cache(
        litellm_raw=LITELLM_FIXTURE,
        modelsdev_raw=MODELSDEV_FIXTURE,
        refreshed_at="2026-06-10T00:00:00Z",
    )
    mp.write_pricing_cache(tmp_path, good)
    before = json.loads((tmp_path / "model-pricing.json").read_text())

    fetcher = _fetcher(
        {mp.LITELLM_PRICING_URL: {}, mp.MODELSDEV_API_URL: {}},
        fail={mp.LITELLM_PRICING_URL, mp.MODELSDEV_API_URL},
    )
    result = mp.refresh_pricing_cache(
        tmp_path, refreshed_at="2026-06-11T00:00:00Z", fetcher=fetcher,
    )
    assert result is None  # nothing written
    after = json.loads((tmp_path / "model-pricing.json").read_text())
    # The stale-but-good cache is untouched.
    assert after == before
    assert after["refreshed_at"] == "2026-06-10T00:00:00Z"


def test_refresh_writes_when_a_source_succeeds(tmp_path):
    """A normal refresh (both sources OK) writes the cache and returns its path."""
    fetcher = _fetcher({
        mp.LITELLM_PRICING_URL: LITELLM_FIXTURE,
        mp.MODELSDEV_API_URL: MODELSDEV_FIXTURE,
    })
    result = mp.refresh_pricing_cache(
        tmp_path, refreshed_at="2026-06-11T00:00:00Z", fetcher=fetcher,
    )
    assert result == tmp_path / "model-pricing.json"
    cache = mp.read_pricing_cache(tmp_path)
    assert cache["refreshed_at"] == "2026-06-11T00:00:00Z"
    assert cache["degraded"] == []
