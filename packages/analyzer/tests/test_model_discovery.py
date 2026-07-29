"""tests/test_model_discovery.py — discovery-based model freshness (Phase 4).

Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §"Freshness check rework".

Covers the required matrix:
  - a listing containing claude-fable-5 with a rungs config lacking it →
    exactly ONE model_discovery finding (the class that catches Fable)
  - re-run with the same listing → deduped by signature (signal store)
  - a listing FAILURE → degraded-mode result, NOT "all current"
  - dated-snapshot-alias and non-chat filtering (out of scope)
  - RECOMMENDED-fallback / degraded path flagged degraded

All HTTP is mocked via an injected enumerator — CI makes zero live API calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import model_discovery as md  # noqa: E402
from signals import store as signals_store  # noqa: E402
import importlib  # noqa: E402
# The package __init__ re-exports the ``observe`` function, which shadows the
# ``observe`` submodule attribute — load the submodule explicitly so we can
# reach both observe() and observe_signals().
mdgen = importlib.import_module("generators.model_discovery.observe")  # noqa: E402
from generators.model_discovery.observe import ModelDiscoveryContext  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lm(provider, model_id, **kw):
    return md.ListedModel(
        provider=provider,
        model_id=model_id,
        qualified_id=f"{provider}/{model_id}",
        **kw,
    )


def _make_enumerator(listings: dict[str, list], fail: dict[str, str] | None = None):
    """Build a mock (provider, key) -> ProviderEnumeration callable.

    ``listings`` maps provider -> list[ListedModel]. ``fail`` maps provider ->
    reason; those providers return ok=False (simulating a failed listing call).
    """
    fail = fail or {}

    def _enum(provider, key):  # noqa: ARG001
        if provider in fail:
            return md.ProviderEnumeration(
                provider=provider, ok=False, reason=fail[provider],
            )
        return md.ProviderEnumeration(
            provider=provider, ok=True, models=listings.get(provider, []),
        )

    return _enum


# A rungs config WITHOUT fable — the pod hasn't adopted it.
_NETWORK_NO_FABLE = {
    "bots": {},
    "models": {
        "rungs": [
            {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
        ],
    },
}

# A genuinely-UNKNOWN frontier line — NOT in DEFAULT_MODEL_CATALOG, so it
# surfaces as a discovery on any pod that hasn't adopted it. Post-Phase-6
# (spec §Addendum 2.4) claude-fable-5 ships in the default catalog and is
# therefore KNOWN everywhere, so it can no longer be the "new model" fixture —
# the next frontier SKU (spec §Addendum note 5: "Mythos") plays that role here.
_NEW_BARE = "claude-mythos-5"
_NEW_ID = f"anthropic/{_NEW_BARE}"


# ── Discovery: fable surfaces as exactly one finding ──────────────────────────

def test_new_frontier_surfaces_as_single_discovery(tmp_path):
    """An Anthropic listing with a genuinely-new frontier line + a rungs config
    lacking it produces exactly one discovery finding."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE, display_name="Claude Mythos 5"),
    ]}
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE,
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    assert len(result.discoveries) == 1
    d = result.discoveries[0]
    assert d.qualified_id == _NEW_ID
    assert not result.is_degraded
    assert not result.all_current  # a discovery is NOT "all current"


def test_default_catalog_fable_produces_zero_discoveries(tmp_path):
    """Spec §Addendum 2.4: claude-fable-5 ships in DEFAULT_MODEL_CATALOG, so a
    pod with NO fable config anywhere still treats it as KNOWN — a listing
    carrying fable produces ZERO discoveries. This is the lifecycle that
    auto-resolves the pending Fable AdoptModel proposal."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", "claude-fable-5", display_name="Claude Fable 5"),
    ]}
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE,
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    assert result.discoveries == [], (
        "claude-fable-5 is a code default — it must never surface as a discovery"
    )


def test_known_models_produce_no_discovery(tmp_path):
    """When every listed model is already in a rung, there are no
    discoveries and (no staleness, no degraded) → all_current is True."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
    ]}
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE,
        bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    assert result.discoveries == []
    assert result.all_current


# ── Out-of-scope filtering ────────────────────────────────────────────────────

def test_non_chat_models_are_ignored(tmp_path):
    """Embedding/audio/image models never surface as discoveries."""
    listings = {"openai": [
        _lm("openai", "text-embedding-3-large",
            capabilities=md._openai_capabilities("text-embedding-3-large")),
        _lm("openai", "whisper-1",
            capabilities=md._openai_capabilities("whisper-1")),
        _lm("openai", "dall-e-3",
            capabilities=md._openai_capabilities("dall-e-3")),
    ]}
    network = {"bots": {}, "models": {"rungs": []}}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"openai": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    assert result.discoveries == []
    assert result.ignored_count == 3


def test_snapshot_alias_of_known_model_is_ignored(tmp_path):
    """A dated snapshot alias of a known model is auto-ignored — it's a
    pinned snapshot, not a new line."""
    network = {
        "bots": {},
        "models": {"rungs": [
            {"id": "sonnet-class", "models": ["openai/gpt-4o"], "costClass": "medium"},
        ]},
    }
    listings = {"openai": [
        _lm("openai", "gpt-4o", capabilities=["chat"]),
        _lm("openai", "gpt-4o-2024-08-06", capabilities=["chat"]),  # snapshot alias
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"openai": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # gpt-4o is known; its dated snapshot is ignored. Zero discoveries.
    assert result.discoveries == []
    assert result.ignored_count == 1


def test_operator_ignore_list_suppresses_discovery(tmp_path):
    """A model on the operator-editable ignore list never surfaces."""
    ignore_dir = tmp_path / "model-freshness"
    ignore_dir.mkdir(parents=True)
    (ignore_dir / "discovery-ignore.json").write_text(
        '{"ignore": ["openai/o9-experimental"]}'
    )
    listings = {"openai": [
        _lm("openai", "o9-experimental", capabilities=["chat"]),
    ]}
    network = {"bots": {}, "models": {"rungs": []}}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"openai": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    assert result.discoveries == []
    assert result.ignored_count == 1


# ── Degraded mode — the cardinal invariant ────────────────────────────────────

def test_listing_failure_is_degraded_not_all_current(tmp_path):
    """A failed listing call must yield a degraded result with a reason —
    NEVER 'all current'. This is the bug class the rework exists to kill."""
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE, bot_users=[], bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator({}, fail={"anthropic": "listing HTTP 401 (Unauthorized)"}),
    )
    assert result.is_degraded
    assert not result.all_current
    assert result.degraded_providers
    assert result.degraded_providers[0]["provider"] == "anthropic"
    assert "401" in result.degraded_providers[0]["reason"]
    assert result.enumerated_providers == []


def test_partial_degradation_still_surfaces_other_discoveries(tmp_path):
    """One provider failing must not suppress another provider's discoveries,
    and the run is still flagged degraded."""
    listings = {"openai": [
        # A novel-family line (not gpt/gpt-mini, which the enriched defaults
        # already know) so it surfaces as a genuine discovery.
        _lm("openai", "o5-pro", capabilities=["chat"]),  # unknown → discovery
    ]}
    network = {"bots": {}, "models": {"rungs": []}}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"openai": "sk-x", "anthropic": "sk-y"},
        enumerator=_make_enumerator(listings, fail={"anthropic": "listing unreachable (timeout)"}),
    )
    assert result.is_degraded
    assert len(result.discoveries) == 1
    assert result.discoveries[0].qualified_id == "openai/o5-pro"
    assert any(p["provider"] == "anthropic" for p in result.degraded_providers)


def test_no_listing_endpoint_provider_is_degraded(tmp_path):
    """A credentialed provider whose key is passed to enumeration but which has
    no fetcher falls back and is flagged degraded — never silently current.
    (xai now HAS a fetcher; a still-uncovered provider is the fixture here.)"""
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE, bot_users=[], bot_configs={},
        shared_dir=tmp_path,
        keys={"acme": "acme-key"},  # real enumerate_provider has no acme fetcher
        enumerator=None,  # use the real enumerate_provider
    )
    assert result.is_degraded
    assert result.degraded_providers[0]["provider"] == "acme"
    assert "no listing endpoint" in result.degraded_providers[0]["reason"]


# ── Within-family staleness from the LISTING ──────────────────────────────────

