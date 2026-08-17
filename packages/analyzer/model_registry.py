"""
model_registry.py — Per-(provider × tier) recommendations, sourced from OC.

Tier semantics match models.py (role equivalents in parens):
  tier0  Cross-model judge (judge).   Different provider than tier2.
  tier1  Power (power).               Top-of-line, expensive, rare use.
  tier2  Workhorse (standard).        Default for user-facing conversations.
  tier3  Grunt (fast).                Cheap+fast for background tasks.

PHASE 4 (spec-model-rungs-and-roles-2026-06-09 §"Freshness check rework",
SHIPPED): the discovery-based freshness check now lives in
:mod:`model_discovery` (enumerate live provider listings → diff against the
pod's rungs → propose). ``RECOMMENDED`` below is **demoted to an offline
fallback** — it is the source of truth for *nothing* about the live market.
It is consulted only:

  - by :func:`resolve_current_model` / :func:`check_bot_freshness` (the
    legacy list-matching advisory path, still wired to the on-demand
    ``/api/models/check-freshness`` endpoint), and
  - by :mod:`model_discovery` when a provider's listing call fails, so the
    pod still has *some* recommendation rather than going blind — but that
    path is always flagged **degraded** ("discovery skipped for <provider>"),
    never reported as "all current".

``RECOMMENDED`` is intentionally **kept, not deleted**: it is the offline
floor for any credentialed provider whose live listing call fails (the
degraded path), and the static fallback when OC exposes no alias for a
(provider, tier) cell (e.g. xAI has no aliases in OC v2026.6.1, so
``resolve_current_model`` falls back here for the xai rows). xAI now HAS a
listing adapter in ``model_discovery`` (``api.x.ai/v1/models``), so the
discovery freshness path enumerates it like the other providers — the dict is
no longer the *only* freshness signal for xAI, just its OC-alias fallback. The
day every credentialed provider has both a listing adapter and OC alias
coverage, deleting this dict becomes safe; until then it is the degraded-mode
floor. Do not rename the tier keys here — they map to roles via
``models.TIER_TO_ROLE``.

The cardinal rule: no code path may report "current" from this dict alone
without flagging that discovery was skipped. Silence and "all current" must
not be indistinguishable (the silent-monitor-drift lesson).

Two sources, in order:

  1. **Live OC signals** (preferred). :mod:`tier_resolver` consults
     :mod:`oc_introspection`, which parses OC's bundled JS files
     (``DEFAULT_MODEL_ALIASES``, ``OPENAI_DEFAULT_MODEL``,
     ``upgradeRetiredOpenAiModelId``, etc.). Recommendations track
     OC release upgrades automatically — no Evolve redeploy needed.

  2. **Static RECOMMENDED below** (graceful fallback). When the OC
     install isn't readable or doesn't yet expose a signal for the
     (provider, tier) cell (e.g., xAI has no aliases in OC v2026.6.1),
     ``resolve_current_model`` falls back to this dict.

Historical note: through Q2-2026 the *static* dict was the only source.
It rotted between Evolve releases and caused a real production bug —
the freshness check kept pushing operators onto OC-retired model names
(``gpt-4o``), OC silently migrated them away, and the catalog drift
advisory looped. The OC-introspection path closes that loop because
OC's own retirement map is now the gate.

Update path for the static fallback: edit RECOMMENDED below when a
provider Evolve cares about lacks OC alias/default coverage. Don't
edit it for the openai/anthropic/google common cases — those resolve
from OC at runtime, and editing here just creates drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Canonical recommendations ─────────────────────────────────────────────────
# Format: model IDs are "{provider}/{model-name}", matching evolve-tiers.json.
# Update RECOMMENDED when a new model ships; the released date is informational
# (shown in the UI as "available since YYYY-MM-DD").

RECOMMENDED: dict[str, dict[str, dict[str, str]]] = {
    "anthropic": {
        "tier1": {"model": "anthropic/claude-opus-4-7", "released": "2026-04-22"},
        "tier2": {"model": "anthropic/claude-sonnet-4-6", "released": "2026-04-15"},
        "tier3": {"model": "anthropic/claude-haiku-4-5", "released": "2025-10-01"},
    },
    # Fallback values mirror OC v2026.6.1's bundled signals — kept in
    # sync so a missing OC install (test env, dev sandbox) still
    # produces sane recommendations rather than pushing operators
    # backward to gpt-4o (which OC has retired). The structural source
    # of truth is the live introspection in tier_resolver.
    "openai": {
        "tier0": {"model": "openai/gpt-4.1", "released": "2025-04-14"},
        "tier1": {"model": "openai/gpt-4.1", "released": "2025-04-14"},
        "tier2": {"model": "openai/gpt-4.1", "released": "2025-04-14"},
        "tier3": {"model": "openai/gpt-4.1-mini", "released": "2025-04-14"},
    },
    "google": {
        "tier1": {"model": "google/gemini-2.5-pro", "released": "2025-12-10"},
        "tier2": {"model": "google/gemini-2.5-pro", "released": "2025-12-10"},
        "tier3": {"model": "google/gemini-2.0-flash", "released": "2025-02-05"},
    },
    "xai": {
        "tier1": {"model": "xai/grok-4", "released": "2026-02-18"},
        "tier2": {"model": "xai/grok-4", "released": "2026-02-18"},
        "tier3": {"model": "xai/grok-4-mini", "released": "2026-02-18"},
    },
    # Moonshot (Kimi) — OpenAI-compatible API. kimi-k3 is the flagship
    # (1M context); kimi-k2.6 is the cheap agentic workhorse.
    "moonshot": {
        "tier1": {"model": "moonshot/kimi-k3", "released": "2026-07-16"},
        "tier2": {"model": "moonshot/kimi-k3", "released": "2026-07-16"},
        "tier3": {"model": "moonshot/kimi-k2.6", "released": "2026-04-20"},
    },
}


# ── Model + advisory dataclasses ──────────────────────────────────────────────

@dataclass
class ModelAdvisory:
    """One recommendation: bot X's tier Y is using model A, current is B.

    Two reasons we emit one:

    - ``is_stale=True`` and ``is_retired=False`` — the configured model
      is fine to run (still accepted by the provider) but a newer
      Evolve/OC-recommended model exists. Soft upgrade.

    - ``is_retired=True`` — the configured model is in OC's retirement
      map. OC will silently migrate routing requests to the
      replacement; meanwhile, the catalog and tier definitions drift
      apart on every gateway restart (the bug that surfaced this whole
      rework). Operator should update the tier explicitly.

    ``source`` and ``oc_version`` carry provenance for the UI — so an
    operator can see "this came from OC v2026.6.1's alias map" rather
    than wondering whether the recommendation is hand-curated.
    """

    bot_id: str
    tier: str
    provider: str
    current_model: str | None      # what's configured (may be None if tier empty)
    recommended_model: str         # what we'd put there
    recommended_released: str      # ISO date — empty when sourced from OC
    is_stale: bool                 # True if current != recommended
    is_retired: bool = False       # True when OC's retirement map flags current
    source: str = "static"         # "oc_alias" | "oc_default" | "oc_retirement" | "static"
    oc_version: str | None = None  # OC version when source is OC-derived

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderDiversityAdvisory:
    """Soft recommendation: bot X has only one credentialed LLM provider.

    Fired when a bot's auth-profiles holds keys for exactly one of the
    LLM providers tracked in RECOMMENDED. The advisory suggests adding
    a second provider so the bot has (a) a fallback when the primary
    is rate-limited or down, and (b) a cross-vendor option for the
    judge tier (tier0 wants a different vendor than tier2).

    Dismissible: the operator may decide a single-provider bot is
    fine (cost, simplicity, deliberate). Dismissals are persisted
    per-bot in {shared_dir}/model-freshness/dismissals.json.
    """

    bot_id: str
    current_providers: list[str]    # the (one) provider(s) the bot has
    suggested_providers: list[str]  # registry providers not yet credentialed
    reasons: list[str]              # ["fallback", "judge"] — for UI copy

    def to_dict(self) -> dict:
        return asdict(self)


# ── Freshness checker ─────────────────────────────────────────────────────────

def _provider_of(model_id: str) -> str | None:
    """Extract provider from a model ID like 'anthropic/claude-sonnet-4-6'."""
    if not model_id or "/" not in model_id:
        return None
    return model_id.split("/", 1)[0].lower()


def _normalize_model(model_id: str) -> str:
    """Lowercase + strip whitespace for comparison."""
    return (model_id or "").strip().lower()


def resolve_current_model(
    provider: str,
    tier: str,
    *,
    oc_signals: "Any | None" = None,
) -> tuple[str, str, str | None, str]:
    """Resolve the current recommended model for (provider, tier).

    Returns ``(model_id, source, oc_version, released_iso)``:

      - ``model_id`` — the recommendation (provider-qualified).
      - ``source`` — ``"oc_alias" | "oc_default" | "static"``. Names
        which signal layer produced the answer so the UI can surface
        provenance.
      - ``oc_version`` — OC version string when sourced from OC,
        ``None`` for the static fallback.
      - ``released_iso`` — ISO date for static entries (informational);
        empty string when sourced from OC (no per-model release date
        on the OC side).

    Resolution order: live OC signals (via :mod:`tier_resolver`) first,
    then the static :data:`RECOMMENDED` fallback. Returns the static
    fallback's recommendation when OC has no rule for the cell — that's
    the graceful-degrade for providers OC hasn't tagged yet (xAI as of
    v2026.6.1, anthropic tier3, openai tier0 are the current gaps).

    Raises ``KeyError`` only when BOTH layers are empty for the cell —
    callers can treat that as "we have no opinion for this provider/tier."
    """
    # Lazy import — tier_resolver imports oc_introspection which we
    # don't want at module load (keeps the import surface light for
    # tests that don't exercise the resolver path).
    try:
        from tier_resolver import resolve_tier_model
        resolved = resolve_tier_model(provider, tier, signals=oc_signals)
    except Exception:
        # Any failure in the OC-introspection path (dist files
        # unreadable, regex didn't match a future OC layout, etc.) is
        # treated the same as "no live signal" — fall through to static.
        resolved = None

    if resolved is not None:
        oc_source_map = {
            "default_constant": "oc_default",
            "alias": "oc_alias",
            "retirement_redirect": "oc_retirement",
        }
        source = oc_source_map.get(resolved.source, "oc_" + resolved.source)
        return resolved.model_id, source, resolved.oc_version, ""

    rec = RECOMMENDED.get(provider, {}).get(tier)
    if rec is None:
        raise KeyError(f"no recommendation for {provider}/{tier}")
    return rec["model"], "static", None, rec.get("released", "")


def check_bot_freshness(
    bot_id: str,
    bot_tiers: dict[str, dict],
    available_providers: set[str],
) -> list[ModelAdvisory]:
    """Compare one bot's tier config against the current recommendation.

    Args:
        bot_id: bot identifier for the advisory.
        bot_tiers: the tier dict from evolve-tiers.json — {tier_id: {models: [...]}}.
            May be empty/missing — bots that have never had their tiers configured
            still need advisories so the user can populate them via the Update button.
        available_providers: providers the pod has API keys for. Used to filter
            advisories — no point telling the user "switch to xai" if no xai key.

    Returns one :class:`ModelAdvisory` per (tier × provider) where:

      - the pod has a key for that provider, AND
      - either OC's signals or the static fallback names a recommendation
        for that (provider, tier), AND
      - the bot's tier doesn't already use it OR the tier names a
        retired model (OC's retirement map is the authority).

    The retirement check is the load-bearing piece — without it, an
    operator whose tier explicitly names ``openai/gpt-4o`` gets caught
    in the OC ``legacy-config-migrations`` prune loop documented in the
    2026-06-07 incident. Tagging ``is_retired=True`` lets the UI
    surface that advisory differently (urgent, with the OC-mandated
    replacement) instead of mixing it in with soft upgrade prompts.
    """
    # Lazy: only load signals when we'll actually consult them.
    from tier_resolver import is_retired, covered_tiers
    from oc_introspection import load_oc_signals
    oc_signals = load_oc_signals()

    advisories: list[ModelAdvisory] = []
    bot_tiers = bot_tiers or {}

    # The set of (provider, tier) cells we'll evaluate is the union of
    # OC-covered cells AND static RECOMMENDED cells, intersected with
    # the providers the pod has keys for. Using the union lets new
    # OC-covered cells produce advisories even when they aren't in the
    # static fallback (e.g., a provider OC starts aliasing but Evolve
    # hasn't manually listed yet).
    pairs_to_check: set[tuple[str, str]] = set()
    for provider in available_providers:
        for tier_id in RECOMMENDED.get(provider, {}):
            pairs_to_check.add((provider, tier_id))
        for tier_id in covered_tiers(provider):
            pairs_to_check.add((provider, tier_id))

    for provider, tier_id in sorted(pairs_to_check):
        try:
            recommended, source, oc_version, released = resolve_current_model(
                provider, tier_id, oc_signals=oc_signals,
            )
        except KeyError:
            continue  # neither layer has an opinion

        configured_models = (bot_tiers.get(tier_id) or {}).get("models") or []

        # Find the first model in this tier that comes from the same provider
        # (the tier may list models from multiple providers; advise per-provider).
        current: str | None = None
        for m in configured_models:
            if _provider_of(m) == provider:
                current = m
                break

        # Retirement check — wins over "stale vs recommended" because
        # retirement is the failure mode that creates the catalog
        # reconcile loop. When OC's retirement map fires, the
        # replacement it names IS the recommendation, regardless of
        # what our policy resolved.
        retired = False
        if current:
            retired_flag, oc_replacement = is_retired(provider, current, oc_signals)
            if retired_flag:
                retired = True
                if oc_replacement:
                    recommended = oc_replacement
                    source = "oc_retirement"
                    oc_version = oc_signals.oc_version

        is_stale = (
            current is None
            or _normalize_model(current) != _normalize_model(recommended)
        )

        if is_stale or retired:
            advisories.append(
                ModelAdvisory(
                    bot_id=bot_id,
                    tier=tier_id,
                    provider=provider,
                    current_model=current,
                    recommended_model=recommended,
                    recommended_released=released,
                    is_stale=is_stale,
                    is_retired=retired,
                    source=source,
                    oc_version=oc_version,
                )
            )

    return advisories


def check_bot_diversity(
    bot_id: str,
    credentialed_providers: set[str],
) -> ProviderDiversityAdvisory | None:
    """Soft check: does this bot have at least 2 LLM providers credentialed?

    Considers only providers tracked in RECOMMENDED (anthropic, openai,
    google, xai) — non-LLM credentials (telegram, slack, brave) don't
    count toward diversity.

    Returns None when:
      - the bot has 2+ tracked providers (no advisory needed), OR
      - the bot has 0 tracked providers (no LLM at all — that's a
        different problem the existing freshness check already
        surfaces by listing every tier as missing).

    Returns a ProviderDiversityAdvisory when exactly 1 tracked
    provider is credentialed. The `suggested_providers` list is
    every other registry provider, ordered so the cross-vendor
    suggestion comes first (judge benefits from independence).
    """
    tracked = set(RECOMMENDED.keys())
    current = sorted(p for p in credentialed_providers if p in tracked)

    if len(current) != 1:
        return None

    suggested = sorted(p for p in tracked if p not in current)
    return ProviderDiversityAdvisory(
        bot_id=bot_id,
        current_providers=current,
        suggested_providers=suggested,
        reasons=["fallback", "judge"],
    )


def check_pod_freshness(
    bot_configs: dict[str, dict],
    available_providers: set[str],
) -> list[ModelAdvisory]:
    """Check freshness across every bot in the pod.

    Args:
        bot_configs: {bot_id: full evolve config dict from oc_full_config_get},
            i.e. each value has a "tiers" key.
        available_providers: providers the pod has API keys for.
    """
    all_advisories: list[ModelAdvisory] = []
    for bot_id, cfg in (bot_configs or {}).items():
        if not cfg:
            continue
        bot_tiers = cfg.get("tiers") or {}
        all_advisories.extend(
            check_bot_freshness(bot_id, bot_tiers, available_providers)
        )
    return all_advisories


# ── Last-checked timestamp persistence ────────────────────────────────────────
# A small JSON file under shared_dir/model-freshness/last-check.json so the
# dashboard can show "Last checked: 2026-05-04 02:14 UTC — all current"
# across page reloads and admin restarts.

_FRESHNESS_DIRNAME = "model-freshness"
_LAST_CHECK_FILENAME = "last-check.json"


def _last_check_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / _FRESHNESS_DIRNAME / _LAST_CHECK_FILENAME


def load_last_check(shared_dir: Path) -> dict[str, Any]:
    """Return the most recent freshness-check summary, or {} if never run."""
    path = _last_check_path(shared_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_last_check(
    shared_dir: Path,
    advisories: list[ModelAdvisory],
    available_providers: set[str],
    diversity_advisories: list[ProviderDiversityAdvisory] | None = None,
    discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a freshness-check summary. Returns the saved dict.

    `diversity_advisories` is optional so existing test call sites that
    only pass tier advisories continue to work. When provided, every
    advisory is saved unfiltered — dismissal filtering is applied at
    read time by the endpoint.

    `discovery` is the Phase 4 discovery-based result (live-listing diff +
    degraded-provider markers). Persisted so the GET /freshness-status
    surface can show discoveries + the "discovery skipped for <provider>"
    degraded markers across reloads, not just the POST response.
    """
    summary = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "advisory_count": len(advisories),
        "providers_checked": sorted(available_providers),
        "advisories": [a.to_dict() for a in advisories],
        "diversity_advisories": [
            a.to_dict() for a in (diversity_advisories or [])
        ],
    }
    if discovery is not None:
        summary["discovery"] = discovery
    path = _last_check_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2))
    tmp.replace(path)
    return summary


