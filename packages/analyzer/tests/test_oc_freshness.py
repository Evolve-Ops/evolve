"""tests/test_oc_freshness.py — OC-driven freshness path tests.

Covers the introspection + resolver + check_bot_freshness pipeline that
replaced the static-RECOMMENDED-only path. Three layers:

  - Parsers (oc_introspection): hand-rolled JS fixtures exercise the
    regex/state-machine extractors for alias map, default constants,
    if-chain retirement (openai shape), switch retirement (xai shape),
    and Claude prefix-list retirement (anthropic shape).

  - Resolver (tier_resolver): synthesized OcSignals fixtures verify
    each strategy in TIER_POLICY produces the right model id and the
    right ``source`` provenance tag.

  - End-to-end (model_registry.check_bot_freshness): the retirement
    advisory fires when a bot's tier references a name OC has
    retired — that's the loop-stopping fix the structural rework
    exists for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from oc_introspection import (  # noqa: E402
    OcSignals,
    _extract_anthropic_literal_retirements,
    _extract_function_body,
    _parse_if_chain_retirements,
    _parse_switch_retirements,
    load_alias_map,
    load_default_constants,
    load_retirement_map,
    reset_cache,
)
from tier_resolver import (  # noqa: E402
    TIER_POLICY,
    covered_tiers,
    is_retired,
    resolve_tier_model,
)
from model_registry import (  # noqa: E402
    ModelAdvisory,
    check_bot_freshness,
    resolve_current_model,
)


# ── Fixtures: synthesized OC dist files ─────────────────────────────────────
#
# We don't depend on a real OC install. Each fixture writes a minimal JS
# file that mimics the structural shape of the real OC dist file the
# parser targets — just enough surface for the regex/state-machine to
# do its job.


# Mirrors the real DEFAULT_MODEL_ALIASES block in io-AmXZn-TT.js.
_FIXTURE_IO_JS = """
// Some unrelated header content.
const DEFAULT_MODEL_ALIASES = {
    opus: "anthropic/claude-opus-4-8",
    sonnet: "anthropic/claude-sonnet-4-6",
    gpt: "openai/gpt-5.4",
    "gpt-mini": "openai/gpt-5.4-mini",
    "gpt-nano": "openai/gpt-5.4-nano",
    gemini: "google/gemini-3.1-pro-preview",
    "gemini-flash": "google/gemini-3-flash-preview"
};
// Trailing junk.
const NOT_THE_THING = {};
"""


# Mirrors default-models-D2HKqH8G.js: constants per provider family.
_FIXTURE_DEFAULTS_JS = """
const OPENAI_DEFAULT_MODEL = "openai/gpt-5.5";
const OPENAI_CODEX_DEFAULT_MODEL = OPENAI_DEFAULT_MODEL;
const OPENAI_DEFAULT_IMAGE_MODEL = "gpt-image-2";
const OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small";
const GOOGLE_GEMINI_DEFAULT_MODEL = "google/gemini-3.1-pro-preview";
"""


# Mirrors legacy-config-migrations-o81PpUkj.js: three retirement
# function shapes, all in one file. Use the same function names the
# parsers grep for.
_FIXTURE_MIGRATIONS_JS = """
function upgradeRetiredOpenAiModelId(model, provider) {
    const normalized = normalizeString(model);
    if (normalized === "gpt-4o-mini") return "gpt-5.4-mini";
    if (normalized === "gpt-4" || normalized === "gpt-4o") return "gpt-5.5";
    return null;
}
function upgradeRetiredXaiModelId(model) {
    switch (normalizeString(model)) {
        case "grok-code-fast":
        case "grok-code-fast-1": return "grok-build-0.1";
        case "grok-4-fast-reasoning": return "grok-4.3";
        default: return null;
    }
}
function upgradeOldClaudeToken(token, separator, provider) {
    const normalized = normalizeString(token);
    if (!normalized) return null;
    const opusTarget = claudeTargetModelId("opus", separator, provider);
    const sonnetTarget = claudeTargetModelId("sonnet", separator, provider);
    if (normalized.startsWith("claude-opus-4-7") || normalized.startsWith("claude-opus-4.7")) return null;
    if (normalized.startsWith("claude-haiku-4-5")) return null;
    if (normalized === "claude-opus-4" || hasAnyRetiredVersionPrefix(normalized, [
        "claude-opus-4-5",
        "claude-opus-4.5",
        "claude-opus-4-1"
    ])) return opusTarget;
    if (normalized === "claude-sonnet-4" || hasAnyRetiredVersionPrefix(normalized, [
        "claude-sonnet-4-5",
        "claude-sonnet-4.5"
    ])) return sonnetTarget;
    return null;
}
"""


@pytest.fixture
def oc_dist(tmp_path: Path) -> Path:
    """Write the three fixture JS files into a temporary dist directory."""
    dist = tmp_path / "openclaw" / "dist"
    dist.mkdir(parents=True)
    (dist / "io-FIXTURE.js").write_text(_FIXTURE_IO_JS)
    (dist / "default-models-FIXTURE.js").write_text(_FIXTURE_DEFAULTS_JS)
    (dist / "legacy-config-migrations-FIXTURE.js").write_text(_FIXTURE_MIGRATIONS_JS)
    # package.json one level up so detect_oc_version finds something.
    (tmp_path / "openclaw" / "package.json").write_text('{"version": "fixture-1.0.0"}')
    return dist


# ── Introspection: alias map ────────────────────────────────────────────────


def test_load_alias_map_extracts_all_entries(oc_dist: Path):
    aliases = load_alias_map(oc_dist)
    assert aliases["opus"] == "anthropic/claude-opus-4-8"
    assert aliases["sonnet"] == "anthropic/claude-sonnet-4-6"
    assert aliases["gpt-mini"] == "openai/gpt-5.4-mini"
    assert aliases["gemini-flash"] == "google/gemini-3-flash-preview"
    assert len(aliases) == 7  # opus, sonnet, gpt, gpt-mini, gpt-nano, gemini, gemini-flash


def test_load_alias_map_returns_empty_when_no_match(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "io-NOMATCH.js").write_text("// no aliases here")
    assert load_alias_map(dist) == {}


# ── Introspection: default constants ────────────────────────────────────────


def test_load_default_constants_extracts_per_provider(oc_dist: Path):
    defaults = load_default_constants(oc_dist)
    assert defaults["openai"] == "openai/gpt-5.5"
    # GOOGLE_GEMINI_DEFAULT_MODEL maps to "google" via the prefix table.
    assert defaults["google"] == "google/gemini-3.1-pro-preview"


def test_load_default_constants_skips_non_llm_constants(oc_dist: Path):
    """Image/TTS/embedding constants share the naming pattern but use
    bare model names without provider prefix. The parser must skip them
    so they don't pollute the LLM resolution layer."""
    defaults = load_default_constants(oc_dist)
    # Even though _FIXTURE_DEFAULTS_JS has OPENAI_DEFAULT_IMAGE_MODEL
    # and OPENAI_DEFAULT_EMBEDDING_MODEL, those values lack the
    # "openai/" prefix so the parser filters them out — only the
    # provider-qualified ``openai/gpt-5.5`` survives.
    assert defaults["openai"] == "openai/gpt-5.5"