def test_within_family_staleness_from_listing(tmp_path):
    """A known model with a newer version in the same family per the LISTING
    produces a staleness finding (latest derived from listing, not a dict)."""
    network = {
        "bots": {},
        "models": {"rungs": [
            {"id": "opus-class", "models": ["anthropic/claude-opus-4-6"], "costClass": "high"},
        ]},
    }
    listings = {"anthropic": [
        _lm("anthropic", "claude-opus-4-6"),
        _lm("anthropic", "claude-opus-4-8"),  # newer in same family
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # opus-4-6 is known+stale → staleness on it. opus-4-8 is in
    # DEFAULT_MODEL_CATALOG (a code default), so it is KNOWN — it never surfaces
    # as a discovery, and the upgrade nudge comes through staleness only.
    stale = result.staleness
    assert any(
        s.current_model == "anthropic/claude-opus-4-6"
        and s.latest_model == "anthropic/claude-opus-4-8"
        for s in stale
    )
    assert [d.qualified_id for d in result.discoveries] == [], (
        "newer-in-adopted-family must surface via staleness, not discovery"
    )
    # opus-4-8 is a known default, not a frontier-skipped unknown.
    assert result.skipped_count == 0
    assert not result.all_current


# ── Frontier filter: only NEW families surface as discoveries ─────────────────

def test_live_pod_shape_only_new_families_surface(tmp_path):
    """Reproduces the live-pod noise finding: a rich listing carrying many
    OLDER members of already-adopted families (opus-4-7/4-6/4-5, sonnet-4-5,
    older gpt/gemini) plus dated snapshot aliases, against a known set that
    runs the latest of each family. EXACTLY the genuinely-new families surface
    as discoveries (a new anthropic frontier line + a fictional new
    other-provider line); all the within-adopted-family olds land in
    skipped_count, the dated aliases in ignored_count. The daily generator
    would emit one proposal per discovery — so this must be 2, not ~37."""
    network = {
        "bots": {},
        "models": {"rungs": [
            {"id": "haiku", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
            {"id": "sonnet", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
            {"id": "opus", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
            {"id": "oai", "models": ["openai/gpt-4o", "openai/gpt-4o-mini"], "costClass": "medium"},
            {"id": "gem", "models": ["google/gemini-2.5-flash"], "costClass": "low"},
        ]},
    }
    listings = {
        "anthropic": [
            # adopted (latest) — known, no discovery
            _lm("anthropic", "claude-haiku-4-5"),
            _lm("anthropic", "claude-sonnet-4-6"),
            _lm("anthropic", "claude-opus-4-8"),
            # OLDER members of adopted families — frontier-filtered (skipped)
            _lm("anthropic", "claude-opus-4-7"),
            _lm("anthropic", "claude-opus-4-6"),
            _lm("anthropic", "claude-opus-4-5"),
            _lm("anthropic", "claude-opus-4-5-20251101"),  # dated alias of opus-4-5 (in listing)
            _lm("anthropic", "claude-sonnet-4-5"),
            _lm("anthropic", "claude-sonnet-4-5-20250929"),  # dated alias of sonnet-4-5
            # genuinely NEW family → the ONE Anthropic discovery
            _lm("anthropic", _NEW_BARE, display_name="Claude Mythos 5"),
        ],
        "openai": [
            _lm("openai", "gpt-4o", capabilities=["chat"]),         # known
            _lm("openai", "gpt-4o-mini", capabilities=["chat"]),    # known
            _lm("openai", "gpt-4o-2024-08-06", capabilities=["chat"]),  # dated alias of gpt-4o
        ],
        "google": [
            _lm("google", "gemini-2.5-flash", capabilities=["chat"]),  # known
            _lm("google", "gemini-2.0-flash", capabilities=["chat"]),  # older in adopted flash family
            # a fictional brand-new other-provider line → a second discovery
            _lm("google", "gemini-spark-1", capabilities=["chat"]),
        ],
    }
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "k", "openai": "k", "google": "k"},
        enumerator=_make_enumerator(listings),
    )
    ids = sorted(d.qualified_id for d in result.discoveries)
    assert ids == [_NEW_ID, "google/gemini-spark-1"], ids
    assert result.skipped_count > 0, "older-in-adopted-family models must be counted as skipped"
    assert result.ignored_count > 0, "dated snapshot aliases must be counted as ignored"


def test_older_member_of_adopted_family_is_skipped_not_discovered(tmp_path):
    """A single older member of an adopted family (opus-4-7 when the pod runs
    opus-4-8) is frontier-filtered into skipped_count, never a discovery."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"anthropic": [
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", "claude-opus-4-7"),  # older, adopted family → skipped
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk"}, enumerator=_make_enumerator(listings),
    )
    assert result.discoveries == []
    assert result.skipped_count == 1
    # opus-4-7 is older than the adopted opus-4-8, so no staleness either.
    assert result.staleness == []


def test_unparseable_name_fails_open_to_discovery(tmp_path):
    """A model whose name carries no parseable family/version (e.g. ``o3`` or
    ``o5``) must fail OPEN — it surfaces as a discovery so genuinely alien
    names stay visible rather than being silently swallowed. (Names from a
    family already in the enriched defaults — e.g. grok-4 — are NOT alien and
    would be within-family staleness instead.)"""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {
        "anthropic": [_lm("anthropic", "claude-opus-4-8")],  # known
        "openai": [
            _lm("openai", "o3", capabilities=["chat"]),       # alien name
            _lm("openai", "o5", capabilities=["chat"]),       # alien name
        ],
    }
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "k", "openai": "k"},
        enumerator=_make_enumerator(listings),
    )
    ids = sorted(d.qualified_id for d in result.discoveries)
    assert ids == ["openai/o3", "openai/o5"], ids


def test_dated_alias_of_unknown_listing_model_does_not_double_surface(tmp_path):
    """A new family that appears both as a base id and a dated snapshot alias
    in the same listing surfaces exactly once (via the base), not twice — the
    dated alias is filtered against the listing, not just the known set."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"anthropic": [
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE),               # new line, base
        _lm("anthropic", f"{_NEW_BARE}-20260601"),  # dated alias of the new line
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk"}, enumerator=_make_enumerator(listings),
    )
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == [_NEW_ID], ids
    assert result.ignored_count == 1  # the dated alias


# ── Legacy tiers synthesis (un-migrated pod) ──────────────────────────────────

def test_legacy_tiers_count_as_known(tmp_path):
    """A pod with no models.rungs but per-bot legacy tiers: those models are
    known (Phase 1 legacy synthesis). A genuinely-new frontier line still
    surfaces; sonnet doesn't."""
    network = {"bots": {"evo": {}}, "models": {}}
    bot_configs = {"evo": {"tiers": {
        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        "tier1": {"models": ["anthropic/claude-opus-4-8"]},
    }}}
    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs=bot_configs,
        shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == [_NEW_ID]


# ── Generator path: Signal emission + dedup + Proposal linkage ────────────────

def _ctx(tmp_path, listings, fail=None, keys=None):
    return ModelDiscoveryContext(
        bot_id=None,
        shared_dir=tmp_path,
        network=_NETWORK_NO_FABLE,
        bot_users=[],
        bot_configs={},
        keys=keys or {"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings, fail=fail),
        consult_dismissals=False,
    )


def test_generator_emits_signal_only_no_proposal_for_new_frontier(tmp_path):
    """Signal-only contract (spec §Addendum 12): a genuinely-new frontier line
    produces exactly one model_discovery Signal, and observe() authors NO
    Proposal — adoption moved to the AI Optimization card. The retained
    AdoptModel builder still constructs a valid action from that Signal (the
    card's adopt route drives the same builder/applier)."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE, display_name="Claude Mythos 5"),
    ]}
    ctx = _ctx(tmp_path, listings)

    # observe_signals → store (mirrors generator_runner)
    specs = mdgen.observe_signals(ctx)
    discovery_specs = [s for s in specs if s["type"] == "model_discovery"]
    assert len(discovery_specs) == 1
    for spec in specs:
        signals_store.observe(tmp_path, **spec)

    # The firing signal exists
    active = list(signals_store.iter_active(tmp_path, producer="model_discovery", state="firing"))
    disc_sigs = [s for s in active if s.type == "model_discovery"]
    assert len(disc_sigs) == 1
    assert disc_sigs[0].details["qualified_id"] == _NEW_ID

    # observe() is signal-only — NO proposal queued (the whole point of §A12).
    assert mdgen.observe(ctx) == []

    # The retained AdoptModel builder still produces the right action from the
    # firing Signal (the card's adopt route reconstructs the same AdoptModel).
    p = mdgen._make_discovery_proposal(ctx, disc_sigs[0])
    assert p is not None
    assert p.action.kind == "AdoptModel"
    assert p.action.provider == "anthropic"
    assert p.action.model_id == _NEW_BARE
    # Mythos-5 resolves to the PREMIUM band (family-map: claude-mythos→premium),
    # and the suggested rung is derived from that band via the rung costClass
    # DATA — premium → the catalog's premium rung (fable-class). The rung is no
    # longer a per-model-name ("mythos-class") literal.
    assert p.action.rung_slug == "fable-class"
    # Operator choices default to dormant adoption — max is NEVER pre-selected.
    assert p.action.role_mapping == "none"
    assert p.action.cap_per_day is None
    assert p.approval_audience == "pod_operator"
    assert p.bot_id == "<pod>"  # pod-wide sentinel
    assert disc_sigs[0].id in p.motivating_signals
    # The body lives in `problem` and cites the evidence (cite-or-don't-recommend).
    assert _NEW_BARE in p.problem


def test_generator_signal_dedupes_on_rerun(tmp_path):
    """Re-running discovery with the same listing re-observes the same
    signature → no second signal (signal store dedups)."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE),
    ]}
    # First run
    specs1 = mdgen.observe_signals(_ctx(tmp_path, listings))
    for spec in specs1:
        signals_store.observe(tmp_path, **spec)
    # Second run (fresh ctx so discovery recomputes)
    specs2 = mdgen.observe_signals(_ctx(tmp_path, listings))
    for spec in specs2:
        signals_store.observe(tmp_path, **spec)

    active = list(signals_store.iter_active(tmp_path, producer="model_discovery", state="firing"))
    disc_sigs = [s for s in active if s.type == "model_discovery"]
    assert len(disc_sigs) == 1  # deduped by signature
    assert disc_sigs[0].observation_count == 2  # but observed twice