# ── Dismissals (soft-advisory acknowledgements) ───────────────────────────────
# Per-bot, permanent until reset. Stored at
# {shared_dir}/model-freshness/dismissals.json:
#   {
#     "diversity": {
#       "evo": "2026-06-01T12:34:56Z",
#       "team-bot-a": "2026-06-01T12:35:01Z"
#     }
#   }
# The diversity advisory is dismissed per-bot; if the operator later
# adds a second provider via Add Key (or copy-from-bot) the advisory
# naturally stops firing — no need to clear the dismissal.

_DISMISSALS_FILENAME = "dismissals.json"


def _dismissals_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / _FRESHNESS_DIRNAME / _DISMISSALS_FILENAME


def load_dismissals(shared_dir: Path) -> dict[str, dict[str, str]]:
    """Return the full dismissals doc, or {} if no file."""
    path = _dismissals_path(shared_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_dismissals(shared_dir: Path, data: dict) -> None:
    path = _dismissals_path(shared_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def dismiss_advisory(
    shared_dir: Path, advisory_type: str, key: str,
) -> dict[str, dict[str, str]]:
    """Record a dismissal for (advisory_type, key). Returns the updated doc."""
    data = load_dismissals(shared_dir)
    bucket = data.setdefault(advisory_type, {})
    if isinstance(bucket, dict):
        bucket[key] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_dismissals(shared_dir, data)
    return data


def reset_dismissals(
    shared_dir: Path, advisory_type: str | None = None,
) -> dict[str, dict[str, str]]:
    """Reset dismissals — either a single type or all of them."""
    if advisory_type is None:
        _write_dismissals(shared_dir, {})
        return {}
    data = load_dismissals(shared_dir)
    data.pop(advisory_type, None)
    _write_dismissals(shared_dir, data)
    return data


def is_dismissed(
    shared_dir: Path, advisory_type: str, key: str,
) -> bool:
    """Convenience: has (advisory_type, key) been dismissed?"""
    data = load_dismissals(shared_dir)
    bucket = data.get(advisory_type) or {}
    return isinstance(bucket, dict) and key in bucket


# ── Helpers used by the API layer ─────────────────────────────────────────────

def providers_with_recommendations() -> list[str]:
    """All providers the registry knows about (used for UI legend)."""
    return sorted(RECOMMENDED.keys())


def recommendation_for(provider: str, tier: str) -> dict[str, str] | None:
    """Convenience accessor; returns {model, released} or None."""
    return RECOMMENDED.get(provider, {}).get(tier)