# ── Introspection: retirement parsing ──────────────────────────────────────


def test_if_chain_retirement_parser_pairs_conditions_to_returns():
    body = '''
        if (normalized === "gpt-4o-mini") return "gpt-5.4-mini";
        if (normalized === "gpt-4" || normalized === "gpt-4o") return "gpt-5.5";
    '''
    out = _parse_if_chain_retirements(body)
    assert out == {
        "gpt-4o-mini": "gpt-5.4-mini",
        "gpt-4": "gpt-5.5",
        "gpt-4o": "gpt-5.5",
    }


def test_switch_retirement_parser_handles_fallthrough_cases():
    body = '''
        switch (normalizeString(model)) {
            case "a":
            case "b": return "x";
            case "c": return "y";
        }
    '''
    out = _parse_switch_retirements(body)
    assert out == {"a": "x", "b": "x", "c": "y"}


def test_anthropic_parser_separates_keep_list_from_retirement_list():
    """OC's upgradeOldClaudeToken has BOTH ``startsWith("claude-opus-4-7")
    return null`` (keep) AND ``"claude-opus-4-5" return opusTarget``
    (retire). The state machine should partition them correctly."""
    out = _extract_anthropic_literal_retirements(_FIXTURE_MIGRATIONS_JS)
    # 4-7 / 4.7 are current (return null) — must NOT appear in the map
    assert "claude-opus-4-7" not in out
    assert "claude-opus-4.7" not in out
    # haiku-4-5 is current (return null) — must NOT appear
    assert "claude-haiku-4-5" not in out
    # 4-5 / 4.5 / 4-1 are retired (return opusTarget) — must appear
    assert out["claude-opus-4-5"] == "__alias:opus__"
    assert out["claude-opus-4.5"] == "__alias:opus__"
    assert out["claude-opus-4-1"] == "__alias:opus__"
    # sonnet retirements go to sonnet alias
    assert out["claude-sonnet-4-5"] == "__alias:sonnet__"