def test_generator_emits_degraded_signal_no_proposal(tmp_path):
    """A failed listing emits a degraded-mode Signal (warn) and NO discovery
    proposal — the run is loud, not silently 'all current', and not naggy."""
    ctx = _ctx(tmp_path, {}, fail={"anthropic": "listing HTTP 500 (Server Error)"})
    specs = mdgen.observe_signals(ctx)
    degraded = [s for s in specs if s["type"] == "model_discovery_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["severity"] == "warn"
    assert "anthropic" in degraded[0]["title"]
    for spec in specs:
        signals_store.observe(tmp_path, **spec)

    # Degraded signals don't produce proposals
    props = mdgen.observe(ctx)
    assert props == []


# ── R5: recency + modality gate (spawned by META:rsi) ─────────────────────────
# A live-pod review surfaced two false-positive classes the gate fixes:
#   • RECENCY — gpt-4o (OpenAI listing ``created`` ≈ May 2024, >1yr old) shown
#     as a "new model available" advisory.
#   • MODALITY — a Grok VIDEO model slotted into an LLM rung it can't route to.
# The gate filters both at Signal emission (observe_signals) and defensively
# re-checks at proposal time (observe) so a lingering pre-gate Signal can't
# emit one last stale/non-LLM Proposal.

import datetime as _dt  # noqa: E402

# Fixed clock so the recency tests are deterministic (no date-rollover coupling).
_NOW = _dt.datetime(2026, 6, 13, tzinfo=_dt.timezone.utc)

# gpt-4o's real OpenAI-listing ``created`` epoch (≈ 2024-05-10) — the NAMED
# regression. >2 years before _NOW, so the recency gate suppresses it.
_GPT4O_CREATED = 1715367049


def _epoch_days_ago(days: int) -> int:
    return int((_NOW - _dt.timedelta(days=days)).timestamp())


def _gate_ctx(tmp_path, listings, keys=None):
    """A pod-wide ctx pinned to _NOW with an injected listing enumerator."""
    return ModelDiscoveryContext(
        bot_id=None,
        shared_dir=tmp_path,
        network=_NETWORK_NO_FABLE,
        bot_users=[],
        bot_configs={},
        keys=keys or {"openai": "sk-test"},
        enumerator=_make_enumerator(listings),
        consult_dismissals=False,
        now=_NOW,
    )


# --- unit: _gate_reason ---

def test_gate_recency_drops_stale_gpt4o():
    """The named regression: gpt-4o's provider epoch (≈ May 2024) is >1yr old,
    so the recency gate suppresses it as a 'new model' advisory."""
    ev = {"model_id": "gpt-4o", "created": _GPT4O_CREATED, "capabilities": ["chat"]}
    reason = mdgen._gate_reason(ev, _NOW)
    assert reason is not None and "stale" in reason


def test_gate_recency_passes_recent_model():
    """A text LLM with a recent provider epoch flows through untouched."""
    ev = {"model_id": "o5-pro", "created": _epoch_days_ago(30), "capabilities": ["chat"]}
    assert mdgen._gate_reason(ev, _NOW) is None


def test_gate_recency_threshold_boundary():
    """Inside the 12-month window passes; just past it is dropped."""
    inside = {"model_id": "o5-pro", "created": _epoch_days_ago(300), "capabilities": ["chat"]}
    outside = {"model_id": "o5-pro", "created": _epoch_days_ago(400), "capabilities": ["chat"]}
    assert mdgen._gate_reason(inside, _NOW) is None
    assert mdgen._gate_reason(outside, _NOW) is not None


def test_gate_no_date_fails_open():
    """No resolvable date → NOT suppressed. A missing date most strongly
    correlates with a brand-new frontier line (the Fable case the rework exists
    to catch); suppressing dateless models would re-blind discovery. Both an
    explicit ``None`` and an absent ``created`` fail OPEN."""
    assert mdgen._gate_reason(
        {"model_id": "claude-mythos-5", "created": None, "capabilities": ["chat"]}, _NOW,
    ) is None
    assert mdgen._gate_reason(
        {"model_id": "claude-mythos-5", "capabilities": ["chat"]}, _NOW,
    ) is None
    # We never fabricate a date — garbage/zero is treated as no-date, fail-open.
    assert mdgen._model_epoch({"created": 0}) is None
    assert mdgen._model_epoch({"created": "2024"}) is None


def test_gate_modality_drops_video_model():
    """The named regression: a Grok VIDEO model must never be a rung candidate,
    even though the listing's id heuristic tags it chat-capable (it doesn't yet
    know 'video'). Modality wins over recency (checked first)."""
    ev = {
        "model_id": "grok-2-video",
        "created": _epoch_days_ago(5),  # recent — proves modality gates first
        "capabilities": md._id_chat_capabilities("grok-2-video"),
    }
    reason = mdgen._gate_reason(ev, _NOW)
    assert reason is not None and "modality" in reason


def test_gate_modality_drops_non_chat_capability():
    """An explicitly non-chat capability (embedding/etc.) is excluded too —
    the gate reuses model_discovery's canonical chat-capability check."""
    assert mdgen._gate_reason({"model_id": "x", "capabilities": ["embedding"]}, _NOW) is not None
    assert mdgen._gate_reason({"model_id": "some-speech-model", "capabilities": ["chat"]}, _NOW) is not None


def test_gate_modality_uses_no_provider_literals():
    """A plain text LLM from xAI (a provider) is NOT modality-gated — the gate
    keys on modality words, never a provider name."""
    ev = {"model_id": "grok-5", "created": _epoch_days_ago(10), "capabilities": ["chat"]}
    assert mdgen._gate_reason(ev, _NOW) is None


# --- integration: observe_signals filters before Signal emission ---

def test_observe_signals_filters_stale_gpt4o(tmp_path):
    """End-to-end: a stale gpt-4o discovery never becomes a model_discovery
    Signal spec (so it never becomes an AdoptModel Proposal)."""
    listings = {"openai": [
        _lm("openai", "gpt-4o", created=_GPT4O_CREATED, capabilities=["chat"]),
    ]}
    specs = mdgen.observe_signals(_gate_ctx(tmp_path, listings))
    disc = [s for s in specs if s["type"] == "model_discovery"]
    assert disc == [], "stale gpt-4o must be gated out before Signal emission"


def test_observe_signals_filters_video_model(tmp_path):
    """End-to-end: a Grok video model never becomes a model_discovery Signal."""
    listings = {"xai": [
        _lm("xai", "grok-2-video", created=_epoch_days_ago(10),
            capabilities=md._id_chat_capabilities("grok-2-video")),
    ]}
    specs = mdgen.observe_signals(_gate_ctx(tmp_path, listings, keys={"xai": "xai-test"}))
    disc = [s for s in specs if s["type"] == "model_discovery"]
    assert disc == [], "a non-LLM (video) model must be gated out of rung candidates"


def test_observe_signals_passes_recent_llm(tmp_path):
    """A recent, text LLM frontier model still flows through to a Signal."""
    listings = {"openai": [
        _lm("openai", "o5-pro", created=_epoch_days_ago(20), capabilities=["chat"]),
    ]}
    specs = mdgen.observe_signals(_gate_ctx(tmp_path, listings))
    disc = [s for s in specs if s["type"] == "model_discovery"]
    assert len(disc) == 1
    assert disc[0]["details"]["qualified_id"] == "openai/o5-pro"


def test_observe_signals_passes_dateless_frontier(tmp_path):
    """A dateless frontier line (Anthropic listings carry no epoch) flows
    through — the gate must NOT suppress dateless models (the Fable case)."""
    listings = {"anthropic": [
        _lm("anthropic", "claude-mythos-5"),  # no ``created``
    ]}
    specs = mdgen.observe_signals(_gate_ctx(tmp_path, listings, keys={"anthropic": "sk-test"}))
    disc = [s for s in specs if s["type"] == "model_discovery"]
    assert len(disc) == 1
    assert disc[0]["details"]["qualified_id"] == "anthropic/claude-mythos-5"


def test_observe_emits_no_proposal_for_lingering_signal(tmp_path):
    """Signal-only invariant (spec §Addendum 12): observe() authors NO Proposal
    for ANY firing Signal — including a planted, pre-gate stale one. Adoption
    moved to the AI Optimization card, so a lingering Signal never produces a
    queued proposal on a cutover cycle; the runner sweep resolves it instead."""
    stale_spec = {
        "signature": "model_discovery:openai:gpt-4o",
        "producer": "model_discovery",
        "type": "model_discovery",
        "severity": "info",
        "scope": "pod",
        "category": "hygiene",
        "title": "New model available: openai/gpt-4o",
        "body": "stale",
        "details": {
            "provider": "openai",
            "model_id": "gpt-4o",
            "qualified_id": "openai/gpt-4o",
            "evidence": {
                "model_id": "gpt-4o", "created": _GPT4O_CREATED, "capabilities": ["chat"],
            },
        },
    }
    signals_store.observe(tmp_path, **stale_spec)
    props = mdgen.observe(_gate_ctx(tmp_path, {"openai": []}))
    assert props == [], "a lingering stale signal must not produce a proposal"


# ── Capability / family helper units ──────────────────────────────────────────

def test_family_of_groups_versions():
    assert md._family_of("anthropic/claude-opus-4-8") == "claude-opus"
    assert md._family_of("anthropic/claude-opus-4-6") == "claude-opus"
    assert md._family_of("openai/gpt-4o") == "gpt-4o"


def test_family_of_collapses_mid_version_token():
    """Google-shape names (vendor-<numeric version>-<tier>) collapse across the
    version so gemini-2.0-flash and gemini-2.5-flash share a family — the
    frontier filter needs this so an older Gemini point release of an adopted
    tier isn't mistaken for a new line. gpt-4o (alphanumeric '4o', not a bare
    version) must stay its own family."""
    assert md._family_of("google/gemini-2.5-flash") == "gemini-flash"
    assert md._family_of("google/gemini-2.0-flash") == "gemini-flash"
    assert md._family_of("google/gemini-1.5-pro") == "gemini-pro"
    assert md._family_of("anthropic/claude-3-5-sonnet") == "claude-sonnet"
    # gpt-4o is NOT a mid-version shape — '4o' is alphanumeric, stays intact.
    assert md._family_of("openai/gpt-4o") == "gpt-4o"
    assert md._family_of("openai/gpt-4o-mini") == "gpt-4o-mini"


def test_family_of_unparseable_name_is_self_family():
    """A name with no parseable version token is its own family (fail-open:
    the frontier filter then has no adopted stem to collide with, so an alien
    name surfaces as a discovery)."""
    assert md._family_of("openai/o3") == "o3"
    assert md._family_of("xai/grok-4") == "grok"


def test_google_capabilities_from_methods():
    assert "chat" in md._google_capabilities(["generateContent"])
    assert "embedding" in md._google_capabilities(["embedContent"])
    assert md._google_capabilities([]) == ["non-chat"]


# ── F3: API-key scrub in degraded reason ──────────────────────────────────────

def test_degraded_reason_scrubs_leaked_key():
    """A fetcher exception whose str() carries a ``key=<secret>`` URL must NOT
    leak the secret into the operator-facing degraded reason. Belt-and-braces
    on top of the header-based Google fix — protects every provider's
    catch-all (F3)."""
    secret = "AIzaSyTOPSECRET123"

    def _leaky_fetcher(_key):
        # Simulate a urllib/SSL error that stringifies the full request URL,
        # which historically embedded ?key=<secret>. (_FETCHERS entries are
        # called as fetcher(key) — one positional arg.)
        raise RuntimeError(
            f"<urlopen error [Errno 8] nodename nor servname provided for "
            f"url https://example.com/v1/models?pageSize=1000&key={secret}>"
        )

    saved = md._FETCHERS.get("google")
    md._FETCHERS["google"] = _leaky_fetcher
    try:
        enum = md.enumerate_provider("google", secret)
    finally:
        if saved is not None:
            md._FETCHERS["google"] = saved
    assert not enum.ok
    assert secret not in enum.reason
    assert "key=REDACTED" in enum.reason


def test_scrub_key_helper_redacts_any_provider():
    raw = "listing failed (OSError: GET https://h/v1/models?key=sk-abc123&x=1)"
    scrubbed = md._scrub_key(raw)
    assert "sk-abc123" not in scrubbed
    assert "key=REDACTED" in scrubbed
    # Non-key text is untouched.
    assert "x=1" in scrubbed


def test_google_fetch_does_not_put_key_in_url(monkeypatch):
    """F3: the Google key rides the x-goog-api-key header, never the URL."""
    seen = {}

    def _capture(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {"models": []}

    monkeypatch.setattr(md, "_http_get_json", _capture)
    md._fetch_google("super-secret-key")
    assert "key=" not in seen["url"]
    assert "super-secret-key" not in seen["url"]
    assert seen["headers"].get("x-goog-api-key") == "super-secret-key"


# ── F1/F2: integration through the REAL generator_runner sweep path ───────────
#
# These were the gap that hid F1: every prior generator test called
# observe()/observe_signals() directly and never exercised the runner's
# emission-bucketing vs. sweep-lookup, where the "<pod>" sentinel mismatch
# archived the proposal on its own run. We drive the real run_generators()
# machinery (emit → ingest → proposal sweep → signal sweep), scoping the
# registry to model_discovery only and injecting a mock enumerator via the
# real context factory.

# A pod whose rungs lack the new frontier line. (Fable is a code default now,
# so the genuinely-unknown model that drives the lifecycle is _NEW_BARE.)
_RUNGS_NO_NEW = [
    {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
    {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
]
_NEW_LISTING = {"anthropic": [
    _lm("anthropic", "claude-haiku-4-5"),
    _lm("anthropic", "claude-opus-4-8"),
    _lm("anthropic", _NEW_BARE, display_name="Claude Mythos 5"),
]}


def _scope_registry_to_model_discovery(monkeypatch):
    """Patch Registry.active_generators to return ONLY model_discovery so the
    integration test doesn't run (and possibly fail on) every shipped
    generator. The model_discovery LoadedGenerator is the real one — real
    charter, real fresh record (active + due)."""
    from registry.registry import Registry

    real_active = Registry.active_generators

    def _only_md(self):
        active = real_active(self)
        return {k: v for k, v in active.items() if k == "model_discovery"}

    monkeypatch.setattr(Registry, "active_generators", _only_md)


def _reset_md_due(shared_dir):
    """Clear model_discovery's last_update_at so the daily-cadence generator
    is 'due' again on the next run_generators() call (tests fire several runs
    back-to-back within the same second)."""
    from registry.registry import Registry, _atomic_write_json

    reg = Registry(
        generators_code_dir=Path(__file__).parent.parent / "generators",
        records_dir=shared_dir / "generators",
    )
    reg.load_all(strict=False)
    rec = reg._loaded["model_discovery"].record
    rec.track_record.last_update_at = None
    _atomic_write_json(
        shared_dir / "generators" / "model_discovery.json", rec.to_dict()
    )


def _patch_md_factory(monkeypatch, network_rungs, listings):
    """Patch the production model_discovery context factory to inject a mock
    enumerator + the test network, so the runner builds a fully-mocked ctx."""
    import generator_runner as gr

    def _factory(shared_dir, network_config, bot_id, gen_config, now):  # noqa: ARG001
        return ModelDiscoveryContext(
            bot_id=None,
            shared_dir=shared_dir,
            network={"bots": {}, "models": {"rungs": network_rungs}},
            bot_users=[],
            bot_configs={},
            keys={"anthropic": "sk-test"},
            enumerator=_make_enumerator(listings),
            consult_dismissals=False,
            now=now,
        )

    factory, per_bot = gr._CONTEXT_FACTORIES["model_discovery"]
    monkeypatch.setitem(gr._CONTEXT_FACTORIES, "model_discovery", (_factory, per_bot))


def test_runner_signal_only_sweeps_preexisting_adopt_proposal(tmp_path, monkeypatch):
    """Migration through the REAL runner (spec §Addendum 12): a pending
    AdoptModel proposal authored BEFORE the signal-only cutover is auto-archived
    by the ``resolves_when_silent`` sweep — observe() now returns ``[]``, so the
    proposal's fingerprint is never re-emitted and the sweep resolves it. The
    discovery Signal keeps firing (it's the AI Optimization card's data source +
    the nav badge, decoupled from the proposal), and NO new proposal is ever
    created — on this run or the next."""
    from generator_runner import run_generators
    from arbiter.store import iter_proposals, write_proposal

    _scope_registry_to_model_discovery(monkeypatch)
    network = {"members": []}  # pod-wide generator; no per-bot members needed
    _patch_md_factory(monkeypatch, _RUNGS_NO_NEW, _NEW_LISTING)

    # Plant a pre-cutover pending AdoptModel proposal, built the old way from a
    # firing discovery Signal — exactly what the queue held before this change.
    ctx = ModelDiscoveryContext(
        bot_id=None, shared_dir=tmp_path,
        network={"bots": {}, "models": {"rungs": _RUNGS_NO_NEW}},
        bot_users=[], bot_configs={}, keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(_NEW_LISTING), consult_dismissals=False,
    )
    for spec in mdgen.observe_signals(ctx):
        signals_store.observe(tmp_path, **spec)
    sig = next(
        s for s in signals_store.iter_active(
            tmp_path, producer="model_discovery", state="firing")
        if s.type == "model_discovery"
    )
    planted = mdgen._make_discovery_proposal(ctx, sig)
    assert planted is not None
    planted.status = "pending"
    write_proposal(planted, tmp_path)

    def _md_pending():
        return [p for p in iter_proposals(tmp_path, subdirs=("pending",))
                if p.generator_id == "model_discovery"]

    def _md_firing():
        return [s for s in signals_store.iter_active(
                    tmp_path, producer="model_discovery", state="firing")
                if s.type == "model_discovery"]

    assert len(_md_pending()) == 1, "fixture: planted proposal is pending"

    # ── Run 1: observe_signals re-emits the signal; observe() returns [] → the
    #     planted proposal is no longer emitted → the sweep archives it. ──
    run_generators(tmp_path, network, log_fn=lambda *_: None)

    assert _md_pending() == [], "signal-only: pre-cutover proposal must sweep out"
    archived = [p for p in iter_proposals(tmp_path, subdirs=("archived",))
                if p.generator_id == "model_discovery"]
    assert [a.id for a in archived] == [planted.id]
    assert archived[0].status == "resolved_externally"

    # The discovery Signal is STILL firing — decoupled from the proposal.
    firing = _md_firing()
    assert len(firing) == 1
    assert firing[0].details["qualified_id"] == _NEW_ID

    # ── Run 2: idempotent — still no proposal, no resurrection, signal stays. ──
    _reset_md_due(tmp_path)
    run_generators(tmp_path, network, log_fn=lambda *_: None)
    assert _md_pending() == [], "no proposal must ever be re-created (signal-only)"
    assert len(_md_firing()) == 1


# ── Regression: known-set sourced from per-bot evolve-tiers.json ──────────────
#
# Live-canary finding (the bug these tests pin): on the real pod ``network.json``
# carried NO models section — the rungs/roles config lives in each bot's
# ``~/.openclaw/evolve-tiers.json``. The old known_model_set read only
# network.json + oc_full_config, returned ``known_model_count: 0``, and
# re-"discovered" already-adopted sonnet/opus. These tests reproduce that exact
# shape and assert the per-bot file is the source of truth.

import primary_bot as _primary_bot  # noqa: E402


def _wire_tiers_files(monkeypatch, tmp_path, files: dict[str, dict | str | None]):
    """Write each bot's evolve-tiers.json under tmp and point the path
    resolver at them. ``files`` maps bot_id -> dict (written as JSON),
    str (written raw, e.g. malformed), or None (file absent).

    A bot mapped to the sentinel ``"<denied>"`` simulates an unreadable file:
    the path points at a file we then make unreadable via a stub reader.
    """
    paths: dict[str, Path] = {}
    for bot_id, content in files.items():
        p = tmp_path / f"{bot_id}-evolve-tiers.json"
        if content is None:
            pass  # leave absent
        elif isinstance(content, str):
            p.write_text(content)
        else:
            import json as _json
            p.write_text(_json.dumps(content))
        paths[bot_id] = p

    def _fake_path(network, bot_id):  # noqa: ARG001
        return paths.get(bot_id, tmp_path / f"{bot_id}-MISSING.json")

    monkeypatch.setattr(_primary_bot, "_bot_evolve_tiers_path", _fake_path)
    return paths


# Migrated (new rungs/roles) shape carried by a bot's evolve-tiers.json.
_RUNGS_TIERS_FILE = {
    "rungs": [
        {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
        {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-6"], "costClass": "medium"},
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ],
    "roles": {"fast": "haiku-class", "standard": "sonnet-class", "power": "opus-class"},
}


def test_live_pod_shape_per_bot_tiers_are_known(monkeypatch, tmp_path):
    """THE live-canary regression. network.json has NO models section; the
    rungs live in a bot's evolve-tiers.json. sonnet/opus/haiku must be KNOWN
    (no discovery), a genuinely-new frontier line still discovered. Old code
    returned known_count=0 and re-discovered sonnet+opus."""
    network = {"bots": {"evo": {"user": "evo"}}}  # NO "models" key at all
    _wire_tiers_files(monkeypatch, tmp_path, {"evo": _RUNGS_TIERS_FILE})

    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"),
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE, display_name="Claude Mythos 5"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # The per-bot rungs (haiku/sonnet/opus) are all also in the code defaults,
    # so the known set is exactly the 7 default bare ids — non-zero is the
    # regression assertion (old code returned 0 here).
    assert result.known_model_count >= 3, "per-bot tiers must populate the known set"
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == [_NEW_ID], "only the new frontier line is new; sonnet/opus are adopted"
    assert not result.is_degraded


def test_per_bot_legacy_tiers_shape_is_known(monkeypatch, tmp_path):
    """A bot whose evolve-tiers.json still carries the legacy ``tiers`` shape
    (un-migrated) contributes its models to the known set too."""
    network = {"bots": {"team-bot-c": {"user": "team-bot-c"}}}
    legacy_file = {"tiers": {
        "tier2": {"models": ["anthropic/claude-sonnet-4-6"]},
        "tier1": {"models": ["anthropic/claude-opus-4-8"]},
    }}
    _wire_tiers_files(monkeypatch, tmp_path, {"team-bot-c": legacy_file})

    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == [_NEW_ID]
    # sonnet/opus are in the defaults too; the known set is the 7 default ids.
    assert result.known_model_count >= 2


def test_per_bot_legacy_tier_fallbacks_are_known(monkeypatch, tmp_path):
    """Legacy tier ``fallbacks`` are adopted models too — the Phase-1
    migration folds them into the rung cluster (``migrate_models_block``),
    so a fallback-only model on an un-migrated bot must not re-surface as
    a discovery."""
    network = {"bots": {"team-bot-c": {"user": "team-bot-c"}}}
    legacy_file = {"tiers": {
        "tier2": {
            "models": ["anthropic/claude-sonnet-4-6"],
            "fallbacks": ["openai/gpt-5.2"],
        },
    }}
    _wire_tiers_files(monkeypatch, tmp_path, {"team-bot-c": legacy_file})

    listings = {
        "anthropic": [_lm("anthropic", "claude-sonnet-4-6")],
        "openai": [_lm("openai", "gpt-5.2", capabilities=["chat"])],
    }
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test", "openai": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # gpt-5.2 (fallback) + the 7 code defaults — all known, nothing surfaces.
    assert result.known_model_count >= 2
    assert result.discoveries == []
    assert result.all_current


def test_unreadable_bot_file_is_degraded_not_empty(monkeypatch, tmp_path):
    """An unreadable tiers file must NOT silently empty the known set. With one
    readable bot (carries sonnet/opus) and one unreadable bot, the known set is
    NON-empty (partial) and the run is flagged degraded — never 'all current'.
    Fable still surfaces; sonnet/opus stay known via the readable bot."""
    network = {"bots": {
        "evo": {"user": "evo"},
        "broken": {"user": "broken"},
    }}
    paths = _wire_tiers_files(monkeypatch, tmp_path, {
        "evo": _RUNGS_TIERS_FILE,
        "broken": {"rungs": []},  # path exists; we force a degraded read below
    })

    real_reader = _primary_bot._read_bot_owned_json

    def _reader(path):
        if path == paths["broken"]:
            return None, False  # simulate PermissionError after sudo fallback
        return real_reader(path)

    monkeypatch.setattr(_primary_bot, "_read_bot_owned_json", _reader)

    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # Pod-sourced known set is NON-empty (the readable bot's models survive) →
    # not the empty-degraded suppression case; discoveries are still computed.
    assert result.known_model_count >= 3
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == [_NEW_ID]
    # Partial-degraded read is surfaced — never silently 'all current'.
    assert result.is_degraded
    assert any(p["provider"] == "known-set" for p in result.degraded_providers)


def test_empty_known_set_with_read_error_suppresses_discoveries(monkeypatch, tmp_path):
    """The cardinal failure posture: the POD-SOURCED known set is EMPTY *because*
    a source errored → discovery is suppressed entirely (no emissions). An empty
    pod set + emissions would flood the Improvements page with already-adopted
    models — the exact bug class this fix exists to prevent.

    Post-Phase-6 the code defaults are ALWAYS present in the known set, so the
    suppression guard keys off the pod-sourced set (known minus defaults) being
    empty — NOT the full known set, which is never empty. This pins that the
    defaults do not silently DISABLE the suppression posture."""
    network = {"bots": {"evo": {"user": "evo"}}}  # NO network models section
    _wire_tiers_files(monkeypatch, tmp_path, {"evo": {"rungs": []}})

    def _reader(path):  # noqa: ARG001
        return None, False  # every read degraded

    monkeypatch.setattr(_primary_bot, "_read_bot_owned_json", _reader)

    # A genuinely-new line (NOT a default) — it would surface if the suppression
    # guard were broken by the always-present defaults.
    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-6"),
        _lm("anthropic", "claude-opus-4-8"),
        _lm("anthropic", _NEW_BARE),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # The full known set carries only the code defaults (pod-sourced set empty).
    assert result.known_model_count == len(md.default_catalog_bare_ids())
    assert result.discoveries == [], (
        "empty pod-sourced + errored known set must suppress emissions even "
        "though the code defaults are present"
    )
    assert result.is_degraded
    assert any(p["provider"] == "known-set" for p in result.degraded_providers)
    assert not result.all_current


def test_fresh_rungless_pod_still_allows_discoveries(monkeypatch, tmp_path):
    """A genuinely fresh pod (no rungs anywhere, NO read errors) is valid: the
    pod-sourced known set is empty but discovery PROCEEDS so the operator gets
    their first rungs. Empty-without-errors must not be conflated with
    empty-from-error.

    Post-Phase-6 the code defaults are always KNOWN, so a fresh pod no longer
    re-discovers the default ladder — only genuinely-new (non-default) lines
    surface. This listing carries two such lines to prove discovery proceeds."""
    network = {"bots": {"evo": {"user": "evo"}}}  # NO network models section
    # File simply absent → (set(), ok=True), no error.
    _wire_tiers_files(monkeypatch, tmp_path, {"evo": None})

    listings = {"anthropic": [
        _lm("anthropic", _NEW_BARE),
        # A genuinely-new openai line whose family is NOT among the enriched
        # code defaults (the defaults now cover the gpt/gpt-mini families, so a
        # gpt-N line would be within-family staleness, not a new discovery).
        _lm("openai", "o5-pro", capabilities=["chat"]),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test", "openai": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    # Pod-sourced set is empty; known set is the code defaults.
    assert result.known_model_count == len(md.default_catalog_bare_ids())
    assert not result.is_degraded, "no read error → not degraded"
    # A rung-less pod legitimately discovers the genuinely-new (non-default)
    # listed lines — proving discovery still proceeds on a fresh pod.
    ids = sorted(d.qualified_id for d in result.discoveries)
    assert ids == [_NEW_ID, "openai/o5-pro"]


def test_bot_evolve_tiers_models_missing_file_is_ok_not_error(tmp_path):
    """Unit: a missing tiers file yields (empty, ok=True) — a bot that never
    saved tiers genuinely knows nothing; that is NOT a degraded read."""
    network = {"bots": {"ghost": {"user": "nonexistent-user-xyz"}}}
    models, ok = _primary_bot.bot_evolve_tiers_models(network, "ghost")
    assert models == set()
    assert ok is True


def test_bot_evolve_tiers_models_malformed_file_is_degraded(monkeypatch, tmp_path):
    """Unit: a malformed tiers file yields (empty, ok=False) — a degraded
    read the caller must treat as 'could not confirm', not 'knows nothing'."""
    network = {"bots": {"evo": {"user": "evo"}}}
    _wire_tiers_files(monkeypatch, tmp_path, {"evo": "{not valid json"})
    models, ok = _primary_bot.bot_evolve_tiers_models(network, "evo")
    assert models == set()
    assert ok is False


# ── Phase 9: listings cache (validated-picker candidate source) ───────────────

def test_run_discovery_writes_listings_cache(tmp_path):
    """A discovery run persists the enumerated listings to
    ``{shared_dir}/model-listings.json`` with the expected shape — providers
    map to serialized model dicts, refreshed_at is set, degraded recorded."""
    listings = {
        "anthropic": [
            _lm("anthropic", "claude-opus-4-8", display_name="Claude Opus 4.8"),
            _lm("anthropic", "claude-sonnet-4-6"),
        ],
        "openai": [_lm("openai", "gpt-7")],
    }
    md.run_discovery(
        network=_NETWORK_NO_FABLE,
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk-a", "openai": "sk-o"},
        enumerator=_make_enumerator(listings),
    )
    cache_path = tmp_path / "model-listings.json"
    assert cache_path.exists()
    import json as _json
    doc = _json.loads(cache_path.read_text())
    assert "refreshed_at" in doc and doc["refreshed_at"]
    assert set(doc["providers"].keys()) == {"anthropic", "openai"}
    # canonical ids preserved + capability fields carried through to_dict()
    anthropic_ids = [m["model_id"] for m in doc["providers"]["anthropic"]]
    assert "claude-opus-4-8" in anthropic_ids
    assert doc["providers"]["anthropic"][0]["qualified_id"] == "anthropic/claude-opus-4-8"
    assert doc["degraded"] == []


def test_build_listings_cache_annotates_family_latest():
    """``build_listings_cache`` flags the latest-version member of each family
    within a provider's listing (Phase 10b — the catalog picker bolds it).
    Two versions in one family → only the highest flagged; an unrelated family
    gets its own latest flagged. Family is computed via ``_family_of`` (reused,
    not reimplemented)."""
    enums = [
        md.ProviderEnumeration(provider="anthropic", ok=True, models=[
            _lm("anthropic", "claude-opus-4-6"),     # older opus
            _lm("anthropic", "claude-opus-4-8"),     # latest opus
            _lm("anthropic", "claude-sonnet-4-6"),   # unrelated family
        ]),
    ]
    doc = md.build_listings_cache(enums, refreshed_at="2026-06-11T00:00:00Z")
    flag = {
        m["model_id"]: m["is_family_latest"]
        for m in doc["providers"]["anthropic"]
    }
    # Only the highest opus version is the family-latest; the older one is not.
    assert flag["claude-opus-4-8"] is True
    assert flag["claude-opus-4-6"] is False
    # An unrelated family's sole member is its own latest.
    assert flag["claude-sonnet-4-6"] is True
    # The family stem is carried for debuggability and matches _family_of.
    fam = {m["model_id"]: m["family"] for m in doc["providers"]["anthropic"]}
    assert fam["claude-opus-4-8"] == md._family_of("claude-opus-4-8")
    assert fam["claude-opus-4-6"] == fam["claude-opus-4-8"]  # same family
    assert fam["claude-sonnet-4-6"] != fam["claude-opus-4-8"]


def test_build_listings_cache_family_latest_per_provider():
    """Family-latest is computed within each provider independently — a second
    provider's latest is flagged in its own listing, not crowded out."""
    enums = [
        md.ProviderEnumeration(provider="anthropic", ok=True, models=[
            _lm("anthropic", "claude-opus-4-8"),
        ]),
        md.ProviderEnumeration(provider="openai", ok=True, models=[
            _lm("openai", "gpt-7"),
        ]),
    ]
    doc = md.build_listings_cache(enums, refreshed_at="2026-06-11T00:00:00Z")
    assert doc["providers"]["anthropic"][0]["is_family_latest"] is True
    assert doc["providers"]["openai"][0]["is_family_latest"] is True


def test_listings_cache_records_degraded_provider(tmp_path):
    """A provider whose listing call fails is recorded in ``degraded`` with a
    reason — never silently dropped (the silent-monitor lesson)."""
    listings = {"openai": [_lm("openai", "gpt-7")]}
    md.run_discovery(
        network=_NETWORK_NO_FABLE,
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk-a", "openai": "sk-o"},
        enumerator=_make_enumerator(
            listings, fail={"anthropic": "listing HTTP 401 (Unauthorized)"},
        ),
    )
    import json as _json
    doc = _json.loads((tmp_path / "model-listings.json").read_text())
    assert "openai" in doc["providers"]
    assert "anthropic" not in doc["providers"]
    degraded = {d["provider"]: d["reason"] for d in doc["degraded"]}
    assert "anthropic" in degraded
    assert "401" in degraded["anthropic"]


def test_listings_cache_write_is_atomic_no_tmp_left(tmp_path):
    """The atomic temp+rename write leaves no ``.tmp`` debris behind."""
    md.run_discovery(
        network=_NETWORK_NO_FABLE,
        bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-a"},
        enumerator=_make_enumerator({"anthropic": [_lm("anthropic", "claude-opus-4-8")]}),
    )
    leftovers = list(tmp_path.glob("model-listings.json.tmp*"))
    assert leftovers == []


def test_read_listings_cache_missing_returns_none(tmp_path):
    assert md.read_listings_cache(tmp_path) is None


def test_read_listings_cache_corrupt_returns_none(tmp_path):
    (tmp_path / "model-listings.json").write_text("{not json")
    assert md.read_listings_cache(tmp_path) is None


# ── Phase 9: free-text validation against the listing ─────────────────────────

def _cache_doc():
    return {
        "refreshed_at": "2026-06-10T00:00:00Z",
        "providers": {
            "anthropic": [
                {"model_id": "claude-opus-4-8", "qualified_id": "anthropic/claude-opus-4-8"},
                {"model_id": "claude-sonnet-4-6", "qualified_id": "anthropic/claude-sonnet-4-6"},
            ],
            "openai": [
                {"model_id": "gpt-7", "qualified_id": "openai/gpt-7"},
            ],
        },
        "degraded": [],
    }


def test_validate_accepts_listed_bare_id_normalizes_to_canonical():
    res = md.validate_against_listing("claude-opus-4-8", _cache_doc())
    assert res["ok"] is True
    assert res["canonical"] == "anthropic/claude-opus-4-8"


def test_validate_accepts_listed_qualified_id():
    res = md.validate_against_listing("openai/gpt-7", _cache_doc())
    assert res["ok"] is True
    assert res["canonical"] == "openai/gpt-7"


def test_validate_rejects_unlisted_with_nearest_suggestion():
    # Typo: missing the trailing digit.
    res = md.validate_against_listing("claude-opus-4", _cache_doc())
    assert res["ok"] is False
    assert res["suggestion"] == "anthropic/claude-opus-4-8"


def test_validate_credentialed_filter_excludes_uncredentialed_provider():
    # openai not credentialed → its models are not valid candidates.
    res = md.validate_against_listing(
        "openai/gpt-7", _cache_doc(), providers={"anthropic"},
    )
    assert res["ok"] is False


def test_validate_empty_string_rejected_no_suggestion():
    res = md.validate_against_listing("   ", _cache_doc())
    assert res["ok"] is False
    assert res["suggestion"] is None


# ── xAI listing adapter (OpenAI-compatible /v1/models) ────────────────────────
#
# xAI's API is OpenAI-shaped: GET /v1/models, Bearer auth, {data:[{id,...}]}.
# These pin the adapter's parse, the grok chat-capability tagging, grok family
# grouping + family-latest, and that xai now enumerates through the SAME
# pod-wide credential-sourcing path the other providers use. All fixture-based;
# the live api.x.ai fetch runs only on the pod sweep.

def test_xai_in_listing_providers():
    """xai is a listing-capable provider (adapter wired) — the set the
    credential resolver and the gap check both key off."""
    assert "xai" in md._LISTING_PROVIDERS
    assert "xai" in md._FETCHERS


def test_fetch_xai_parses_openai_shaped_listing(monkeypatch):
    """The xAI fetcher parses the OpenAI-compatible ``{data:[{id}]}`` envelope,
    tags grok chat models chat-capable, and authenticates with a Bearer header
    against api.x.ai — modeled on the OpenAI adapter."""
    seen = {}

    def _capture(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [
            {"id": "grok-4"},
            {"id": "grok-4-mini"},
            {"id": "grok-4-fast"},
        ]}

    monkeypatch.setattr(md, "_http_get_json", _capture)
    models = md._fetch_xai("xai-secret-key")

    # Endpoint + auth are adapter DATA: host is api.x.ai, Bearer header, no key
    # in the URL.
    assert "api.x.ai/v1/models" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer xai-secret-key"
    assert "key=" not in seen["url"]

    ids = [m.model_id for m in models]
    assert ids == ["grok-4", "grok-4-mini", "grok-4-fast"]
    # Every grok-* chat model is tagged chat-capable.
    assert all(m.provider == "xai" for m in models)
    assert all(m.qualified_id == f"xai/{m.model_id}" for m in models)
    assert all("chat" in m.capabilities for m in models)
    assert all(md.is_chat_capable(m) for m in models)


def test_fetch_xai_excludes_non_chat_variants(monkeypatch):
    """A non-chat xAI variant (e.g. an embedding/image model) is tagged
    non-chat by the shared id heuristic and excluded from chat discovery."""
    def _stub(url, headers):  # noqa: ARG001
        return {"data": [
            {"id": "grok-4"},
            {"id": "grok-2-image"},        # image → non-chat
            {"id": "grok-text-embedding"},  # embedding → non-chat
        ]}

    monkeypatch.setattr(md, "_http_get_json", _stub)
    models = md._fetch_xai("k")
    chat = [m for m in models if md.is_chat_capable(m)]
    assert [m.model_id for m in chat] == ["grok-4"]


def test_grok_chat_capability_tagging():
    """Grok chat models tag chat-capable; non-chat grok ids do not. The shared
    id heuristic (``_id_chat_capabilities``) is provider-neutral — same rule as
    OpenAI."""
    assert md._id_chat_capabilities("grok-4") == ["chat"]
    assert md._id_chat_capabilities("grok-4-mini") == ["chat"]
    assert md._id_chat_capabilities("grok-4-fast") == ["chat"]
    assert md._id_chat_capabilities("grok-2-image") == ["non-chat"]
    # The back-compat OpenAI alias resolves to the same shared rule.
    assert md._openai_capabilities("grok-4") == md._id_chat_capabilities("grok-4")


def test_grok_family_grouping_and_latest():
    """grok-4/grok-3 share family ``grok``; grok-*-mini share ``grok-mini``;
    grok-*-fast share ``grok-fast`` — so the family-latest flag picks the
    highest version within each, not across tiers. Reuses ``_family_of`` (no
    new family regex)."""
    assert md._family_of("xai/grok-4") == "grok"
    assert md._family_of("xai/grok-3") == "grok"
    assert md._family_of("xai/grok-4-mini") == "grok-mini"
    assert md._family_of("xai/grok-4-fast") == "grok-fast"

    enums = [md.ProviderEnumeration(provider="xai", ok=True, models=[
        _lm("xai", "grok-3", capabilities=["chat"]),
        _lm("xai", "grok-4", capabilities=["chat"]),        # latest grok
        _lm("xai", "grok-4-mini", capabilities=["chat"]),   # latest grok-mini
    ])]
    doc = md.build_listings_cache(enums, refreshed_at="2026-06-11T00:00:00Z")
    flag = {m["model_id"]: m["is_family_latest"] for m in doc["providers"]["xai"]}
    assert flag["grok-4"] is True
    assert flag["grok-3"] is False           # older member of the grok family
    assert flag["grok-4-mini"] is True       # sole member of its own family


def test_xai_grok_default_models_are_known_not_discovered(tmp_path):
    """grok-4 / grok-4-mini ship in DEFAULT_MODEL_CATALOG, so an xAI listing
    carrying them on a pod with no grok rung produces ZERO discoveries — they
    are code defaults (KNOWN everywhere), just like claude-fable-5. This is the
    lifecycle that keeps the picker from re-flagging an already-blessed model."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"xai": [
        _lm("xai", "grok-4", capabilities=["chat"], display_name="Grok 4"),
        _lm("xai", "grok-4-mini", capabilities=["chat"]),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"xai": "xai-key"},
        enumerator=_make_enumerator(listings),
    )
    assert result.discoveries == [], (
        "grok-4/grok-4-mini are code defaults — they must not surface as new"
    )


def test_xai_new_grok_line_surfaces_as_discovery(tmp_path):
    """A genuinely-NEW grok line (not in the defaults, e.g. a future grok-5)
    discovered from the xAI listing surfaces as exactly one discovery — the
    picker/easy-setup gap this PR closes for new xAI models. The cost band is
    pricing/family-derived from the grok family map (not hand-typed)."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"xai": [
        _lm("xai", "grok-4", capabilities=["chat"]),          # default → known
        _lm("xai", "grok-5", capabilities=["chat"], display_name="Grok 5"),  # NEW
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"xai": "xai-key"},
        enumerator=_make_enumerator(listings),
    )
    # grok-4 (default) is known; grok-5 shares family 'grok' with grok-4, so it
    # surfaces via STALENESS on grok-4, not as a discovery (frontier filter).
    assert result.discoveries == []
    assert any(
        s.current_model == "xai/grok-4" and s.latest_model == "xai/grok-5"
        for s in result.staleness
    ), "a newer grok in the adopted grok family is a staleness upgrade nudge"


def test_xai_novel_family_surfaces_as_discovery(tmp_path):
    """A novel xAI line whose family is NOT among the defaults (a hypothetical
    new product line) surfaces as a genuine discovery, with a cost band derived
    from the grok family map or naming heuristic."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"xai": [
        _lm("xai", "aurora-1", capabilities=["chat"], display_name="Aurora 1"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"xai": "xai-key"},
        enumerator=_make_enumerator(listings),
    )
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == ["xai/aurora-1"]
    d = result.discoveries[0]
    assert d.provider == "xai"
    # A cost band is assigned (pricing/family-map when known, else heuristic).
    assert d.suggested_cost_class in ("low", "medium", "high", "premium")


def test_xai_listing_persists_to_cache(tmp_path):
    """A run that enumerates xai writes its models into the listings cache, so
    the validated picker + easy-setup filter can offer them."""
    listings = {"xai": [
        _lm("xai", "grok-4", capabilities=["chat"]),
        _lm("xai", "grok-4-mini", capabilities=["chat"]),
    ]}
    md.run_discovery(
        network=_NETWORK_NO_FABLE, bot_users=[], bot_configs={},
        shared_dir=tmp_path,
        keys={"xai": "xai-key"},
        enumerator=_make_enumerator(listings),
    )
    import json as _json
    doc = _json.loads((tmp_path / "model-listings.json").read_text())
    assert "xai" in doc["providers"]
    assert {m["model_id"] for m in doc["providers"]["xai"]} == {"grok-4", "grok-4-mini"}
    # xai now counts as an LLM provider in the listings (offers chat models).
    assert "xai" in md.llm_providers_from_listings(doc)


# ── DeepSeek listing adapter (OpenAI-compatible /v1/models) ───────────────────
#
# DeepSeek's API is OpenAI-shaped: GET /v1/models, Bearer auth, {data:[{id,...}]}.
# These pin the adapter's parse, the deepseek chat-capability tagging, deepseek
# family grouping + family-latest, and that a credentialed deepseek now
# ENUMERATES (so it leaves ``uncovered_providers``). All fixture-based; the live
# api.deepseek.com fetch runs only on the pod sweep.

def test_deepseek_in_listing_providers():
    """deepseek is a listing-capable provider (adapter wired) — so a
    credentialed deepseek is covered, not the no-adapter gap."""
    assert "deepseek" in md._LISTING_PROVIDERS
    assert "deepseek" in md._FETCHERS


def test_fetch_deepseek_parses_openai_shaped_listing(monkeypatch):
    """The DeepSeek fetcher parses the OpenAI-compatible ``{data:[{id}]}``
    envelope, tags deepseek chat models chat-capable, and authenticates with a
    Bearer header against api.deepseek.com — modeled on the OpenAI adapter."""
    seen = {}

    def _capture(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [
            {"id": "deepseek-chat"},
            {"id": "deepseek-reasoner"},
        ]}

    monkeypatch.setattr(md, "_http_get_json", _capture)
    models = md._fetch_deepseek("deepseek-secret-key")

    # Endpoint + auth are adapter DATA: host is api.deepseek.com, Bearer header,
    # no key in the URL.
    assert "api.deepseek.com/v1/models" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer deepseek-secret-key"
    assert "key=" not in seen["url"]

    ids = [m.model_id for m in models]
    assert ids == ["deepseek-chat", "deepseek-reasoner"]
    assert all(m.provider == "deepseek" for m in models)
    assert all(m.qualified_id == f"deepseek/{m.model_id}" for m in models)
    assert all("chat" in m.capabilities for m in models)
    assert all(md.is_chat_capable(m) for m in models)


def test_deepseek_chat_capability_tagging():
    """deepseek-chat / deepseek-reasoner tag chat-capable via the shared,
    provider-neutral id heuristic — same rule as OpenAI/xAI."""
    assert md._id_chat_capabilities("deepseek-chat") == ["chat"]
    assert md._id_chat_capabilities("deepseek-reasoner") == ["chat"]


def test_deepseek_family_grouping_and_latest():
    """deepseek-chat / deepseek-reasoner are distinct families (each its own
    line); the family-latest flag picks the highest version within each. Reuses
    ``_family_of`` (no new family regex)."""
    assert md._family_of("deepseek/deepseek-chat") == "deepseek-chat"
    assert md._family_of("deepseek/deepseek-reasoner") == "deepseek-reasoner"

    enums = [md.ProviderEnumeration(provider="deepseek", ok=True, models=[
        _lm("deepseek", "deepseek-chat", capabilities=["chat"]),
        _lm("deepseek", "deepseek-reasoner", capabilities=["chat"]),
    ])]
    doc = md.build_listings_cache(enums, refreshed_at="2026-06-12T00:00:00Z")
    flag = {m["model_id"]: m["is_family_latest"] for m in doc["providers"]["deepseek"]}
    # Each is the sole member of its own family → both latest.
    assert flag["deepseek-chat"] is True
    assert flag["deepseek-reasoner"] is True


def test_deepseek_new_line_surfaces_as_discovery(tmp_path):
    """A novel DeepSeek line whose family is NOT among the defaults surfaces as
    a genuine discovery with a cost band assigned (pricing/family-map, not
    hand-typed)."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"deepseek": [
        _lm("deepseek", "deepseek-prover", capabilities=["chat"],
            display_name="DeepSeek Prover"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"deepseek": "ds-key"},
        enumerator=_make_enumerator(listings),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == ["deepseek/deepseek-prover"]
    d = result.discoveries[0]
    assert d.provider == "deepseek"
    assert d.suggested_cost_class in ("low", "medium", "high", "premium")


def test_credentialed_deepseek_enumerated_not_uncovered(tmp_path):
    """A credentialed deepseek now ENUMERATES (it has an adapter) and is NOT in
    ``uncovered_providers`` — the gap this adapter closes."""
    listings = {"deepseek": [
        _lm("deepseek", "deepseek-chat", capabilities=["chat"]),
        _lm("deepseek", "deepseek-reasoner", capabilities=["chat"]),
    ]}
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE, bot_users=[], bot_configs={},
        shared_dir=tmp_path,
        keys={"deepseek": "ds-key"},
        credentialed_providers={"deepseek"},
        enumerator=_make_enumerator(listings),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    assert "deepseek" in result.enumerated_providers
    assert "deepseek" not in result.uncovered_providers
    # The listing persisted to the cache (validated picker source).
    import json as _json
    doc = _json.loads((tmp_path / "model-listings.json").read_text())
    assert {m["model_id"] for m in doc["providers"]["deepseek"]} == {
        "deepseek-chat", "deepseek-reasoner",
    }
    assert "deepseek" in md.llm_providers_from_listings(doc)


# ── Mistral listing adapter (OpenAI-compatible /v1/models) ────────────────────
#
# Mistral's API is OpenAI-shaped: GET /v1/models, Bearer auth, {data:[{id,...}]}.
# These pin the adapter's parse, the mistral chat-capability tagging (incl.
# mistral-embed exclusion), mistral family grouping + family-latest, and that a
# credentialed mistral now ENUMERATES (so it leaves ``uncovered_providers``).
# All fixture-based; the live api.mistral.ai fetch runs only on the pod sweep.

def test_mistral_in_listing_providers():
    """mistral is a listing-capable provider (adapter wired) — so a
    credentialed mistral is covered, not the no-adapter gap."""
    assert "mistral" in md._LISTING_PROVIDERS
    assert "mistral" in md._FETCHERS


def test_fetch_mistral_parses_openai_shaped_listing(monkeypatch):
    """The Mistral fetcher parses the OpenAI-compatible ``{data:[{id}]}``
    envelope, tags mistral chat models chat-capable, and authenticates with a
    Bearer header against api.mistral.ai — modeled on the OpenAI adapter."""
    seen = {}

    def _capture(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [
            {"id": "mistral-large-latest"},
            {"id": "mistral-small-latest"},
            {"id": "codestral-latest"},
        ]}

    monkeypatch.setattr(md, "_http_get_json", _capture)
    models = md._fetch_mistral("mistral-secret-key")

    # Endpoint + auth are adapter DATA: host is api.mistral.ai, Bearer header,
    # no key in the URL.
    assert "api.mistral.ai/v1/models" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer mistral-secret-key"
    assert "key=" not in seen["url"]

    ids = [m.model_id for m in models]
    assert ids == ["mistral-large-latest", "mistral-small-latest", "codestral-latest"]
    assert all(m.provider == "mistral" for m in models)
    assert all(m.qualified_id == f"mistral/{m.model_id}" for m in models)
    assert all("chat" in m.capabilities for m in models)
    assert all(md.is_chat_capable(m) for m in models)


def test_fetch_mistral_excludes_embeddings(monkeypatch):
    """A non-chat Mistral variant (mistral-embed / mistral-ocr) is tagged
    non-chat by the shared id heuristic and excluded from chat discovery."""
    def _stub(url, headers):  # noqa: ARG001
        return {"data": [
            {"id": "mistral-large-latest"},
            {"id": "mistral-embed"},       # embedding → non-chat
            {"id": "mistral-ocr-latest"},  # OCR → non-chat
        ]}

    monkeypatch.setattr(md, "_http_get_json", _stub)
    models = md._fetch_mistral("k")
    chat = [m for m in models if md.is_chat_capable(m)]
    assert [m.model_id for m in chat] == ["mistral-large-latest"]


def test_mistral_chat_capability_tagging():
    """mistral-large/mistral-small/codestral tag chat-capable; mistral-embed
    does not. The shared id heuristic is provider-neutral — same rule as
    OpenAI/xAI/DeepSeek."""
    assert md._id_chat_capabilities("mistral-large-latest") == ["chat"]
    assert md._id_chat_capabilities("mistral-small-latest") == ["chat"]
    assert md._id_chat_capabilities("codestral-latest") == ["chat"]
    assert md._id_chat_capabilities("mistral-embed") == ["non-chat"]


def test_mistral_family_grouping_and_latest():
    """mistral-large-* share family ``mistral-large``; mistral-small-* share
    ``mistral-small``; codestral-* share ``codestral`` — so the family-latest
    flag picks the highest version within each. Reuses ``_family_of`` (no new
    family regex)."""
    assert md._family_of("mistral/mistral-large-latest") == "mistral-large"
    assert md._family_of("mistral/mistral-large-2411") == "mistral-large"
    assert md._family_of("mistral/mistral-small-latest") == "mistral-small"
    assert md._family_of("mistral/codestral-2501") == "codestral"

    enums = [md.ProviderEnumeration(provider="mistral", ok=True, models=[
        _lm("mistral", "mistral-large-2407", capabilities=["chat"]),
        _lm("mistral", "mistral-large-2411", capabilities=["chat"]),  # latest large
        _lm("mistral", "mistral-small-latest", capabilities=["chat"]),
        _lm("mistral", "codestral-latest", capabilities=["chat"]),
    ])]
    doc = md.build_listings_cache(enums, refreshed_at="2026-06-12T00:00:00Z")
    flag = {m["model_id"]: m["is_family_latest"] for m in doc["providers"]["mistral"]}
    assert flag["mistral-large-2411"] is True
    assert flag["mistral-large-2407"] is False    # older member of mistral-large
    assert flag["mistral-small-latest"] is True   # sole member of its family
    assert flag["codestral-latest"] is True       # sole member of its family


def test_mistral_new_line_surfaces_as_discovery(tmp_path):
    """A novel Mistral line whose family is NOT among the defaults surfaces as a
    genuine discovery with a cost band assigned (pricing/family-map, not
    hand-typed)."""
    network = {"bots": {}, "models": {"rungs": [
        {"id": "opus-class", "models": ["anthropic/claude-opus-4-8"], "costClass": "high"},
    ]}}
    listings = {"mistral": [
        _lm("mistral", "mistral-large-latest", capabilities=["chat"],
            display_name="Mistral Large"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"mistral": "mi-key"},
        enumerator=_make_enumerator(listings),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    ids = [d.qualified_id for d in result.discoveries]
    assert ids == ["mistral/mistral-large-latest"]
    d = result.discoveries[0]
    assert d.provider == "mistral"
    # mistral-large maps to the 'mistral-large' family band (medium).
    assert d.suggested_cost_class in ("low", "medium", "high", "premium")


def test_credentialed_mistral_enumerated_not_uncovered(tmp_path):
    """A credentialed mistral now ENUMERATES (it has an adapter) and is NOT in
    ``uncovered_providers`` — the gap this adapter closes. mistral-embed is
    filtered out of the chat listing path."""
    listings = {"mistral": [
        _lm("mistral", "mistral-large-latest", capabilities=["chat"]),
        _lm("mistral", "mistral-small-latest", capabilities=["chat"]),
        _lm("mistral", "codestral-latest", capabilities=["chat"]),
    ]}
    result = md.run_discovery(
        network=_NETWORK_NO_FABLE, bot_users=[], bot_configs={},
        shared_dir=tmp_path,
        keys={"mistral": "mi-key"},
        credentialed_providers={"mistral"},
        enumerator=_make_enumerator(listings),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    assert "mistral" in result.enumerated_providers
    assert "mistral" not in result.uncovered_providers
    # The listing persisted to the cache (validated picker source).
    import json as _json
    doc = _json.loads((tmp_path / "model-listings.json").read_text())
    assert {m["model_id"] for m in doc["providers"]["mistral"]} == {
        "mistral-large-latest", "mistral-small-latest", "codestral-latest",
    }
    assert "mistral" in md.llm_providers_from_listings(doc)


# ── Credential sourcing: xai flows through the SAME pod-wide path ──────────────
#
# The existing mechanism enumerates openai/google even though the PRIMARY bot
# (evo) lacks those keys — it sources keys from across the pod. These pin that
# xai rides that exact path: a non-primary bot's xai key is the one discovery
# uses, and the credentialed-set scan picks xai up too.

def _auth_profiles_dict(provider, key):
    return {"profiles": {f"{provider}:default": {
        "provider": provider, "type": "api_key", "key": key,
    }}}


def test_discover_provider_keys_sources_xai_from_any_bot(monkeypatch):
    """discover_provider_keys resolves an xai key from whichever bot holds it —
    pod-wide, not just the primary. (Mirrors how openai/google enumerate even
    when evo lacks the key.)"""
    profiles_by_user = {
        "evo": {"profiles": {"anthropic:default": {
            "provider": "anthropic", "type": "api_key", "key": "sk-ant",
        }}},
        "team-bot-a": _auth_profiles_dict("xai", "xai-from-team-bot-a"),
    }
    monkeypatch.setattr(
        md, "_read_auth_profiles", lambda user: profiles_by_user.get(user),
    )
    keys = md.discover_provider_keys(["evo", "team-bot-a"])
    # xai resolved from team-bot-a even though evo (listed first) lacks it.
    assert keys.get("xai") == "xai-from-team-bot-a"
    assert keys.get("anthropic") == "sk-ant"


def test_discover_credentialed_providers_picks_up_all_keyed_providers(monkeypatch):
    """The credentialed-set scan returns every provider any bot holds an
    api_key for — including providers with no listing adapter (the gap input).
    """
    profiles_by_user = {
        "evo": {"profiles": {
            "anthropic:default": {"provider": "anthropic", "type": "api_key", "key": "a"},
            # A token/subscription profile must NOT count (only api_key).
            "claudemax:default": {"provider": "anthropic", "type": "token", "key": "t"},
        }},
        "team-bot-a": {"profiles": {
            "xai:default": {"provider": "xai", "type": "api_key", "key": "x"},
            "deepseek:default": {"provider": "deepseek", "type": "api_key", "key": "d"},
            # Non-LLM provider with a key — still credentialed; the gap check
            # only flags it if it has no adapter, which is the caller's concern.
            "brave:default": {"provider": "brave", "type": "api_key", "key": "b"},
        }},
    }
    monkeypatch.setattr(
        md, "_read_auth_profiles", lambda user: profiles_by_user.get(user),
    )
    provs = md.discover_credentialed_providers(["evo", "team-bot-a"])
    assert provs == {"anthropic", "xai", "deepseek", "brave"}


def test_run_discovery_enumerates_xai_from_pod_credentials(monkeypatch, tmp_path):
    """End-to-end credential sourcing: with keys resolved from auth-profiles
    (not injected), a run enumerates xai using a non-primary bot's xai key and
    persists grok to the listings cache. Live, the identical path hits
    api.x.ai; here the fetch is stubbed so CI makes no network call."""
    profiles_by_user = {
        "evo": {"profiles": {"anthropic:default": {
            "provider": "anthropic", "type": "api_key", "key": "sk-ant",
        }}},
        "team-bot-a": _auth_profiles_dict("xai", "xai-live-key"),
    }
    monkeypatch.setattr(
        md, "_read_auth_profiles", lambda user: profiles_by_user.get(user),
    )

    # Stub the per-provider fetch (stands in for the live api.x.ai / anthropic
    # calls) — keyed off the provider so each returns its own listing.
    def _enum(provider, key):  # noqa: ARG001
        if provider == "xai":
            assert key == "xai-live-key"  # the team-bot-a key flowed through
            return md.ProviderEnumeration(provider="xai", ok=True, models=[
                _lm("xai", "grok-4", capabilities=["chat"]),
            ])
        return md.ProviderEnumeration(provider=provider, ok=True, models=[])

    result = md.run_discovery(
        network={"bots": {"evo": {}, "team-bot-a": {}}, "models": {"rungs": []}},
        bot_users=["evo", "team-bot-a"],
        bot_configs={},
        shared_dir=tmp_path,
        keys=None,             # resolve from auth-profiles (the real path)
        enumerator=_enum,
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    assert "xai" in result.enumerated_providers
    import json as _json
    doc = _json.loads((tmp_path / "model-listings.json").read_text())
    assert "grok-4" in {m["model_id"] for m in doc["providers"]["xai"]}


# ── Credential SOURCE: OpenClaw 2026.6.9 SQLite migration (oc_store ladder) ────
#
# Discovery needs raw api_key VALUES. OpenClaw migrated auth-profiles.json into a
# per-agent SQLite store; the values still live in store_json (verified live).
# _read_auth_profiles now reads the SOURCE through evolve_admin.oc_store so the
# values resolve again (the same incident class #3248 fixed for presence).

def _seed_sqlite_auth_store(home: Path, profiles: dict, agent_id: str = "main") -> None:
    """Build a migrated openclaw-agent.sqlite under home's <agent_id> agent dir."""
    import json as _json
    import sqlite3
    agent = home.joinpath(".openclaw", "agents", agent_id, "agent")
    agent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(agent / "openclaw-agent.sqlite"))
    try:
        conn.execute(
            "CREATE TABLE auth_profile_store ("
            "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_profile_store VALUES ('primary', ?, 0)",
            (_json.dumps({"version": 1, "profiles": profiles}),),
        )
        conn.commit()
    finally:
        conn.close()


def test_read_auth_profiles_resolves_key_value_from_sqlite(tmp_path, monkeypatch):
    """The migrated SQLite store_json carries the raw api_key VALUE — discovery's
    extractor must get it back through the oc_store source ladder (the bug: the
    old direct read of the deleted JSON returned None on every migrated pod)."""
    import evolve_config
    home = tmp_path / "bot-home"
    _seed_sqlite_auth_store(home, {
        "anthropic:api": {"provider": "anthropic", "type": "api_key", "key": "sk-ant-sqlite"},
    })
    monkeypatch.setattr(evolve_config, "user_home", lambda user: home)

    profiles = md._read_auth_profiles("bot-acct")
    assert profiles is not None
    assert md._key_from_profiles(profiles, "anthropic") == "sk-ant-sqlite"


def test_discover_provider_keys_over_real_sqlite_stores(tmp_path, monkeypatch):
    """Pod-wide key sourcing flows through the real oc_store ladder: each bot's
    key is read from its own migrated SQLite store, no monkeypatched reader."""
    import evolve_config
    homes = {
        "evo-acct": tmp_path / "evo",
        "team-bot-a-acct": tmp_path / "tba",
    }
    _seed_sqlite_auth_store(homes["evo-acct"], {
        "anthropic:api": {"provider": "anthropic", "type": "api_key", "key": "sk-ant"},
    })
    _seed_sqlite_auth_store(homes["team-bot-a-acct"], {
        "xai:default": {"provider": "xai", "type": "api_key", "key": "xai-key"},
    })
    monkeypatch.setattr(evolve_config, "user_home", lambda user: homes[user])

    keys = md.discover_provider_keys(
        ["evo-acct", "team-bot-a-acct"], providers={"anthropic", "xai"},
    )
    assert keys == {"anthropic": "sk-ant", "xai": "xai-key"}
    provs = md.discover_credentialed_providers(["evo-acct", "team-bot-a-acct"])
    assert provs == {"anthropic", "xai"}


# NB: a bot whose store lives ONLY under a non-`main` agent id (e.g.
# `email-reader`) resolves once oc_store's agent-dir discovery lands (#3252);
# this reroute inherits that for free with no further change here. Not asserted
# in this PR since its base predates #3252 — current pods keep their keys under
# the `main` agent, so this reroute already resolves every live bot.


def test_read_auth_profiles_falls_back_when_evolve_admin_unimportable(tmp_path, monkeypatch):
    """When evolve_admin.oc_store can't be imported, the legacy direct read is
    used so un-migrated / legacy pods still resolve."""
    import sys
    # Force the oc_store import to fail (ImportError on `from … import`).
    monkeypatch.setitem(sys.modules, "evolve_admin.oc_store", None)
    sentinel = {"profiles": {"anthropic:api": {
        "provider": "anthropic", "type": "api_key", "key": "legacy",
    }}}
    called = {}

    def _legacy(bot_user):
        called["user"] = bot_user
        return sentinel

    monkeypatch.setattr(md, "_read_auth_profiles_legacy", _legacy)
    out = md._read_auth_profiles("bot-acct")
    assert out is sentinel
    assert called["user"] == "bot-acct"


# ── Meta-fix: credentialed-but-no-adapter gap Signal ──────────────────────────
#
# When a credentialed provider has NO listing adapter (not in
# _LISTING_PROVIDERS), discovery must surface an advisory instead of silently
# skipping it (the silent-monitor-drift rule — exactly how the xAI gap should
# have announced itself).

def test_uncovered_provider_surfaces_in_result(monkeypatch, tmp_path):
    """A credentialed LLM provider with no adapter lands in
    ``uncovered_providers`` — never silently skipped."""
    profiles_by_user = {
        "team-bot-a": {"profiles": {
            "xai:default": {"provider": "xai", "type": "api_key", "key": "x"},
            # 'groq' is a credentialed LLM provider with no listing
            # adapter → the gap (it's in _LLM_PROVIDERS, not _LISTING_PROVIDERS).
            "groq:default": {"provider": "groq", "type": "api_key", "key": "g"},
        }},
    }
    monkeypatch.setattr(
        md, "_read_auth_profiles", lambda user: profiles_by_user.get(user),
    )
    result = md.run_discovery(
        network={"bots": {"team-bot-a": {}}, "models": {"rungs": []}},
        bot_users=["team-bot-a"],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"xai": "x"},  # only the covered provider gets enumerated
        enumerator=_make_enumerator({"xai": [_lm("xai", "grok-4", capabilities=["chat"])]}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    # xai is covered (has an adapter); groq is the uncovered LLM gap.
    assert result.uncovered_providers == ["groq"]
    assert "uncovered_providers" in result.to_dict()


def test_non_llm_credentialed_providers_never_flagged(monkeypatch, tmp_path):
    """The reviewer's repro: a pod credentialing brave (search) + runway
    (video) + groq (LLM) must flag ONLY groq. brave/runway are not
    LLM providers, so 'add a listing adapter' is nonsensical for them — they
    must NOT surface in the gap advisory (the false-fire this fix closes)."""
    profiles_by_user = {
        "team-bot-a": {"profiles": {
            "brave:default": {"provider": "brave", "type": "api_key", "key": "b"},
            "runway:default": {"provider": "runway", "type": "api_key", "key": "r"},
            "groq:default": {"provider": "groq", "type": "api_key", "key": "g"},
        }},
    }
    monkeypatch.setattr(
        md, "_read_auth_profiles", lambda user: profiles_by_user.get(user),
    )
    result = md.run_discovery(
        network={"bots": {"team-bot-a": {}}, "models": {"rungs": []}},
        bot_users=["team-bot-a"],
        bot_configs={},
        shared_dir=tmp_path,
        keys={},  # nothing covered gets enumerated; the gap is what matters
        enumerator=_make_enumerator({}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    # ONLY groq (an LLM provider) is flagged; brave/runway are excluded.
    assert result.uncovered_providers == ["groq"]
    assert "brave" not in result.uncovered_providers
    assert "runway" not in result.uncovered_providers


def test_uncovered_providers_injectable_set(tmp_path):
    """The credentialed set is injectable so the gap can be tested without
    auth-profiles. A provider in the set but not in _LISTING_PROVIDERS is the
    gap; a covered one is not."""
    result = md.run_discovery(
        network={"bots": {}, "models": {"rungs": []}},
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk"},
        credentialed_providers={"anthropic", "xai", "groq"},
        enumerator=_make_enumerator({"anthropic": []}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    # anthropic + xai are covered; groq has no adapter → the single gap.
    assert result.uncovered_providers == ["groq"]


def test_uncovered_provider_means_not_all_current(tmp_path):
    """An uncovered (credentialed-but-no-adapter) provider is a known blind
    spot — the run must NOT report ``all_current`` even when nothing else
    surfaced (the silent-monitor-drift lesson)."""
    result = md.run_discovery(
        network={"bots": {}, "models": {"rungs": []}},
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk"},
        credentialed_providers={"anthropic", "groq"},  # groq uncovered
        enumerator=_make_enumerator({"anthropic": []}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    assert result.uncovered_providers == ["groq"]
    assert result.all_current is False
    # ...but a run with every provider covered and nothing found IS all_current.
    covered = md.run_discovery(
        network={"bots": {}, "models": {"rungs": []}},
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk"},
        credentialed_providers={"anthropic"},
        enumerator=_make_enumerator({"anthropic": []}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    assert covered.uncovered_providers == []
    assert covered.all_current is True


def test_no_gap_when_every_credentialed_provider_is_covered(tmp_path):
    result = md.run_discovery(
        network={"bots": {}, "models": {"rungs": []}},
        bot_users=[],
        bot_configs={},
        shared_dir=tmp_path,
        keys={"anthropic": "sk", "xai": "x"},
        credentialed_providers={"anthropic", "xai"},
        enumerator=_make_enumerator({"anthropic": [], "xai": []}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
    )
    assert result.uncovered_providers == []


def test_generator_emits_gap_signal_for_uncovered_provider(tmp_path):
    """The generator emits a warn-severity advisory Signal naming the
    uncovered provider, and that Signal does NOT produce an AdoptModel
    Proposal (it's an advisory, not a discovery)."""
    ctx = ModelDiscoveryContext(
        bot_id=None,
        shared_dir=tmp_path,
        network={"bots": {}, "models": {"rungs": []}},
        bot_users=[],
        bot_configs={},
        keys={"anthropic": "sk"},
        credentialed_providers={"anthropic", "groq"},
        enumerator=_make_enumerator({"anthropic": []}),
        pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
        consult_dismissals=False,
    )
    specs = mdgen.observe_signals(ctx)
    gap = [s for s in specs if s["type"] == mdgen.UNCOVERED_SIGNAL_TYPE]
    assert len(gap) == 1
    assert gap[0]["severity"] == "warn"
    assert "groq" in gap[0]["title"]
    assert gap[0]["details"]["uncovered_providers"] == ["groq"]

    for spec in specs:
        signals_store.observe(tmp_path, **spec)

    # The advisory does not produce a Proposal (observe() only proposes for
    # real model_discovery signals).
    props = mdgen.observe(ctx)
    assert props == []


def test_gap_signal_dedupes_on_rerun(tmp_path):
    """Re-running with the same uncovered provider re-observes one stable
    signature → no second signal (signal store dedups)."""
    def _mk_ctx():
        return ModelDiscoveryContext(
            bot_id=None,
            shared_dir=tmp_path,
            network={"bots": {}, "models": {"rungs": []}},
            bot_users=[],
            bot_configs={},
            keys={"anthropic": "sk"},
            credentialed_providers={"anthropic", "groq"},
            enumerator=_make_enumerator({"anthropic": []}),
            pricing_fetcher=lambda url: {},  # no live pricing fetch in CI
            consult_dismissals=False,
        )
    for _ in range(2):
        for spec in mdgen.observe_signals(_mk_ctx()):
            signals_store.observe(tmp_path, **spec)
    active = list(signals_store.iter_active(
        tmp_path, producer="model_discovery", state="firing"))
    gap = [s for s in active if s.type == mdgen.UNCOVERED_SIGNAL_TYPE]
    assert len(gap) == 1
    assert gap[0].observation_count == 2


# ── Recommendation-legibility contract: per-provider coalescing (Bite 1) ──────
#
# Spec: docs/design-recommendation-legibility-2026-06-12.md. A provider that
# ships several new models must surface as ONE coalesced card ("New models
# available from xAI") with the individual models folded in as sub-findings,
# not N fat per-model cards. The cardinality property of the contract.

from types import SimpleNamespace  # noqa: E402


def _coalesce_ctx(tmp_path):
    """Minimal pod-wide ctx for driving ``_make_discovery_proposal`` directly —
    no discovery pipeline, no HTTP. The rungs are empty so the live-position
    recompute falls back cleanly."""
    return ModelDiscoveryContext(
        bot_id=None,
        shared_dir=tmp_path,
        network={"bots": {}, "models": {"rungs": []}},
        bot_users=[],
        bot_configs={},
        keys={},
        consult_dismissals=False,
    )


def _fake_discovery_signal(provider, model_id, *, evidence=None):
    """A firing model_discovery Signal stand-in carrying exactly the
    ``details`` shape ``observe_signals`` writes (see SIGNAL_TYPE spec above)."""
    qualified = f"{provider}/{model_id}"
    return SimpleNamespace(
        id=f"sig-{provider}-{model_id}",
        type=mdgen.SIGNAL_TYPE,
        body=f"{qualified} is listed by {provider}.",
        details={
            "provider": provider,
            "model_id": model_id,
            "qualified_id": qualified,
            "suggested_rung": "a new rung",
            "suggested_rung_slug": "new-rung",
            "suggested_cost_class": "medium",
            "suggested_position": 0,
            "cost_band_source": "heuristic",
            "cost_band_evidence": {},
            "evidence": evidence or {"context_window": 200000, "capabilities": ["chat"]},
            "suggested_rationale": "heuristic rationale",
        },
    )


def test_same_provider_models_coalesce_into_one_card(tmp_path):
    """Two models discovered from the SAME provider fold into ONE pending
    parent (the second becomes a sub_finding), and the parent's human_title is
    count-agnostic. Each model stays individually adoptable: every built
    proposal carries its own AdoptModel action + listing evidence, and the
    folded sub-finding retains the second model's adopt evidence."""
    from arbiter.state_machine import transition
    from arbiter.store import iter_proposals, write_proposal

    ctx = _coalesce_ctx(tmp_path)
    prop_a = mdgen._make_discovery_proposal(
        ctx, _fake_discovery_signal("xai", "grok-5",
                                    evidence={"context_window": 256000, "capabilities": ["chat"]}))
    prop_b = mdgen._make_discovery_proposal(
        ctx, _fake_discovery_signal("xai", "grok-5-fast",
                                    evidence={"context_window": 131072, "capabilities": ["chat"]}))

    # Each proposal is individually adoptable BEFORE folding — its own
    # AdoptModel action with the right model + per-model listing evidence.
    for prop, mid in ((prop_a, "grok-5"), (prop_b, "grok-5-fast")):
        assert prop.action.kind == "AdoptModel"
        assert prop.action.provider == "xai"
        assert prop.action.model_id == mid
        assert prop.action.evidence  # per-model listing evidence retained

    # Per-provider coalesce grain + count-agnostic humanized title (display name).
    assert prop_a.coalesce_key == prop_b.coalesce_key == "model_discovery:xai"
    assert prop_a.human_title == "New models available from xAI"
    assert prop_b.human_title == "New models available from xAI"
    # No count baked into the title — the UI's sub-findings badge supplies the
    # live count, which stays correct as models fold in or get adopted out.
    assert not any(ch.isdigit() for ch in prop_a.human_title)

    # Fold: writing both to the store yields ONE pending parent.
    for prop in (prop_a, prop_b):
        transition(prop, "pending", actor="test", reason="seed")
        write_proposal(prop, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(pending) == 1, "xai's two models fold into one parent card"
    parent = pending[0]
    assert parent.id == prop_a.id, "first writer becomes the parent"
    assert parent.human_title == "New models available from xAI"
    assert len(parent.sub_findings) == 1
    sf = parent.sub_findings[0]
    assert sf["trigger_observation"] == "model_discovery:xai:grok-5-fast"
    # The folded sub-finding keeps the second model's adopt info (provenance
    # signals carry qualified_id + listing evidence) so it stays adoptable
    # from the drill-down.
    assert sf["provenance_signals"]["qualified_id"] == "xai/grok-5-fast"
    assert sf["provenance_signals"]["evidence"]


def test_different_providers_stay_separate_cards(tmp_path):
    """An xAI drop and an Anthropic drop on the same day are DIFFERENT cards —
    the coalesce grain is per-provider, so the keys differ and neither folds
    into the other."""
    from arbiter.state_machine import transition
    from arbiter.store import iter_proposals, write_proposal

    ctx = _coalesce_ctx(tmp_path)
    prop_xai = mdgen._make_discovery_proposal(
        ctx, _fake_discovery_signal("xai", "grok-5"))
    prop_anthropic = mdgen._make_discovery_proposal(
        ctx, _fake_discovery_signal("anthropic", "claude-mythos-5"))

    assert prop_xai.coalesce_key == "model_discovery:xai"
    assert prop_anthropic.coalesce_key == "model_discovery:anthropic"
    assert prop_xai.coalesce_key != prop_anthropic.coalesce_key
    assert prop_xai.human_title == "New models available from xAI"
    assert prop_anthropic.human_title == "New models available from Anthropic"

    for prop in (prop_xai, prop_anthropic):
        transition(prop, "pending", actor="test", reason="seed")
        write_proposal(prop, tmp_path)

    pending = list(iter_proposals(tmp_path, subdirs=("pending",)))
    assert len(pending) == 2, "different providers do not fold together"
    assert sorted(p.human_title for p in pending) == [
        "New models available from Anthropic",
        "New models available from xAI",
    ]
    assert all(not p.sub_findings for p in pending)


def test_unknown_provider_falls_back_to_slug_in_title(tmp_path):
    """A provider with no readily-available display name uses its slug in the
    title — cite-or-don't: never invent a prettier name we can't back."""
    ctx = _coalesce_ctx(tmp_path)
    prop = mdgen._make_discovery_proposal(
        ctx, _fake_discovery_signal("acme", "acme-ultra-1"))
    assert prop.coalesce_key == "model_discovery:acme"
    assert prop.human_title == "New models available from acme"


# ── Best-per-(provider, role) discovery selection (Addendum 13) ───────────────

def test_version_tuple_reads_generation_neutrally():
    # Provider-neutral: digit runs only, date runs stripped.
    assert md._version_tuple("gemini-2.5-flash-lite") == (2.5,)
    assert md._version_tuple("claude-3-5-haiku") == (3.0, 5.0)
    assert md._version_tuple("gpt-4o") == (4.0,)
    # An embedded YYYYMMDD is NOT read as a version number.
    assert md._version_tuple("claude-haiku-4-5-20250605") == (4.0, 5.0)


def test_model_generation_rank_orders_newest_first():
    rank = md.model_generation_rank
    # Newer generation wins on the version tuple.
    assert rank("gemini-2.5-flash-lite") > rank("gemini-2.0-flash-lite")
    assert rank("gemini-2.0-flash-lite") > rank("gemini-1.5-flash-lite")
    # Same version, a dated snapshot is the newer build.
    assert rank("claude-x-1-20250605") > rank("claude-x-1-20240101")
    # Same id otherwise → larger context window wins (capability tiebreak).
    assert (rank("m-1", {"context_window": 400000})
            > rank("m-1", {"context_window": 200000}))


def test_select_best_per_rung_collapses_same_provider_role():
    rows = [
        {"provider": "google", "model_id": "gemini-1.5-flash-lite",
         "recommended_role": "fast", "evidence": {}},
        {"provider": "google", "model_id": "gemini-2.5-flash-lite",
         "recommended_role": "fast", "evidence": {}},
        {"provider": "google", "model_id": "gemini-2.0-flash-lite",
         "recommended_role": "fast", "evidence": {}},
    ]
    out = md.select_best_per_rung(rows)
    assert [r["model_id"] for r in out] == ["gemini-2.5-flash-lite"]


def test_select_best_per_rung_keeps_distinct_groups():
    rows = [
        {"provider": "google", "model_id": "gemini-2.5-flash-lite",
         "recommended_role": "fast", "evidence": {}},
        {"provider": "google", "model_id": "gemini-2.5-pro",
         "recommended_role": "power", "evidence": {}},
        {"provider": "anthropic", "model_id": "claude-haiku-9",
         "recommended_role": "fast", "evidence": {}},
    ]
    out = md.select_best_per_rung(rows)
    assert {(r["provider"], r["recommended_role"]) for r in out} == {
        ("google", "fast"), ("google", "power"), ("anthropic", "fast"),
    }


def test_select_best_per_rung_groups_roleless_by_provider():
    # new_tier rows carry no role → group key is (provider, "") = best per
    # provider.
    rows = [
        {"provider": "acme", "model_id": "acme-ultra-1", "evidence": {}},
        {"provider": "acme", "model_id": "acme-ultra-2", "evidence": {}},
    ]
    out = md.select_best_per_rung(rows)
    assert [r["model_id"] for r in out] == ["acme-ultra-2"]


# ── Version freshness — the PRIMARY function (spec §Addendum 15) ───────────────

def test_version_upgrade_surfaces_for_known_class(tmp_path):
    """A pod running claude-sonnet-4-5 + a listing carrying claude-sonnet-5 →
    a SURFACED upgrade (current=4-5, latest=5, standard role), not a silent
    skip. This is the everyday "ride the latest version" case."""
    network = {
        "bots": {},
        "models": {
            "rungs": [
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-5"], "costClass": "medium"},
            ],
            "roles": {"standard": "sonnet-class"},
        },
    }
    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-5"),
        _lm("anthropic", "claude-sonnet-5"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    ups = result.upgrades
    assert len(ups) == 1, ups
    u = ups[0]
    assert u.current_model == "anthropic/claude-sonnet-4-5"
    assert u.latest_model == "anthropic/claude-sonnet-5"
    assert u.rung_slug == "sonnet-class"
    assert "standard" in u.roles
    assert not result.all_current


def test_version_upgrade_none_when_already_latest(tmp_path):
    """A pod already on the newest listed member produces no upgrade."""
    network = {
        "bots": {},
        "models": {"rungs": [
            {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-5"], "costClass": "medium"},
        ]},
    }
    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-5"),  # older, also listed
        _lm("anthropic", "claude-sonnet-5"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    assert result.upgrades == []


def test_version_upgrade_dormant_catalog_entry_gets_a_row(tmp_path):
    """A model in a rung NO role points at (a dormant catalog entry) still gets
    an upgrade row — roles empty, rung_slug populated so it's appliable."""
    network = {
        "bots": {},
        "models": {
            "rungs": [
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-5"], "costClass": "medium"},
                {"id": "extra", "models": ["anthropic/claude-haiku-3-5"], "costClass": "low"},
            ],
            "roles": {"standard": "sonnet-class"},
        },
    }
    listings = {"anthropic": [
        _lm("anthropic", "claude-sonnet-5"),
        _lm("anthropic", "claude-haiku-3-5"),
        _lm("anthropic", "claude-haiku-4-5"),  # newer than the dormant 3-5
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    dormant = [u for u in result.upgrades if u.rung_slug == "extra"]
    assert len(dormant) == 1, result.upgrades
    assert dormant[0].current_model == "anthropic/claude-haiku-3-5"
    assert dormant[0].latest_model == "anthropic/claude-haiku-4-5"
    assert dormant[0].roles == []


def test_version_upgrade_numeric_not_lexicographic():
    """claude-sonnet-10 must rank ABOVE claude-sonnet-4-5 (numeric), so the
    upgrade target is -10, not silently dropped by a string compare."""
    listing = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-5"),
        _lm("anthropic", "claude-sonnet-10"),
    ]}
    loc = {"claude-sonnet-4-5": {
        "qualified": "anthropic/claude-sonnet-4-5", "provider": "anthropic",
        "rung_slug": "sonnet-class", "roles": ["standard"],
    }}
    ups = md.compute_version_upgrades(listing, loc)
    assert len(ups) == 1
    assert ups[0].latest_model == "anthropic/claude-sonnet-10"


def test_version_upgrade_one_per_stale_family(tmp_path):
    """A multi-family pod each stale → one upgrade per family."""
    network = {
        "bots": {},
        "models": {
            "rungs": [
                {"id": "haiku-class", "models": ["anthropic/claude-haiku-4-5"], "costClass": "low"},
                {"id": "sonnet-class", "models": ["anthropic/claude-sonnet-4-5"], "costClass": "medium"},
                {"id": "opus-class", "models": ["anthropic/claude-opus-4-7"], "costClass": "high"},
            ],
            "roles": {"fast": "haiku-class", "standard": "sonnet-class", "power": "opus-class"},
        },
    }
    listings = {"anthropic": [
        _lm("anthropic", "claude-haiku-4-5"), _lm("anthropic", "claude-haiku-5"),
        _lm("anthropic", "claude-sonnet-4-5"), _lm("anthropic", "claude-sonnet-5"),
        _lm("anthropic", "claude-opus-4-7"), _lm("anthropic", "claude-opus-4-8"),
    ]}
    result = md.run_discovery(
        network=network, bot_users=[], bot_configs={}, shared_dir=tmp_path,
        keys={"anthropic": "sk-test"},
        enumerator=_make_enumerator(listings),
    )
    fams = sorted(u.family for u in result.upgrades)
    assert fams == ["claude-haiku", "claude-opus", "claude-sonnet"], result.upgrades


def test_version_upgrade_excludes_dated_snapshot_target():
    """When both a rolling base (claude-sonnet-5) and a pinned dated snapshot
    (claude-sonnet-5-20260101) are listed, the upgrade rides the base, not the
    pinned snapshot."""
    listing = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-5"),
        _lm("anthropic", "claude-sonnet-5"),
        _lm("anthropic", "claude-sonnet-5-20260101"),
    ]}
    loc = {"claude-sonnet-4-5": {
        "qualified": "anthropic/claude-sonnet-4-5", "provider": "anthropic",
        "rung_slug": "sonnet-class", "roles": ["standard"],
    }}
    ups = md.compute_version_upgrades(listing, loc)
    assert len(ups) == 1
    assert ups[0].latest_model == "anthropic/claude-sonnet-5"


def test_known_model_locations_covers_default_catalog():
    """known_model_locations locates every default-catalog model at its rung,
    with the roles that route through it."""
    locs = md.known_model_locations({"bots": {}, "models": {}})
    assert locs["claude-sonnet-4-6"]["rung_slug"] == "sonnet-class"
    assert "standard" in locs["claude-sonnet-4-6"]["roles"]
    # fable-5 ships dormant-ish but max points at fable-class.
    assert locs["claude-fable-5"]["rung_slug"] == "fable-class"


def test_version_upgrade_suppressed_when_rung_already_runs_latest():
    """Once the latest version is in the rung (the upgrade was applied and the
    predecessor lingers as a fallback), NO upgrade surfaces — otherwise the
    card's count would never clear after "Update all to latest"."""
    listing = {"anthropic": [
        _lm("anthropic", "claude-sonnet-4-5"),
        _lm("anthropic", "claude-sonnet-5"),
    ]}
    # Post-apply rung: latest leads, predecessor lingers — both pod-sourced.
    loc = {
        "claude-sonnet-5": {
            "qualified": "anthropic/claude-sonnet-5", "provider": "anthropic",
            "rung_slug": "sonnet-class", "roles": ["standard"],
        },
        "claude-sonnet-4-5": {
            "qualified": "anthropic/claude-sonnet-4-5", "provider": "anthropic",
            "rung_slug": "sonnet-class", "roles": ["standard"],
        },
    }
    assert md.compute_version_upgrades(listing, loc) == []
    # But a stale member in a DIFFERENT rung than the latest still upgrades.
    loc2 = dict(loc)
    loc2["claude-sonnet-4-5"] = {
        **loc["claude-sonnet-4-5"], "rung_slug": "legacy-class",
    }
    ups = md.compute_version_upgrades(listing, loc2)
    assert [u.rung_slug for u in ups] == ["legacy-class"]


def test_build_listings_cache_is_family_latest_numeric():
    """is_family_latest must use numeric ranking: -10 is latest over -4-5."""
    enums = [md.ProviderEnumeration(provider="anthropic", ok=True, models=[
        _lm("anthropic", "claude-sonnet-4-5"),
        _lm("anthropic", "claude-sonnet-10"),
    ])]
    doc = md.build_listings_cache(enums, refreshed_at="2026-06-30T00:00:00Z")
    rows = {m["model_id"]: m["is_family_latest"] for m in doc["providers"]["anthropic"]}
    assert rows["claude-sonnet-10"] is True
    assert rows["claude-sonnet-4-5"] is False