def test_load_retirement_map_covers_all_three_providers(oc_dist: Path):
    rmap = load_retirement_map(oc_dist)
    assert "openai" in rmap
    assert "xai" in rmap
    assert "anthropic" in rmap
    assert rmap["openai"]["gpt-4o"] == "gpt-5.5"
    assert rmap["xai"]["grok-code-fast"] == "grok-build-0.1"
    assert rmap["anthropic"]["claude-opus-4-5"] == "__alias:opus__"


# ── Resolver ───────────────────────────────────────────────────────────────


def _signals_from_fixture() -> OcSignals:
    """Build a fixture-only OcSignals object without touching the filesystem.

    Tests that exercise the resolver in isolation use this so they
    don't depend on the introspection layer at all.
    """
    return OcSignals(
        aliases={
            "opus": "anthropic/claude-opus-4-8",
            "sonnet": "anthropic/claude-sonnet-4-6",
            "gpt-mini": "openai/gpt-5.4-mini",
            "gemini": "google/gemini-3.1-pro-preview",
            "gemini-flash": "google/gemini-3-flash-preview",
        },
        defaults={"openai": "openai/gpt-5.5"},
        retirement={
            "openai": {"gpt-4o": "gpt-5.5", "gpt-4o-mini": "gpt-5.4-mini"},
            "anthropic": {"claude-opus-4-5": "__alias:opus__"},
        },
        oc_version="fixture-1.0",
        source_dir="/fixture",
    )


def test_resolver_default_constant_strategy_for_openai_workhorse():
    sig = _signals_from_fixture()
    r = resolve_tier_model("openai", "tier2", signals=sig)
    assert r is not None
    assert r.model_id == "openai/gpt-5.5"
    assert r.source == "default_constant"
    assert r.oc_version == "fixture-1.0"


def test_resolver_alias_strategy_for_anthropic_tier1():
    sig = _signals_from_fixture()
    r = resolve_tier_model("anthropic", "tier1", signals=sig)
    assert r is not None
    assert r.model_id == "anthropic/claude-opus-4-8"
    assert r.source == "alias"


def test_resolver_returns_none_when_signals_empty():
    """Empty signals → no resolution → caller falls back to static."""
    empty = OcSignals()
    assert resolve_tier_model("openai", "tier2", signals=empty) is None


def test_resolver_returns_none_for_unrecognized_provider():
    sig = _signals_from_fixture()
    assert resolve_tier_model("nobody", "tier2", signals=sig) is None


def test_is_retired_strips_provider_prefix():
    sig = _signals_from_fixture()
    # Bare name
    retired, repl = is_retired("openai", "gpt-4o", sig)
    assert retired is True
    assert repl == "openai/gpt-5.5"
    # Provider-qualified name
    retired, repl = is_retired("openai", "openai/gpt-4o", sig)
    assert retired is True
    assert repl == "openai/gpt-5.5"


def test_is_retired_resolves_anthropic_alias_sentinel():
    """Anthropic retirements use ``__alias:opus__`` sentinels because
    the replacement comes from the alias map. The resolver must
    substitute the alias to a real model id before returning."""
    sig = _signals_from_fixture()
    retired, repl = is_retired("anthropic", "claude-opus-4-5", sig)
    assert retired is True
    assert repl == "anthropic/claude-opus-4-8"


def test_is_retired_returns_false_for_current_model():
    sig = _signals_from_fixture()
    retired, repl = is_retired("openai", "openai/gpt-5.5", sig)
    assert retired is False
    assert repl is None


def test_covered_tiers_returns_policy_tiers_in_order():
    """covered_tiers is the freshness check's enumeration entrypoint —
    if this drifts from TIER_POLICY, the freshness check stops asking
    about cells it should be asking about."""
    cells = covered_tiers("openai")
    assert cells == ["tier0", "tier1", "tier2", "tier3"]
    cells = covered_tiers("anthropic")
    assert cells == ["tier1", "tier2"]  # no tier0/tier3 in policy
    assert covered_tiers("nobody") == []


# ── End-to-end: check_bot_freshness ────────────────────────────────────────


def test_freshness_flags_retired_openai_model_as_retired(monkeypatch, oc_dist: Path):
    """The core loop-stopping behaviour. A bot whose tier explicitly
    names ``openai/gpt-4o`` must produce an ``is_retired=True`` advisory
    with OC's own replacement model. Without this, the catalog
    reconcile button + OC's silent migration enter the oscillation
    documented in the 2026-06-07 incident."""
    monkeypatch.setenv("OPENCLAW_DIST_DIR", str(oc_dist))
    reset_cache()
    bot_tiers = {
        "tier2": {"models": ["openai/gpt-4o"]},
        "tier3": {"models": ["openai/gpt-4o-mini"]},
    }
    advisories = check_bot_freshness("test-bot", bot_tiers, {"openai"})
    retired = [a for a in advisories if a.is_retired]
    assert len(retired) >= 2
    by_tier = {a.tier: a for a in retired}
    assert by_tier["tier2"].current_model == "openai/gpt-4o"
    assert by_tier["tier2"].recommended_model == "openai/gpt-5.5"
    assert by_tier["tier2"].source == "oc_retirement"
    assert by_tier["tier3"].current_model == "openai/gpt-4o-mini"
    assert by_tier["tier3"].recommended_model == "openai/gpt-5.4-mini"


def test_freshness_uses_oc_alias_when_available(monkeypatch, oc_dist: Path):
    """When OC has an alias for the cell, the recommendation tracks OC
    — not the static fallback."""
    monkeypatch.setenv("OPENCLAW_DIST_DIR", str(oc_dist))
    reset_cache()
    # Bot configured with an older Opus than OC's alias points at.
    bot_tiers = {"tier1": {"models": ["anthropic/claude-opus-4-6"]}}
    advisories = check_bot_freshness("test-bot", bot_tiers, {"anthropic"})
    tier1 = [a for a in advisories if a.tier == "tier1"]
    assert len(tier1) == 1
    assert tier1[0].recommended_model == "anthropic/claude-opus-4-8"
    assert tier1[0].source == "oc_alias"
    assert tier1[0].oc_version == "fixture-1.0.0"


def test_freshness_falls_back_to_static_when_no_oc_signals(monkeypatch):
    """No OC dist → static RECOMMENDED is the only source. Existing
    callers (CI, unit tests) keep working."""
    monkeypatch.setenv("OPENCLAW_DIST_DIR", "/genuinely-not-there")
    reset_cache()
    # xAI has no OC alias / default in v2026.6.1 — exercises the
    # static fallback path even when OC IS installed.
    advisories = check_bot_freshness(
        "test-bot",
        {"tier1": {"models": ["xai/grok-2"]}},
        {"xai"},
    )
    xai = [a for a in advisories if a.provider == "xai" and a.tier == "tier1"]
    assert len(xai) == 1
    assert xai[0].source == "static"
    assert xai[0].oc_version is None


def test_resolve_current_model_returns_oc_when_available(monkeypatch, oc_dist: Path):
    monkeypatch.setenv("OPENCLAW_DIST_DIR", str(oc_dist))
    reset_cache()
    model, source, oc_v, _ = resolve_current_model("openai", "tier2")
    assert model == "openai/gpt-5.5"
    assert source == "oc_default"
    assert oc_v == "fixture-1.0.0"


def test_resolve_current_model_falls_back_to_static_when_oc_silent(monkeypatch):
    monkeypatch.setenv("OPENCLAW_DIST_DIR", "/nowhere")
    reset_cache()
    # anthropic tier3 has no OC alias entry; static fallback fills in.
    model, source, oc_v, _ = resolve_current_model("anthropic", "tier3")
    assert source == "static"
    assert oc_v is None
    assert model.startswith("anthropic/")
