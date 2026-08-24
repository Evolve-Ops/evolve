"""model_discovery — discovery-based model freshness (Phase 4).

Spec: internal/spec-model-rungs-and-roles-2026-06-09.md §"Freshness check rework".

The old freshness check (``model_registry.check_bot_freshness``) iterated the
hardcoded ``RECOMMENDED`` dict — "current" meant "matches our hand-edited
list," which is circular: a brand-new model *line* (Fable) was structurally
invisible until a human edited the dict. This module replaces list-matching
with **discovery**:

  1. **Enumerate** — for each credentialed provider, fetch the live model
     listing over plain HTTP against the key the pod already holds. No LLM,
     no web search. One listing call per provider per run.
  2. **Diff** — partition the listing against the pod's known model set
     (``models.rungs[].models`` plus every bot's legacy ``tiers.*.models``):
       - **known**     — already in some rung/tier: within-family staleness
                         derived from the *listing*, not the dict.
       - **unknown**   — chat-capable, in no rung, not ignore-listed, AND
                         on the **frontier** (its family has no known member,
                         i.e. a genuinely new capability *line* like Fable):
                         a **discovery finding** (the class that catches Fable).
       - **out-of-scope** — embeddings/audio/specialty models (by capability
                         metadata); dated snapshot aliases (of a known model OR
                         of any base id that itself appears in the listing); and
                         **non-frontier** unknowns — an unknown member of an
                         *already-adopted* family (e.g. a stale ``opus-4-7`` when
                         the pod runs ``opus-4-8``). These are NOT discoveries.

The **discovery-vs-version-upgrade split** is the rule this filter enforces: a
model surfaces through exactly ONE channel. If its family is already adopted,
a NEWER listed member is a *version upgrade* of the known model (the everyday
"ride the latest version of the model class you already chose" nudge — see
``compute_version_upgrades`` / the ``VersionUpgrade`` first-class result) — it is
never ALSO a discovery; and an OLDER member is just stale catalog noise
(skipped). Discovery is reserved for families the pod has no member of at all —
the occasional "new model *line*" (Fable) case. A model whose name does not
parse into a family at all fails OPEN to discovery (visibility for genuinely
alien names like ``o3`` or ``grok-4``).

**Version freshness is the PRIMARY function** (spec §Addendum 15). The "Check
Now" / Model Freshness card's #1 job is to bring every model the pod already
runs up to the latest version of its class (Sonnet 4-5 → Sonnet 5, Opus 4-7 →
4-8). ``compute_version_upgrades`` is the deterministic pass that powers it,
keyed off ``known_model_locations`` (every model the pod runs + the rung it
occupies + the roles it serves). New-line discovery is the secondary case.
The old ``StalenessFinding`` was the (dead-ended, never-consumed) precursor to
``VersionUpgrade``; the latter carries the rung/role location an upgrade needs
to be one-click-appliable via the ``AdoptModel`` applier.
  3. **Propose, never auto-categorize** — discovery emits a Signal
     (``type: model_discovery``) and the generator turns it into a Proposal.
     Accepting the proposal is the operator action that edits the catalog;
     this module NEVER writes config.

The cardinal rule (silent-monitor-drift lesson): a swallowed listing
exception must NEVER yield "all current." Every provider whose listing call
fails is reported as ``degraded`` with a reason; the check result and Signal
say "discovery skipped for <provider>: <reason>," never a bare "all current."

``RECOMMENDED`` (``model_registry``) is demoted to an offline fallback used
*only* when a provider's listing call fails — and that path is always flagged
degraded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import model_pricing  # pricing-catalog mirror (Addendum 8 §B) — data layer.
import model_cost_bands  # pricing → cost band (Addendum 8 §C) — band layer.

_log = logging.getLogger(__name__)


# ── Provider listing endpoints ────────────────────────────────────────────────
# Each provider entry: how to build the request + how to parse the response
# into a list of normalized model records. Pure HTTP, short timeouts.

_HTTP_TIMEOUT_SECONDS = 12

# Providers we know how to enumerate. A credentialed provider NOT in this set
# has no listing adapter — discovery cannot enumerate it, so it never reaches
# the validated picker / easy-setup filter. Such a provider is surfaced as an
# advisory (the credentialed-but-no-adapter gap Signal — see
# ``uncovered_providers`` in ``run_discovery``), NEVER silently skipped (the
# silent-monitor-drift lesson). To add a provider: add its name here AND wire a
# fetcher into ``_FETCHERS`` — both are adapter DATA, not routing logic.
_LISTING_PROVIDERS = {"anthropic", "openai", "google", "xai", "deepseek", "mistral", "moonshot"}

# The providers Evolve categorizes as LLM (chat-capable) providers. This is the
# DATA mirror of the ``"llm"`` rows in the admin key-registry
# (``_KEY_REGISTRY`` in ``routes_admin.py``) — the same categorization the admin
# UI uses to group "LLM PROVIDERS" apart from Messaging / Media / Search. It is
# the AUTHORITATIVE set for "is this an LLM provider?", broader than the catalog
# clusters on purpose: a freshly-credentialed LLM provider (DeepSeek, Mistral,
# Groq …) is an LLM provider even before it appears in ``DEFAULT_MODEL_CATALOG``
# or gets a listing adapter — which is exactly the credentialed-but-no-adapter
# gap this set scopes (see ``uncovered_providers`` in ``run_discovery``). Non-LLM
# credentialed providers (brave/runway/elevenlabs/…) are deliberately absent, so
# they never false-fire the gap advisory. Keep in sync with the ``"llm"`` rows of
# ``_KEY_REGISTRY``.
# provider-literal-allow-begin: LLM-provider categorization DATA (home #1) —
# mirrors the ``"llm"`` category of routes_admin._KEY_REGISTRY.
_LLM_PROVIDERS = {
    "anthropic",
    "openai",
    "google",
    "xai",
    "mistral",
    "groq",
    "perplexity",
    "together",
    "deepseek",
    "cohere",
    "moonshot",
}
# provider-literal-allow-end


# ── Normalized listing record ─────────────────────────────────────────────────

@dataclass
class ListedModel:
    """One model as reported by a provider's live listing.

    ``provider``/``model_id`` are the identity. The rest is evidence the
    discovery proposal cites (cite-or-don't-recommend): every claim on the
    proposal traces to a field here.
    """

    provider: str
    model_id: str                       # bare id as the provider returns it
    qualified_id: str                   # "{provider}/{model_id}"
    display_name: str = ""
    created: int | None = None          # unix ts when the provider exposes it
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # ``raw`` can be large/noisy; keep it out of the serialized evidence.
        d.pop("raw", None)
        return d


@dataclass
class ProviderEnumeration:
    """Result of enumerating one provider's listing.

    ``ok=False`` means the listing call failed — ``reason`` carries the
    operator-facing "discovery skipped for <provider>: <reason>" string and
    ``models`` is empty. The caller MUST surface this as degraded; it must
    never be flattened into "all current."
    """

    provider: str
    ok: bool
    models: list[ListedModel] = field(default_factory=list)
    reason: str = ""                    # populated when ok=False

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "reason": self.reason,
            "model_count": len(self.models),
        }


# ── Discovery findings ────────────────────────────────────────────────────────

@dataclass
class DiscoveryFinding:
    """An unknown chat-capable model the pod hasn't adopted into any rung.

    This is the class the rework exists to surface — a new model *line*
    (Fable) that the old list-matching check was structurally blind to.
    """

    provider: str
    model_id: str                       # bare id
    qualified_id: str                   # "{provider}/{model_id}"
    evidence: dict[str, Any]            # listing fields (context window, etc.)
    suggested_rung: str                 # heuristic placement, e.g. "above opus-class"
    suggested_rationale: str            # heuristic, optionally LLM-polished
    # Structured placement for the AdoptModel action (spec §Addendum A).
    # ``suggested_rung_slug`` is the rung id the applier creates/extends;
    # ``suggested_cost_class`` is the costClass for a newly-created rung;
    # ``suggested_position`` is the cost-rank insertion index into the pod
    # rungs[] array (0 = cheapest end). Defaults keep older constructors
    # (and tests) valid — the heuristic always fills them.
    #
    # ``suggested_rung_slug`` defaults to "" (UNPLACED), NEVER the placeholder
    # ``"new-rung"`` (Addendum 13): a placeholder slug used to persist a dead
    # rung no role pointed at — the near-bug this rework removes. An unplaceable
    # model carries an empty slug, and the applier rejects an empty/placeholder
    # slug rather than minting a dead cluster.
    suggested_rung_slug: str = ""
    suggested_cost_class: str = "medium"
    suggested_position: int = 0
    # ── Placement verdict (Addendum 13) ──────────────────────────────────────
    # The capability-aware fit verdict, computed at observe time so the card
    # reads it with zero re-run. ``placement_verdict`` ∈
    # ``fits_existing | new_tier | mode_variant | specialist | cannot_place``;
    # ``recommended_role`` is a real role ONLY for ``fits_existing`` (None for the
    # other four); ``recommended_rung_slug`` is the existing rung for
    # ``fits_existing`` / a PROPOSED slug for ``new_tier`` / None for
    # ``mode_variant`` / ``specialist`` / ``cannot_place`` — and NEVER the
    # ``"new-rung"`` placeholder.
    # ``fit_reason`` is one plain-language operator-facing sentence (no
    # rung/band/dormant/position jargon — Bite 2 owns card copy, this owns the
    # reason string the Signal carries). ``fit_confidence`` is 0..1.
    # ``fit_evidence`` cites the signals behind the verdict. ``fit_source`` says
    # which layer produced it (``deterministic`` | ``llm``). Defaults are the
    # safe ``cannot_place`` so an older constructor (or a partial finding) never
    # mis-claims a placement.
    placement_verdict: str = "cannot_place"
    recommended_role: str | None = None
    recommended_rung_slug: str | None = None
    fit_reason: str = ""
    fit_confidence: float = 0.0
    fit_evidence: dict[str, Any] = field(default_factory=dict)
    fit_source: str = "deterministic"
    # Cited pricing evidence behind ``suggested_cost_class`` (Addendum 8 §C,
    # cite-or-don't-recommend). One of:
    #   {"source": "pricing", "input_cost_per_token": ..., "pricing_source": ...}
    #   {"source": "family-map", "family": ..., "band": ...}
    #   {"source": "unknown"}  — no price anywhere AND no family match; the band
    #     is then NOT pricing-derived (it falls back to the naming heuristic) and
    #     the proposal says so. ``cost_band_source`` lifts the source out for the
    #     Signal/Proposal so the cite-or-don't-recommend rule is checkable.
    cost_band_source: str = "heuristic"
    cost_band_evidence: dict[str, Any] = field(default_factory=dict)

    def signature(self) -> str:
        """Stable signal signature per (provider, model_id) so re-fires dedup."""
        return f"model_discovery:{self.provider}:{self.model_id}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StalenessFinding:
    """A known model that has a newer version in the same family per the
    *listing* (not the dict). Within-family staleness, soft upgrade.
    """

    provider: str
    family: str                         # e.g. "claude-opus"
    current_model: str                  # qualified id the pod uses
    latest_model: str                   # qualified id the listing exposes
    evidence: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VersionUpgrade:
    """A model the pod runs that has a newer version of the SAME class available
    in the provider's live listing — the everyday "ride the latest version"
    nudge (spec §Addendum 15). This is the first-class, actually-consumed
    promotion of the dead-ended :class:`StalenessFinding`: it carries the
    on-pod LOCATION (``rung_slug`` + ``roles``) an upgrade needs to be applied
    with one click via the ``AdoptModel`` applier (splice ``latest_model`` into
    the rung the predecessor occupies, AHEAD of that predecessor — the resolver
    routes to the first credentialed cluster member, not the newest, so the new
    version must lead its predecessor for routing to actually move; the role
    re-point is then a no-op).
    """

    provider: str
    family: str                         # e.g. "claude-sonnet"
    current_model: str                  # qualified id the pod runs today
    latest_model: str                   # qualified id of the newest listed member
    rung_slug: str                      # the rung current_model occupies ("" if unlocated)
    roles: list[str]                    # roles routing through that rung (may be empty: dormant)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiscoveryResult:
    """The full output of one discovery run.

    ``degraded_providers`` is the load-bearing field: any provider that
    failed enumeration lands here with a reason. A result with
    ``degraded_providers`` non-empty is NOT "all current" — callers render
    it as partially-degraded.
    """

    checked_at: str
    enumerated_providers: list[str]                 # providers with a live listing
    degraded_providers: list[dict[str, str]]        # [{provider, reason}]
    discoveries: list[DiscoveryFinding] = field(default_factory=list)
    staleness: list[StalenessFinding] = field(default_factory=list)
    # Version upgrades — the PRIMARY result (spec §Addendum 15): every model the
    # pod runs that has a newer same-class version in the listing, with the rung
    # it occupies so the card can offer a one-click "update to latest".
    upgrades: list[VersionUpgrade] = field(default_factory=list)
    known_model_count: int = 0
    ignored_count: int = 0
    # Credentialed providers Evolve has NO listing adapter for (credentialed but
    # not in _LISTING_PROVIDERS). Their models can't be enumerated, so they never
    # reach the validated picker / easy-setup filter. Surfaced as an advisory
    # (the gap Signal in observe.py) rather than silently skipped — the
    # silent-monitor-drift lesson, and exactly how the xAI gap should have
    # announced itself. Distinct from ``degraded_providers`` (a listing CALL that
    # failed): an uncovered provider has no adapter to call at all.
    uncovered_providers: list[str] = field(default_factory=list)
    # Non-frontier unknowns: chat-capable models the pod hasn't adopted but
    # whose family already HAS an adopted member (a stale/older or
    # newer-but-staleness-covered variant). Counted, never silently dropped,
    # so the frontier filtering is visible in the result.
    skipped_count: int = 0

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_providers)

    @property
    def all_current(self) -> bool:
        """True ONLY when discovery actually ran for every credentialed
        provider AND found nothing. A degraded run is never 'all current';
        neither is a run with an UNCOVERED provider — a credentialed provider
        Evolve has no adapter for is a known blind spot (its models were never
        enumerated), so the run cannot honestly claim everything is current
        (the silent-monitor-drift lesson)."""
        return (
            not self.is_degraded
            and not self.uncovered_providers
            and not self.discoveries
            and not self.staleness
            and not self.upgrades
        )

    def to_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "enumerated_providers": self.enumerated_providers,
            "degraded_providers": self.degraded_providers,
            "discoveries": [d.to_dict() for d in self.discoveries],
            "staleness": [s.to_dict() for s in self.staleness],
            "upgrades": [u.to_dict() for u in self.upgrades],
            "known_model_count": self.known_model_count,
            "ignored_count": self.ignored_count,
            "skipped_count": self.skipped_count,
            "uncovered_providers": self.uncovered_providers,
            "is_degraded": self.is_degraded,
            "all_current": self.all_current,
        }


# ── Credential discovery ──────────────────────────────────────────────────────
# The discovery enumeration needs actual key VALUES (the freshness check only
# needed presence). OpenClaw 2026.6.9 migrated each bot's per-agent auth out of
# ``auth-profiles.json`` into a per-agent SQLite store (``openclaw-agent.sqlite``);
# the raw ``api_key`` VALUES still live in that store's ``store_json`` blob
# (verified live on the mini: every api_key profile carries a non-empty ``key``),
# so a listing call can still use them. Read the SOURCE through the audited
# evolve-context reader ``evolve_admin.oc_store.read_auth_store`` — it walks the
# source ladder (sqlite store → legacy ``auth-profiles.json`` → transitional bak)
# across all discovered agent dirs (main-first, not hardcoded) and is
# platform-aware (no hardcoded ``/Users`` path), mirroring how the app scanner
# resolves keys. A direct-read fallback covers the rare context where
# ``evolve_admin`` isn't importable (mirrors arbiter.refine / app_posture_reflect).

_AUTH_PROFILES_RELPATH = ".openclaw/agents/main/agent/auth-profiles.json"


def _strip_json_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _read_auth_profiles(bot_user: str) -> dict | None:
    """Resolve a bot's parsed auth-profiles document, or ``None``.

    SOURCE is ``oc_auth_store.read_auth_store`` (sqlite → legacy json → bak,
    agent-dir discovered, platform-aware) — the analyzer-side home of the one
    reader since #3475; this used to reach UP into ``evolve_admin.oc_store``,
    against the dependency direction, and silently degraded to the JSON-only
    legacy read in any context where admin wasn't importable. Before OpenClaw 2026.6.9's
    SQLite migration this read the now-deleted ``auth-profiles.json`` directly,
    returned ``None`` on every migrated pod, and discovery silently enumerated
    zero providers — the same incident class #3248 fixed for the presence reader
    and the app scanner fixed for key resolution. Returns the full parsed
    ``{version, profiles: {...}}`` document (with raw ``api_key`` values) so the
    per-provider extractors below find ``profiles[...].key``.
    """
    raw: str | None
    try:
        from oc_auth_store import read_auth_store  # type: ignore
    except Exception:
        # The shared reader is an analyzer sibling and should always be
        # importable; if it somehow is not, fall back to the legacy direct read
        # so un-migrated / legacy pods still resolve.
        return _read_auth_profiles_legacy(bot_user)
    raw = read_auth_store(bot_user, user=bot_user)
    if not raw:
        return None
    try:
        return json.loads(_strip_json_trailing_commas(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def _read_auth_profiles_legacy(bot_user: str) -> dict | None:
    """Legacy direct read of the per-``main``-agent ``auth-profiles.json``.

    Fallback for contexts where ``oc_auth_store`` can't be imported (in
    practice: none — it is an analyzer sibling of this module). Reads the JSON
    directly (evolve ACL) with a ``sudo /bin/cat`` fallback; does NOT see the
    post-2026.6.9 SQLite store, so it resolves only un-migrated pods.
    """
    path = f"/Users/{bot_user}/{_AUTH_PROFILES_RELPATH}"
    text: str | None = None
    try:
        text = Path(path).read_text()
    except PermissionError:
        try:
            r = subprocess.run(
                ["sudo", "/bin/cat", path],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0:
                text = r.stdout
        except Exception:
            text = None
    except (FileNotFoundError, OSError):
        text = None
    if not text:
        return None
    try:
        return json.loads(_strip_json_trailing_commas(text))
    except (json.JSONDecodeError, ValueError):
        return None


def _key_from_profiles(profiles: dict, provider: str) -> str | None:
    """Return the first non-empty api_key value for ``provider`` from a
    parsed auth-profiles dict. Key VALUES are read here (not just presence) —
    the value never crosses a process boundary back out of this module; it's
    used in-process to build the Authorization header and then dropped."""
    for p in (profiles.get("profiles") or {}).values():
        if not isinstance(p, dict):
            continue
        if p.get("provider") != provider:
            continue
        mode = p.get("type", p.get("mode", ""))
        if mode != "api_key":
            continue
        val = p.get("key", "")
        if val and str(val).strip():
            return str(val).strip()
    return None


def _providers_from_profiles(profiles: dict) -> set[str]:
    """Every provider name that has an ``api_key`` profile in a parsed
    auth-profiles dict. Used for the credentialed-but-no-adapter gap check —
    we enumerate ALL credentialed providers (not just listing-capable ones) so
    a provider Evolve can't enumerate still surfaces as an advisory rather than
    being silently skipped.

    Restricted to ``api_key`` profiles (the only mode a listing adapter could
    use) so subscription/oauth-token profiles don't produce phantom gaps."""
    out: set[str] = set()
    for p in (profiles.get("profiles") or {}).values():
        if not isinstance(p, dict):
            continue
        mode = p.get("type", p.get("mode", ""))
        if mode != "api_key":
            continue
        prov = p.get("provider")
        val = p.get("key", "")
        if prov and val and str(val).strip():
            out.add(str(prov))
    return out


def discover_credentialed_providers(bot_users: list[str]) -> set[str]:
    """Resolve the set of every provider any bot in the pod holds an api_key
    for — pod-wide, regardless of whether Evolve has a listing adapter for it.

    This is the input to the credentialed-but-no-adapter gap check: a provider
    in this set but NOT in :data:`_LISTING_PROVIDERS` is credentialed yet
    un-enumerable, so its models never reach the validated picker / easy-setup
    filter. Surfacing that gap (instead of silently skipping the provider) is
    how the xAI gap would have announced itself. Derived from auth-profiles —
    no provider-name literals, so a new provider needs no edit here."""
    found: set[str] = set()
    for user in bot_users:
        profiles = _read_auth_profiles(user)
        if not profiles:
            continue
        found |= _providers_from_profiles(profiles)
    return found


def discover_provider_keys(
    bot_users: list[str],
    providers: set[str] | None = None,
) -> dict[str, str]:
    """Resolve one usable API key per credentialed provider, pod-wide.

    Walks each bot's auth-profiles in order and returns the first usable
    api_key found for each provider. Enumeration is pod-wide (one listing
    call per provider per run), so we only need one key per provider — the
    first bot that has it wins.

    ``providers`` optionally restricts to a known set (defaults to the
    listing-capable providers).
    """
    want = providers if providers is not None else set(_LISTING_PROVIDERS)
    found: dict[str, str] = {}
    for user in bot_users:
        if want and set(found.keys()) >= want:
            break  # already have a key for everything we care about
        profiles = _read_auth_profiles(user)
        if not profiles:
            continue
        for provider in want:
            if provider in found:
                continue
            key = _key_from_profiles(profiles, provider)
            if key:
                found[provider] = key
    return found


# ── HTTP listing fetchers ─────────────────────────────────────────────────────
# provider-literal-allow-begin: provider ADAPTERS (home #2) — per-provider
# listing endpoints + key formats. Provider names here are adapter wiring, not
# routing/availability logic (spec §Addendum 3.B three-homes rule).

def _http_get_json(url: str, headers: dict[str, str]) -> dict:
    """Plain stdlib GET → parsed JSON. Raises on HTTP/network error so the
    caller can record a degraded reason. No retries (the daily sweep stays
    cheap; a transient failure degrades this run and recovers next run)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _fetch_anthropic(key: str) -> list[ListedModel]:
    data = _http_get_json(
        "https://api.anthropic.com/v1/models?limit=1000",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    out: list[ListedModel] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append(ListedModel(
            provider="anthropic",
            model_id=mid,
            qualified_id=f"anthropic/{mid}",
            display_name=m.get("display_name") or "",
            # Anthropic returns created_at ISO; keep raw, leave created None.
            capabilities=["chat"],  # the /models listing is chat models
            raw=m,
        ))
    return out


def _fetch_openai(key: str) -> list[ListedModel]:
    data = _http_get_json(
        "https://api.openai.com/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    out: list[ListedModel] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append(ListedModel(
            provider="openai",
            model_id=mid,
            qualified_id=f"openai/{mid}",
            created=m.get("created") if isinstance(m.get("created"), int) else None,
            capabilities=_openai_capabilities(mid),
            raw=m,
        ))
    return out


def _fetch_xai(key: str) -> list[ListedModel]:
    # xAI's API is OpenAI-compatible: GET /v1/models, Bearer auth, a
    # ``{data: [{id, ...}]}`` envelope. We model the adapter on the OpenAI one;
    # the only differences are the host and the provider tag. The listing
    # carries no capability metadata, so capability is inferred from the id via
    # the shared substring heuristic (grok-* chat models surface; any non-chat
    # variant is excluded by ``_NON_CHAT_SUBSTRINGS``).
    data = _http_get_json(
        "https://api.x.ai/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    out: list[ListedModel] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append(ListedModel(
            provider="xai",
            model_id=mid,
            qualified_id=f"xai/{mid}",
            created=m.get("created") if isinstance(m.get("created"), int) else None,
            capabilities=_id_chat_capabilities(mid),
            raw=m,
        ))
    return out


def _fetch_deepseek(key: str) -> list[ListedModel]:
    # DeepSeek's API is OpenAI-compatible: GET /v1/models, Bearer auth, a
    # ``{data: [{id, ...}]}`` envelope. Modeled on the OpenAI/xAI adapters; the
    # only differences are the host and the provider tag. The listing carries no
    # capability metadata, so capability is inferred from the id via the shared
    # substring heuristic (deepseek-chat / deepseek-reasoner surface as chat; any
    # non-chat variant is excluded by ``_NON_CHAT_SUBSTRINGS``).
    data = _http_get_json(
        "https://api.deepseek.com/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    out: list[ListedModel] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append(ListedModel(
            provider="deepseek",
            model_id=mid,
            qualified_id=f"deepseek/{mid}",
            created=m.get("created") if isinstance(m.get("created"), int) else None,
            capabilities=_id_chat_capabilities(mid),
            raw=m,
        ))
    return out


def _fetch_mistral(key: str) -> list[ListedModel]:
    # Mistral's API is OpenAI-compatible: GET /v1/models, Bearer auth, a
    # ``{data: [{id, ...}]}`` envelope. Modeled on the OpenAI/xAI/DeepSeek
    # adapters; the only differences are the host and the provider tag. The id
    # is the authoritative identity; capability is inferred from the id via the
    # shared substring heuristic (mistral-large / mistral-small / codestral
    # surface as chat; mistral-embed and other non-chat variants are excluded by
    # ``_NON_CHAT_SUBSTRINGS``).
    data = _http_get_json(
        "https://api.mistral.ai/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    out: list[ListedModel] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append(ListedModel(
            provider="mistral",
            model_id=mid,
            qualified_id=f"mistral/{mid}",
            created=m.get("created") if isinstance(m.get("created"), int) else None,
            capabilities=_id_chat_capabilities(mid),
            raw=m,
        ))
    return out


def _fetch_moonshot(key: str) -> list[ListedModel]:
    # Moonshot's (Kimi) API is OpenAI-compatible: GET /v1/models, Bearer auth,
    # a ``{data: [{id, ...}]}`` envelope. Modeled on the OpenAI/xAI/DeepSeek
    # adapters; the only differences are the host and the provider tag. The
    # listing carries no capability metadata, so capability is inferred from
    # the id via the shared substring heuristic (kimi-k* surface as chat; any
    # non-chat variant is excluded by ``_NON_CHAT_SUBSTRINGS``).
    data = _http_get_json(
        "https://api.moonshot.ai/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    out: list[ListedModel] = []
    for m in data.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        if not mid:
            continue
        out.append(ListedModel(
            provider="moonshot",
            model_id=mid,
            qualified_id=f"moonshot/{mid}",
            created=m.get("created") if isinstance(m.get("created"), int) else None,
            capabilities=_id_chat_capabilities(mid),
            raw=m,
        ))
    return out


def _fetch_google(key: str) -> list[ListedModel]:
    # Pass the key via header, NOT the ?key= query string. A query-string key
    # can leak into exception text (some socket/SSL/urllib errors stringify the
    # full URL), which the degraded-Signal catch-all formats into operator chat
    # and disk. The x-goog-api-key header keeps the secret out of the URL.
    data = _http_get_json(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        {"x-goog-api-key": key},
    )
    out: list[ListedModel] = []
    for m in data.get("models") or []:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""  # "models/gemini-2.5-pro"
        mid = name.split("/", 1)[1] if name.startswith("models/") else name
        if not mid:
            continue
        methods = m.get("supportedGenerationMethods") or []
        out.append(ListedModel(
            provider="google",
            model_id=mid,
            qualified_id=f"google/{mid}",
            display_name=m.get("displayName") or "",
            context_window=m.get("inputTokenLimit"),
            max_output_tokens=m.get("outputTokenLimit"),
            capabilities=_google_capabilities(methods),
            raw=m,
        ))
    return out


_FETCHERS = {
    "anthropic": _fetch_anthropic,
    "openai": _fetch_openai,
    "google": _fetch_google,
    "xai": _fetch_xai,
    "deepseek": _fetch_deepseek,
    "mistral": _fetch_mistral,
    "moonshot": _fetch_moonshot,
}
# provider-literal-allow-end


# Matches a ``key=<secret>`` query-string param (any provider) so a leaked
# API key never lands in a degraded-Signal reason. Belt-and-braces alongside
# the header-based Google fix: protects every provider's catch-all in case a
# future fetcher (or a redirect/proxy URL) puts a key in a URL again.
_KEY_QS_RE = re.compile(r"(?i)key=[^&\s'\")]+")


def _scrub_key(text: str) -> str:
    """Redact any ``key=<secret>`` query param from operator-facing text."""
    return _KEY_QS_RE.sub("key=REDACTED", text)


def enumerate_provider(provider: str, key: str) -> ProviderEnumeration:
    """Fetch one provider's live listing. Never raises — a failure returns
    ``ok=False`` with an operator-facing reason. THE silent-failure guard:
    a swallowed exception here is reported degraded, not as 'no models /
    all current'.

    Every exception-derived reason is run through ``_scrub_key`` so a secret
    embedded in a stringified URL can't reach Signal body/details (operator
    chat + disk)."""
    fetcher = _FETCHERS.get(provider)
    if fetcher is None:
        return ProviderEnumeration(
            provider=provider, ok=False,
            reason="no listing endpoint for this provider",
        )
    try:
        models = fetcher(key)
    except urllib.error.HTTPError as e:
        return ProviderEnumeration(
            provider=provider, ok=False,
            reason=_scrub_key(f"listing HTTP {e.code} ({e.reason})"),
        )
    except urllib.error.URLError as e:
        return ProviderEnumeration(
            provider=provider, ok=False,
            reason=_scrub_key(f"listing unreachable ({e.reason})"),
        )
    except (json.JSONDecodeError, ValueError) as e:
        return ProviderEnumeration(
            provider=provider, ok=False,
            reason=_scrub_key(f"listing response unparseable ({e})"),
        )
    except Exception as e:  # defensive: anything else still degrades, not silences
        return ProviderEnumeration(
            provider=provider, ok=False,
            reason=_scrub_key(f"listing failed ({type(e).__name__}: {e})"),
        )
    # An empty-but-successful listing is genuinely "no models from this
    # provider," distinct from a failure. Caller treats ok=True/empty as
    # current; ok=False as degraded.
    return ProviderEnumeration(provider=provider, ok=True, models=models)


# ── Capability + snapshot-alias filtering ─────────────────────────────────────

# Substrings that mark a listed model as out-of-scope for chat discovery:
# embeddings/audio/image/moderation/specialty. Filtered before discovery so
# "claude-fable-5" surfaces but "text-embedding-3-large" never does.
_NON_CHAT_SUBSTRINGS = (
    "embed", "embedding", "whisper", "tts", "audio", "transcribe",
    "moderation", "dall-e", "dalle", "image", "vision-only",
    "aqa", "imagen", "veo", "stable", "rerank", "guard", "ocr",
)


def _id_chat_capabilities(model_id: str) -> list[str]:
    """Heuristic capability tags inferred from a model id alone, for providers
    whose /v1/models listing carries no capability metadata (OpenAI, xAI — any
    OpenAI-compatible ``{data:[{id}]}`` listing). A chat model tags ``["chat"]``;
    an id matching a non-chat substring (embed/audio/image/…) tags
    ``["non-chat"]`` so it never surfaces as a chat discovery.

    The rule is provider-NEUTRAL by design: it keys off the id only, so it is
    the single home for the OpenAI-compatible capability heuristic rather than
    one copy per provider (dup-guard)."""
    low = model_id.lower()
    caps: list[str] = []
    for sub in _NON_CHAT_SUBSTRINGS:
        if sub in low:
            caps.append("non-chat")
            break
    if "non-chat" not in caps:
        caps.append("chat")
    return caps


def _openai_capabilities(model_id: str) -> list[str]:
    """Back-compat alias — OpenAI uses the shared id-based chat heuristic."""
    return _id_chat_capabilities(model_id)


def _google_capabilities(methods: list[str]) -> list[str]:
    """Capability tags from Google's supportedGenerationMethods."""
    caps: list[str] = []
    if any(m in ("generateContent", "streamGenerateContent") for m in methods):
        caps.append("chat")
    if "embedContent" in methods:
        caps.append("embedding")
    if not caps:
        caps.append("non-chat")
    return caps


def is_chat_capable(model: ListedModel) -> bool:
    """True when the model is a chat/completion model (in scope for
    discovery). Uses capability metadata where the provider supplies it
    (Google), substring heuristics otherwise (OpenAI/Anthropic)."""
    caps = model.capabilities or []
    if "chat" in caps:
        # double-check the id doesn't scream non-chat (defensive for
        # providers that tag everything "chat")
        low = model.model_id.lower()
        return not any(sub in low for sub in _NON_CHAT_SUBSTRINGS)
    if "non-chat" in caps or "embedding" in caps:
        return False
    # Unknown — fall back to substring heuristic.
    low = model.model_id.lower()
    return not any(sub in low for sub in _NON_CHAT_SUBSTRINGS)


# A dated snapshot alias is a known model with a date/version suffix, e.g.
# "gpt-4o-2024-08-06" or "claude-3-5-sonnet-20241022". We treat a listed
# model as a snapshot alias of a KNOWN model when, after stripping a trailing
# date token, its base matches a known model's base. Such aliases are
# auto-ignored (they're not a new line — just a pinned snapshot).
_DATE_SUFFIX_RE = re.compile(
    r"[-@](?:\d{8}|\d{4}-\d{2}-\d{2}|v\d+(?:\.\d+)*|latest|preview|exp)$",
    re.IGNORECASE,
)


def _strip_date_suffix(model_id: str) -> str:
    prev = None
    cur = model_id
    # iterate in case of stacked suffixes (e.g. "...-preview-0827")
    while cur != prev:
        prev = cur
        cur = _DATE_SUFFIX_RE.sub("", cur)
        cur = re.sub(r"[-@]\d{4,8}$", "", cur)
    return cur


def _bare_id(qualified_or_bare: str) -> str:
    return qualified_or_bare.split("/", 1)[1] if "/" in qualified_or_bare else qualified_or_bare


def _family_of(model_id: str) -> str:
    """Coarse family key for within-family staleness, e.g.
    'claude-opus', 'gpt-4o', 'gemini-flash'. Strips version/date tokens so
    different versions of the same line share a family.

    Two shapes are normalized:
      • trailing version run — ``claude-opus-4-8`` → ``claude-opus`` (vendor
        names the line, then the version trails; Anthropic shape).
      • mid version token — ``gemini-2.5-flash`` → ``gemini-flash`` (vendor,
        then a *pure-numeric* version, then a tier word; Google shape). Only a
        token that is fully numeric (``2.5``, ``2.0``) collapses, so ``gpt-4o``
        — where ``4o`` is alphanumeric, not a bare version — is left intact and
        stays its own family (matching the existing contract)."""
    base = _strip_date_suffix(_bare_id(model_id)).lower()
    # Collapse a mid version token: vendor-<numeric-version>-<tier...> → vendor-<tier...>.
    base = re.sub(r"(^[a-z]+)[-_]\d+(?:[.\-]\d+)*[-_]([a-z].*)$", r"\1-\2", base)
    # Drop a trailing version run like "-4-8", "-4.6", "-5" → family stem.
    base = re.sub(r"[-_](?:\d+([.\-]\d+)*)$", "", base)
    base = re.sub(r"[-_](?:\d+([.\-]\d+)*)$", "", base)  # twice for "-4-8"
    return base


def is_snapshot_alias_of_known(model: ListedModel, known_bare: set[str]) -> bool:
    """True when ``model`` is a dated snapshot alias of a model already in the
    pod's known set (so it's not a new line to surface)."""
    bare = model.model_id.lower()
    if bare in known_bare:
        return False  # exact known model is handled by staleness, not ignore
    stripped = _strip_date_suffix(bare)
    if stripped == bare:
        return False  # no date suffix → not a snapshot alias
    return stripped in known_bare or any(stripped == k for k in known_bare)


def is_snapshot_alias_of_listing(model: ListedModel, listing_bare: set[str]) -> bool:
    """True when ``model`` is a dated snapshot alias of a *base* id that itself
    appears in the same listing (e.g. ``claude-opus-4-5-20251101`` alongside a
    plain ``claude-opus-4-5``). Such aliases are pinned snapshots of a model
    already represented by its base entry — they must never surface separately,
    regardless of whether the base is known. The base must be a DIFFERENT id
    (a model is not a snapshot alias of itself)."""
    bare = model.model_id.lower()
    stripped = _strip_date_suffix(bare)
    if stripped == bare:
        return False  # no date suffix → not a snapshot alias
    return stripped != bare and stripped in listing_bare


def known_family_stems(known_bare: set[str]) -> set[str]:
    """Family stems (e.g. ``claude-opus``, ``gpt-4o``) the pod has adopted a
    member of. The frontier filter checks an unknown model's stem against this
    set; a hit means the family is already adopted → the unknown is a
    within-family variant (staleness-covered or stale catalog), NOT a discovery.

    ``known_bare`` holds bare, provider-stripped ids (that's how
    ``known_model_set`` stores them). The family stem carries the vendor prefix
    in every real listing (``claude-*`` / ``gpt-*`` / ``gemini-*``), so the stem
    alone is provider-distinct and we don't need to re-attach a provider key."""
    return {_family_of(k) for k in known_bare if k.strip()}


# ── Band → rung mapping (catalog-DATA-driven, no provider literals) ────────────
# A discovered model's suggested rung is derived from its computed COST BAND, not
# its provider (spec §Addendum 3.B "no provider literals in logic" + §Addendum 8
# §C cost-band system). The canonical rungs in ``DEFAULT_MODEL_CATALOG`` each
# carry a ``costClass`` (haiku-class=low, sonnet-class=medium, opus-class=high,
# fable-class=premium) and the ``roles`` map names each rung's role (fast /
# standard / power / max). Inverting that DATA gives a band → (rung-slug, role)
# map with zero provider/model literals: a ``low``-band model from ANY provider
# suggests fast/haiku-class, a ``high``-band model suggests power/opus-class.
#
# Built once from the catalog and memoized; a new provider needs NO edit here
# (its models flow through the same band → rung map). When two rungs share a
# costClass, the first in catalog array order wins (matches the cost-order
# tiebreak in primary_bot).

def _band_to_rung_map() -> dict[str, tuple[str, str]]:
    """Map each cost band → ``(rung_slug, role)`` from ``DEFAULT_MODEL_CATALOG``.

    Pure DATA derivation: read each rung's ``costClass`` + ``id`` and the
    ``roles`` map (role → rung-slug), invert to rung → role, and key by band.
    Names no provider or model. Falls back to an empty map if the catalog can't
    be imported (the caller then treats every band as unknown — unplaced,
    operator-categorized, per "propose, never auto-categorize")."""
    try:
        from primary_bot import default_model_catalog  # type: ignore
        cat = default_model_catalog()
    except Exception:
        return {}
    # rung-slug → role (invert the catalog roles map; a role entry may be a bare
    # slug or a ``{"rung": ...}`` object for constrained roles like judge).
    rung_role: dict[str, str] = {}
    for role, entry in (cat.get("roles") or {}).items():
        slug = entry if isinstance(entry, str) else (
            entry.get("rung") if isinstance(entry, dict) else None
        )
        if isinstance(slug, str) and slug:
            rung_role.setdefault(slug, role)
    out: dict[str, tuple[str, str]] = {}
    for rung in (cat.get("rungs") or []):
        if not isinstance(rung, dict):
            continue
        band = rung.get("costClass")
        slug = rung.get("id")
        if not isinstance(band, str) or not isinstance(slug, str) or not slug:
            continue
        if band in out:
            continue  # first rung in array order wins for a shared costClass
        out[band] = (slug, rung_role.get(slug, ""))
    return out


# Naming-size tokens used ONLY in the unknown-band fallback (model not in the
# pricing cache AND its family not in the family→band map). These are generic
# SIZE words shared across providers — they are NOT provider/model names, so the
# fallback stays within the three-homes rule. A ``*-mini``/``*-small``/``*-nano``
# id reads as a low-band model; a ``*-large``/flagship marker reads as medium.
# Anything else gets NO confident band (None) → the discovery surfaces unplaced
# and the operator categorizes it (spec "propose, never auto-categorize").
_SIZE_TOKENS_LOW = ("mini", "nano", "small", "lite", "tiny", "micro")
_SIZE_TOKENS_MEDIUM = ("large", "xl", "ultra", "max", "pro")


def _band_from_size_naming(model_id: str) -> str | None:
    """Last-resort, PROVIDER-NEUTRAL band guess from generic size tokens in the
    model id (no provider/model literals). Returns ``low``/``medium`` for a
    recognized size token, else ``None`` (no confident suggestion → unplaced)."""
    low = model_id.lower()
    if any(t in low for t in _SIZE_TOKENS_LOW):
        return "low"
    if any(t in low for t in _SIZE_TOKENS_MEDIUM):
        return "medium"
    return None


def rung_for_band(band: str | None) -> tuple[str, str]:
    """Return ``(rung_slug, role)`` for a cost ``band`` via the catalog map.

    An unknown/None band yields ``("", "")`` — no confident rung — so the
    discovery surfaces UNPLACED for operator categorization rather than being
    force-fit into a cluster (spec "propose, never auto-categorize")."""
    if not band:
        return "", ""
    return _band_to_rung_map().get(band, ("", ""))


def suggest_rung_placement(
    model: ListedModel,
    known_bare: set[str],
    band: str | None = None,
) -> tuple[str, str]:
    """Band-derived (no LLM, no provider literals) rung-placement suggestion +
    rationale.

    ``band`` is the model's computed cost band (``low|medium|high|premium`` from
    :func:`model_cost_bands.resolve_band`, or ``None`` when neither pricing nor
    the family map could place it). The suggested rung is the catalog rung whose
    ``costClass`` matches the band — a band → rung mapping driven by the rung
    ``costClass`` DATA, NOT by ``model.provider``. When the band is unknown the
    model is suggested UNPLACED (operator categorizes it), with a generic-size
    naming hint surfaced as soft evidence only. Every claim in the rationale
    cites a band/listing field (cite-or-don't-recommend).
    """
    bits: list[str] = []
    slug, role = rung_for_band(band)
    if slug:
        rung = f"{slug} ({role})" if role else slug
        bits.append(
            f"cost band '{band}' maps to the {slug} rung (matched costClass)"
        )
    else:
        # No confident band → no confident rung. Surface a generic-size hint as
        # SOFT evidence (never a provider conditional), but leave it unplaced.
        size_band = _band_from_size_naming(model.model_id)
        if size_band:
            hint_slug, hint_role = rung_for_band(size_band)
            rung = (
                f"a new rung — possibly {hint_slug} ({hint_role}) by naming"
                if hint_slug else "a new rung (placement unclear)"
            )
            bits.append(
                f"no priced band; a size token hints a '{size_band}' tier"
            )
        else:
            rung = "a new rung (placement unclear — categorize on adoption)"

    if model.context_window:
        bits.append(f"context window {model.context_window:,} tokens")
    if model.max_output_tokens:
        bits.append(f"max output {model.max_output_tokens:,} tokens")

    rationale = (
        f"Suggested placement: {rung}. "
        + ("Evidence: " + "; ".join(bits) + ". " if bits else "")
        + "Confirm the rung against cost before adopting."
    )
    return rung, rationale


def suggest_rung_structured(
    model: "ListedModel", band: str | None = None,
) -> tuple[str, str]:
    """Return ``(rung_slug, cost_class)`` for the AdoptModel action (spec
    §Addendum A), derived from the model's computed COST BAND — not its provider.

    ``band`` is the resolved cost band (``low|medium|high|premium`` from
    :func:`model_cost_bands.resolve_band`). The slug is the catalog rung whose
    ``costClass`` matches the band (haiku-class/low … fable-class/premium); the
    returned cost_class is that band. There are NO ``model.provider == "..."``
    branches — a band-derived mapping over the rung ``costClass`` DATA, so a new
    provider needs no edit here.

    Unknown band: try a PROVIDER-NEUTRAL generic-size naming hint
    (``*-mini``→low / ``*-large``→medium). If that also fails, the model is
    UNPLACED — an EMPTY slug at the default ``medium`` cost (Addendum 13:
    unplaceable means *no rung suggested*, never the ``"new-rung"`` placeholder
    that used to persist a dead cluster). The operator categorizes it on
    adoption. No provider/model literal is reintroduced in either fallback.
    """
    slug, _role = rung_for_band(band)
    if slug and band:
        return slug, band
    # Unknown band → generic-size naming hint (provider-neutral), else unplaced.
    size_band = _band_from_size_naming(model.model_id)
    if size_band:
        hint_slug, _ = rung_for_band(size_band)
        if hint_slug:
            return hint_slug, size_band
    # Unplaceable: empty slug (no rung) at a harmless default cost. The applier
    # rejects an empty/placeholder slug, so this can never mint a dead rung.
    return "", "medium"


# Cost-rank order of costClass bands (cheapest first). Used to compute the
# suggested insertion index into an existing rungs[] array.
_COST_CLASS_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "premium": 3}


def suggest_rung_position(rungs: list, cost_class: str) -> int:
    """Cost-rank insertion index for a new rung of ``cost_class`` into an
    existing ``rungs[]`` array (cheapest first, position 0 = cheapest end).

    Inserts AFTER all existing rungs whose costClass rank is <= the new
    rung's, so a ``premium`` band lands at the expensive end and a ``low``
    band at the cheap end. Rungs with an unknown/absent costClass are
    treated as ``medium`` for ranking. The applier re-clamps the value, so
    an out-of-range hint is harmless.
    """
    new_rank = _COST_CLASS_RANK.get(cost_class, 1)
    idx = 0
    for r in (rungs or []):
        rc = r.get("costClass") if isinstance(r, dict) else None
        rank = _COST_CLASS_RANK.get(rc, 1)
        if rank <= new_rank:
            idx += 1
        else:
            break
    return idx


# ── Placement verdict — the capability-aware fit engine (Addendum 13) ──────────
# Every firing discovery carries a placement verdict computed HERE at observe
# time (the daily sweep already runs the analysis; the card reads it with zero
# re-run). The deterministic layer below produces the verdict from signals the
# diff already has — the computed cost band (band→role/rung via the catalog
# DATA), the listing's context_window / max_output_tokens / capability flags,
# and provider-NEUTRAL name tokens. An optional LLM layer (generators.
# model_discovery.fit_llm) may SHARPEN this verdict and author its reason, but
# it is never a hard dependency: a failure leaves the deterministic verdict
# standing. No provider/model literal participates — band→role/rung is catalog
# DATA, the role set + name tokens are generic vocabulary.

# The FIVE placement verdicts (Addendum 13). There are five KINDS of discovered
# model and only one is rung material — the engine's job is to say WHICH KIND,
# in operator language, never to force an off-ladder model onto the cost axis
# (the ``new-rung`` junk-drawer bug). Rungs back a SINGLE cost-ordered ladder of
# general-purpose models (the roles fast/standard/power/max); a model that "fits
# no rung" usually isn't an analysis failure — it answers a question routing
# doesn't pose.
#
#   ``fits_existing`` — a general-purpose model whose cost/capability matches a
#       role the pod already runs (recommend that role). RUNG MATERIAL.
#   ``new_tier``      — a general-purpose model genuinely pricier/larger than
#       anything in the catalog (the Fable-catch): extends the ladder with a NEW
#       tier (propose a slug, no role yet). RUNG MATERIAL — explicit op action.
#   ``mode_variant``  — a reasoning / non-reasoning / thinking MODE of a model
#       that already maps to a tier (same model, a compute knob turned). Named,
#       NOT a new rung — no role, no tier.
#   ``specialist``    — a domain / workload specialist (coding, multi-agent,
#       creative, math): off the general cost ladder. Named + tracked, NOT
#       routed. (This bucket's accumulation is the future demand signal for the
#       deferred second capability axis — spec §Non-goals #3.)
#   ``cannot_place``  — a genuine gap: no price, unknown family, LLM abstained.
#       The only true "can't tell" (no rung, no role; the operator decides).
PLACEMENT_FITS_EXISTING = "fits_existing"
PLACEMENT_NEW_TIER = "new_tier"
PLACEMENT_MODE_VARIANT = "mode_variant"
PLACEMENT_SPECIALIST = "specialist"
PLACEMENT_CANNOT_PLACE = "cannot_place"

# The roles a discovered model may be recommended for. DATA (role vocabulary),
# not provider/model literals — the band→role mapping is catalog-derived.
FIT_ROLES: tuple[str, ...] = ("fast", "standard", "power", "max", "judge")

# Slugs that must NEVER persist as a real rung id — the ``new-rung`` footgun:
# placeholder / empty / dunno values. Discovery never emits one as a
# ``recommended_rung_slug``; the AdoptModel applier rejects one rather than mint
# a dead cluster (Addendum 13). Imported by the applier so the contract lives in
# ONE place. (These are config-placeholder tokens, not provider/model names.)
PLACEHOLDER_RUNG_SLUGS: frozenset[str] = frozenset(
    {"", "new-rung", "new_rung", "newrung", "none", "null", "tbd", "unplaced"}
)


def is_placeholder_rung_slug(slug: str | None) -> bool:
    """True when ``slug`` is empty / whitespace / a placeholder that must never
    persist as a rung id (the ``new-rung`` footgun guard)."""
    return (slug or "").strip().lower() in PLACEHOLDER_RUNG_SLUGS


# Plain-language description of each role for operator-facing reason strings.
# Vocabulary rule (Addendum 13): the reason speaks tiers and roles in plain
# words — never ``rung`` / ``band`` / ``dormant`` / a position number.
_ROLE_PLAIN: dict[str, str] = {
    "fast": "fast role (the quick, inexpensive model your bots use for everyday calls)",
    "standard": "standard role (your default everyday workhorse)",
    "power": "power role (reserved for harder reasoning work)",
    "max": "max role (your most capable, most expensive model)",
    "judge": "judge role (a second model that cross-checks the others)",
}

# Generic, PROVIDER-NEUTRAL capability words sometimes embedded in a model id.
# SOFT evidence ONLY — they never decide the cost tier; they enrich
# ``fit_evidence`` and give the LLM (and operator) extra context. These are
# capability vocabulary, never provider/model names.
_CAPABILITY_TOKEN_SET: frozenset[str] = frozenset(
    {"reasoning", "thinking", "reasoner", "code", "coder", "coding",
     "vision", "instruct", "chat", "agent"}
)
_CAPABILITY_TOKEN_JOINED: tuple[str, ...] = ("non-reasoning", "multi-agent")


def _capability_name_tokens(model_id: str) -> list[str]:
    """Provider-neutral capability hints parsed from the model id (SOFT signal).

    Splits the id on separators and keeps tokens in the capability vocabulary,
    plus a couple of joined two-word forms (``non-reasoning`` / ``multi-agent``).
    Never decides placement — it is evidence only."""
    low = model_id.lower()
    parts = re.split(r"[-_./ ]+", low)
    found: list[str] = [p for p in parts if p in _CAPABILITY_TOKEN_SET]
    for joined in _CAPABILITY_TOKEN_JOINED:
        if joined in low and joined not in found:
            found.append(joined)
    # ``non-reasoning`` subsumes a bare ``reasoning`` false-positive.
    if "non-reasoning" in found and "reasoning" in found:
        found.remove("reasoning")
    return found


# Two SHARPER, disjoint token vocabularies the placement engine reads (Addendum
# 13). Generic, PROVIDER-NEUTRAL English (the same three-homes posture as the
# size tokens) — never provider/model names. SOFT signals: they steer the KIND
# of placement, never the cost tier.
#
#   MODE tokens — a per-request compute MODE of a base model (reasoning vs
#     non-reasoning vs thinking). A mode token PLUS a base family the pod already
#     runs ⇒ ``mode_variant`` (the same model, a knob turned — not a new tier).
#   WORKLOAD tokens — a domain/workload SPECIALIST (coding / agentic / creative /
#     math). A workload token ⇒ ``specialist`` (off the general cost ladder).
_MODE_TOKENS: frozenset[str] = frozenset({"reasoning", "thinking", "reasoner"})
_MODE_TOKENS_JOINED: tuple[str, ...] = ("non-reasoning", "non-reasoner")
_WORKLOAD_TOKENS: frozenset[str] = frozenset(
    {"code", "coder", "coding", "build", "creative", "math"}
)
_WORKLOAD_TOKENS_JOINED: tuple[str, ...] = ("multi-agent",)

# Plain-language phrase per workload token, for the operator-facing specialist
# reason (no internal jargon). Unknown/absent token → a generic phrase.
_SPECIALIST_KIND: dict[str, str] = {
    "code": "writing code", "coder": "writing code", "coding": "writing code",
    "build": "building software", "creative": "creative writing",
    "math": "mathematical work", "multi-agent": "coordinating multiple agents",
}


def _tokens_from_id(model_id: str, single: frozenset[str], joined: tuple[str, ...]) -> list[str]:
    """Provider-neutral name tokens parsed from the model id (SOFT signal).

    Splits on separators and keeps tokens in ``single``, plus any ``joined``
    two-word form present as a substring. Shared by the mode + workload readers
    so the parse rule lives once."""
    low = model_id.lower()
    parts = re.split(r"[-_./ ]+", low)
    found: list[str] = [p for p in parts if p in single]
    for j in joined:
        if j in low and j not in found:
            found.append(j)
    return found


def _mode_name_tokens(model_id: str) -> list[str]:
    """Compute-MODE tokens in the model id (reasoning / thinking / non-reasoning /
    non-reasoner). SOFT signal — marks a likely mode variant of a base family;
    never a tier."""
    found = _tokens_from_id(model_id, _MODE_TOKENS, _MODE_TOKENS_JOINED)
    # A joined ``non-<x>`` form subsumes the bare ``<x>`` token it contains, so
    # the variant is named once (``non-reasoning`` not ``non-reasoning`` +
    # ``reasoning``; ``non-reasoner`` not + ``reasoner``).
    for joined in _MODE_TOKENS_JOINED:
        bare = joined.split("-", 1)[1]
        if joined in found and bare in found:
            found.remove(bare)
    return found


def _workload_name_tokens(model_id: str) -> list[str]:
    """Domain/WORKLOAD tokens in the model id (code / coder / build / creative /
    math / multi-agent). SOFT signal marking a likely off-ladder specialist."""
    return _tokens_from_id(model_id, _WORKLOAD_TOKENS, _WORKLOAD_TOKENS_JOINED)


def _strip_mode_tokens(model_id: str) -> str:
    """The model id with compute-MODE token segments removed, yielding the BASE
    model the mode varies (``grok-4-reasoning`` → ``grok-4``;
    ``gpt-5-non-reasoning`` → ``gpt-5``). Used to test whether a mode variant's
    base family is one the pod already runs."""
    low = model_id.lower()
    for j in _MODE_TOKENS_JOINED:
        low = low.replace(j, "")
    parts = [p for p in re.split(r"[-_./ ]+", low) if p and p not in _MODE_TOKENS]
    return "-".join(parts)


@dataclass
class PlacementVerdict:
    """The capability-aware fit verdict for a discovered model (Addendum 13)."""

    placement_verdict: str               # PLACEMENT_* constant
    recommended_role: str | None         # real role for fits_existing, else None
    recommended_rung_slug: str | None    # existing/proposed slug, never new-rung
    fit_reason: str                      # one plain-language operator sentence
    fit_confidence: float                # 0..1
    fit_evidence: dict[str, Any]         # cited signals behind the verdict
    fit_source: str = "deterministic"    # "deterministic" | "llm"


def proposed_rung_slug(band: str | None) -> str:
    """A meaningful, PROVIDER-NEUTRAL slug for a brand-new cost tier, derived
    from the cost class DATA (e.g. ``premium`` → ``premium-class``). Never the
    ``new-rung`` placeholder. Empty string when there is no band to derive one
    from (the caller then declines to propose a tier)."""
    if not band:
        return ""
    return f"{band}-class"


# ── Best-per-(provider, role) discovery selection (Addendum 13) ───────────────
# When several discovered models map to the SAME role for the SAME provider
# (the operator's screenshot had three ``gemini-*-flash-lite`` rows all landing
# on ``fast``), only the single best belongs on the adopt card — recommending
# every listed variant of a rung the operator already fills is the busywork this
# rework removes. "Best" is decided PROVIDER-NEUTRALLY (the hard
# [[no-provider-literals-in-logic]] invariant): NEWEST GENERATION first, then
# raw capability. The ranking reads only generic version/date tokens in the id
# and the listing's capability evidence — it NEVER branches on
# ``provider``/``model_id`` identity.

_DATE_TOKEN_RE = re.compile(r"(20\d{2})-?(\d{2})-?(\d{2})")


def _version_tuple(model_id: str) -> tuple[float, ...]:
    """Ordered numeric version tokens in a model id, with date runs removed.

    ``gemini-2.5-flash-lite`` → ``(2.5,)``; ``claude-3-5-haiku`` → ``(3.0, 5.0)``;
    ``gpt-4o`` → ``(4.0,)``. Compared lexicographically, a newer generation
    (higher leading number, then higher minor) sorts greater — and a
    more-specific id (``claude-3-5`` vs ``claude-3``) sorts after its shorter
    prefix. Provider-neutral: it reads digit runs, never a provider/model name.
    """
    stripped = _DATE_TOKEN_RE.sub(" ", model_id)
    return tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", stripped))


def _date_stamp(model_id: str) -> int:
    """Largest ``YYYYMMDD`` date stamp embedded in the id (``…-20250605`` →
    ``20250605``), else 0. A dated snapshot of the same version is the newer
    build, so it breaks a version-tuple tie."""
    best = 0
    for y, mo, da in _DATE_TOKEN_RE.findall(model_id):
        try:
            best = max(best, int(y) * 10000 + int(mo) * 100 + int(da))
        except ValueError:
            continue
    return best


def _rank_evidence(m: "ListedModel") -> dict:
    """The capability fields :func:`model_generation_rank` uses as tiebreakers,
    pulled off a ``ListedModel`` (its ``raw`` is the provider response, which
    may not carry these keys directly)."""
    return {
        "context_window": m.context_window,
        "max_output_tokens": m.max_output_tokens,
    }


def model_generation_rank(model_id: str, evidence: dict | None = None) -> tuple:
    """Provider-neutral ``newer/more-capable is greater`` sort key for choosing
    the single best model among same-(provider, role) discoveries.

    Ordered by: newest generation (version tuple) → newest dated snapshot →
    larger context window → larger max-output. NEVER branches on
    provider/model identity ([[no-provider-literals-in-logic]]); every term is a
    generic numeric token or a listing capability field."""
    ev = evidence or {}

    def _as_int(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return (
        _version_tuple(model_id),
        _date_stamp(model_id),
        _as_int(ev.get("context_window")),
        _as_int(ev.get("max_output_tokens")),
    )


def select_best_per_rung(
    rows: list[dict], *, role_key: str = "recommended_role",
) -> list[dict]:
    """Collapse discovery rows to the single best model per (provider, role).

    ``rows`` are plain dicts carrying at least ``provider``, the role under
    ``role_key`` (falling back to ``role``), ``model_id`` and ``evidence``. For
    each (provider, role) group the row with the greatest
    :func:`model_generation_rank` survives; the rest are dropped (e.g. the three
    ``gemini-*-flash-lite`` → ``fast`` rows collapse to one). Survivor order
    follows the input order of the first row seen per group, so a pre-sorted
    input stays stably ordered. Selection is provider-neutral
    ([[no-provider-literals-in-logic]]) — the only ranking input that touches the
    id is the generic version/date parse."""
    best: dict[tuple[str, str], dict] = {}
    best_rank: dict[tuple[str, str], tuple] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        role = r.get(role_key) or r.get("role") or ""
        key = (r.get("provider") or "", role)
        rank = model_generation_rank(r.get("model_id") or "", r.get("evidence") or {})
        if key not in best:
            best[key] = r
            best_rank[key] = rank
            order.append(key)
        elif rank > best_rank[key]:
            best[key] = r
            best_rank[key] = rank
    return [best[k] for k in order]


def _fits_reason(role: str, source: str) -> str:
    plain = _ROLE_PLAIN.get(role, f"{role} role")
    if source in ("pricing", "observed"):
        basis = "Its price lands right where the models you already run for that work sit"
    elif source == "family-map":
        basis = "It belongs to a model family that already serves that work for you"
    else:  # naming / size hint
        basis = "Its name suggests it's built for that kind of work"
    return f"{basis}, so it's a natural fit for your {plain}."


def _new_tier_reason(band: str) -> str:
    if band == "premium":
        where = "more capable and more expensive than anything you currently run"
    elif band == "low":
        where = "cheaper than anything you currently run"
    else:
        where = "priced unlike anything you currently run"
    return (
        f"This model is {where}, so adopting it would add a new tier to your "
        f"line-up rather than slot into one you already have."
    )


def _cannot_place_reason() -> str:
    return (
        "There isn't enough public information yet to tell where this model "
        "fits — its price and capabilities aren't published — so you'd pick its "
        "tier and role by hand."
    )


def _mode_variant_reason() -> str:
    return (
        "This looks like a different mode of a model you already run — the same "
        "model with a reasoning or thinking setting turned on — so there's "
        "nothing new to add to your line-up."
    )


def _specialist_reason(tokens: list[str]) -> str:
    kind = _SPECIALIST_KIND.get(tokens[0], "a specific kind of work") if tokens else (
        "a specific kind of work"
    )
    return (
        f"This looks like a model built for {kind} rather than a general-purpose "
        f"model, so it sits outside your everyday line-up — worth tracking, but "
        f"not a drop-in for one of your usual roles."
    )


def compute_placement_verdict(
    model: "ListedModel",
    band: str | None,
    band_source: str,
    band_evidence: dict | None,
    known_stems: set[str] | None = None,
) -> PlacementVerdict:
    """Deterministic placement verdict for a discovered model (no LLM).

    Five-verdict decision tree (all catalog-DATA-driven + generic name tokens,
    no provider literals). The first two checks say WHICH KIND of model this is
    before the cost axis is even consulted — an off-ladder model must never be
    forced onto a rung:

      0. **MODE token + base family the pod already runs** → ``mode_variant``
         (a reasoning/thinking mode of a placed model; no role, no tier).
      1. **WORKLOAD token** → ``specialist`` (a domain specialist off the
         general ladder; no role, no tier). A SOFT hint — the LLM may pull a
         genuinely general model back to ``fits_existing``.
      2. **Known cost band → role-bearing tier** → ``fits_existing`` with that
         role + its rung. Authoritative (high confidence) when the band came
         from a real price (``pricing``/``observed``); softer for the
         family-map / naming fallbacks.
      3. **Known cost band but NO role-bearing tier carries that cost class** →
         ``new_tier`` with a proposed (band-derived, never ``new-rung``) slug.
      4. **No band, but a generic size token hints a tier** that maps to a role
         → ``fits_existing`` at LOW confidence (name-based, soft).
      5. **Nothing to go on** → ``cannot_place`` (no rung, no role).

    ``known_stems`` is the set of family stems the pod already runs (from
    :func:`known_family_stems`); a mode variant is recognized only against a
    base family in that set. Omitted (None/empty) → the mode-variant check is
    inert and a mode-tokened model falls through to the band logic — the
    behavior of a direct unit call that doesn't pass the pod's adopted set.

    ``fit_evidence`` cites every signal consulted (band + source + cited band
    evidence, context window, max output, capability flags, name tokens — incl.
    the mode/workload tokens) so the verdict is auditable and the card can show
    its work.
    """
    mode_toks = _mode_name_tokens(model.model_id)
    workload_toks = _workload_name_tokens(model.model_id)
    evidence: dict[str, Any] = {
        "cost_band": band,
        "cost_band_source": band_source,
        "cost_band_evidence": dict(band_evidence or {}),
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "capability_flags": list(model.capabilities or []),
        "capability_name_tokens": _capability_name_tokens(model.model_id),
        "mode_name_tokens": mode_toks,
        "workload_name_tokens": workload_toks,
    }

    # 0 — MODE VARIANT of an already-placed model (kind 2). A mode token PLUS a
    # base family the pod already runs: same model, a different compute mode —
    # not rung material. No role, no tier (the base is already placed). The
    # family match is concrete, so this outranks the cost axis.
    if mode_toks and known_stems:
        base_stem = _family_of(_strip_mode_tokens(model.model_id))
        if base_stem and base_stem in known_stems:
            evidence["mode_base_family"] = base_stem
            return PlacementVerdict(
                PLACEMENT_MODE_VARIANT, None, None,
                _mode_variant_reason(), 0.7, evidence,
            )

    # 1 — WORKLOAD SPECIALIST (kind 3). A domain/workload token → off the
    # general cost ladder, regardless of what it would cost. Named + tracked,
    # never force-fit onto a tier. SOFT (conf 0.5) so a confident LLM can pull a
    # genuinely general model back to fits_existing — but the default refuses to
    # route a specialist into a general role.
    if workload_toks:
        return PlacementVerdict(
            PLACEMENT_SPECIALIST, None, None,
            _specialist_reason(workload_toks), 0.5, evidence,
        )

    # 2 / 3 — a known band.
    if band:
        slug, role = rung_for_band(band)
        if slug and role:
            authoritative = band_source in ("pricing", "observed")
            if authoritative:
                conf = 0.9
            elif band_source == "family-map":
                conf = 0.75
            else:
                conf = 0.55
            return PlacementVerdict(
                PLACEMENT_FITS_EXISTING, role, slug,
                _fits_reason(role, band_source), conf, evidence,
            )
        # Band known, but no role-bearing tier carries this cost class → a tier
        # the pod doesn't have yet. Propose a band-derived slug, no role.
        proposed = proposed_rung_slug(band)
        conf = 0.6 if band_source in ("pricing", "observed") else 0.45
        return PlacementVerdict(
            PLACEMENT_NEW_TIER, None, (proposed or None),
            _new_tier_reason(band), conf, evidence,
        )

    # 4 — no band, soft size-name hint that maps to a role.
    size_band = _band_from_size_naming(model.model_id)
    if size_band:
        slug, role = rung_for_band(size_band)
        if slug and role:
            return PlacementVerdict(
                PLACEMENT_FITS_EXISTING, role, slug,
                _fits_reason(role, "naming"), 0.4, evidence,
            )

    # 5 — cannot place. No rung suggested (the operator decides).
    return PlacementVerdict(
        PLACEMENT_CANNOT_PLACE, None, None, _cannot_place_reason(), 0.2, evidence,
    )


# ── Role → rung map (catalog-DATA-driven, no provider literals) ────────────────
# The complement of ``_band_to_rung_map``: a discovered model the LLM places in
# a ROLE (e.g. "this is a fast-role model") needs that role's existing rung
# slug. Inverted straight from the catalog ``roles`` DATA — names no provider.

def _role_to_rung_map() -> dict[str, str]:
    """Map each role → its rung slug from ``DEFAULT_MODEL_CATALOG``'s roles."""
    try:
        from primary_bot import default_model_catalog  # type: ignore
        cat = default_model_catalog()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for role, entry in (cat.get("roles") or {}).items():
        slug = entry if isinstance(entry, str) else (
            entry.get("rung") if isinstance(entry, dict) else None
        )
        if isinstance(slug, str) and slug:
            out[role] = slug
    return out


def rung_slug_for_role(role: str) -> str:
    """The existing rung slug a ``role`` points at, from the catalog roles DATA.
    Empty string when the role isn't in the catalog (the caller declines)."""
    return _role_to_rung_map().get(role, "")


# ── The diff engine ───────────────────────────────────────────────────────────

def default_catalog_bare_ids() -> set[str]:
    """Return the bare, lowercased model ids in :data:`DEFAULT_MODEL_CATALOG`.

    Evolve's blessed ladder ships in code, so these are KNOWN on every pod
    regardless of pod/bot config (spec §Addendum 2.4). Exposed separately from
    :func:`known_model_set` so the failure-posture in :func:`run_discovery` can
    tell the always-present defaults apart from the POD-SOURCED adopted set:
    the suppress-on-empty-degraded guard must key off the pod's own set being
    empty, not the defaults masking it.
    """
    out: set[str] = set()
    try:
        from primary_bot import DEFAULT_MODEL_CATALOG  # type: ignore
    except ImportError:
        return out
    for rung in (DEFAULT_MODEL_CATALOG.get("rungs") or []):
        for m in (rung.get("models") or []):
            out.add(_bare_id(str(m)).lower())
    return out


def known_model_set(
    network: dict,
    bot_configs: dict[str, dict] | None = None,
) -> tuple[set[str], bool, set[str]]:
    """Return ``(known_bare, degraded, pod_sourced)`` — the pod's adopted model
    set (bare ids, lowercased), whether any source could not be read, and the
    POD-SOURCED subset (everything EXCEPT the code defaults).

    ``pod_sourced`` is the union of the readable pod/bot config sources (steps
    1-3 below) — it is NOT ``known`` minus the default *ids*, because a pod that
    legitimately configures default-ladder models has a non-empty pod source
    even though those ids coincide with defaults. The
    suppress-on-empty-degraded guard in :func:`run_discovery` keys off this set
    being empty (no readable pod source contributed anything), so it must
    measure "did a pod source yield a model", not "did a pod source yield a
    NON-default model".

    Sources, all unioned into ``known``:
      - **The code-shipped ``DEFAULT_MODEL_CATALOG``** (spec §Addendum 2):
        Evolve's blessed ladder is KNOWN on every pod, so default-ladder
        models are never "discoveries" even with no per-pod/-bot config.
      - **Every pod member bot's** ``~/.openclaw/evolve-tiers.json``
        (rungs + legacy tiers shapes) — THE primary source on a real pod,
        read via ``primary_bot.bot_evolve_tiers_models``. The rungs/roles
        config lives here per-bot, NOT in ``network.json`` (the live-canary
        finding that motivated this: ``network.json`` carried no models
        section, so the old read returned ``known_model_count: 0`` and
        re-"discovered" already-adopted sonnet/opus). Pod membership comes
        from ``network.json::bots`` — never a directory scan.
      - ``network.json::models.rungs[].models`` and the legacy
        ``models.tiers.*.models`` — present only on pods that pin the config
        at the network level. Folded in when present.
      - ``bot_configs`` (the existing ``oc_full_config`` per-bot reads):
        each bot's ``tiers.*.models`` and ``models.rungs[].models``.

    ``degraded`` is True when at least one bot's tiers file existed but could
    not be read or parsed (a degraded read — see
    ``bot_evolve_tiers_models``). The caller MUST treat an empty-known-set +
    degraded run as "do not emit discoveries" (see ``run_discovery``):
    emitting against an empty set guarantees a noise flood of already-adopted
    models. A genuinely fresh, rung-less pod (empty set, NO errors) is valid
    and discovery proceeds normally.
    """
    known: set[str] = set()
    pod_sourced: set[str] = set()  # steps 1-3 only — excludes the code defaults
    degraded = False

    # 0. DEFAULT_MODEL_CATALOG — Evolve's blessed ladder ships in code, so its
    #    models are KNOWN on every pod regardless of pod/bot config (spec
    #    §Addendum 2.4). Without this, a default-ladder model with no per-pod
    #    config anywhere (e.g. claude-fable-5 on a pod that never adopted Fable)
    #    would surface as a fresh discovery every run; unioning the defaults
    #    closes that lifecycle (model known -> signature not kept -> signal
    #    resolves -> any pending AdoptModel proposal sweep-resolves).
    #    NOT folded into ``pod_sourced`` — these ship in code, not from a pod
    #    source, so they must not mask an empty-pod degraded-read.
    default_bare = default_catalog_bare_ids()
    known |= default_bare

    # 1. Per-bot evolve-tiers.json — the real source of truth on a live pod.
    try:
        from primary_bot import bot_evolve_tiers_models  # type: ignore

        for bot_id in (network.get("bots") or {}).keys():
            try:
                models, ok = bot_evolve_tiers_models(network, bot_id)
            except Exception:
                # An unexpected reader error is a degraded read, never
                # silently "this bot knows nothing".
                models, ok = set(), False
            if not ok:
                degraded = True
            for m in models:
                bare = _bare_id(str(m)).lower()
                known.add(bare)
                pod_sourced.add(bare)
    except ImportError:
        # primary_bot unavailable (shouldn't happen in-package) — fall through
        # to the network/bot_configs sources without crashing.
        pass

    # 2. network.json models block (rungs new shape + legacy tiers), present
    #    only on pods that pin config at the network level.
    models_cfg = (network.get("models") or {})
    for rung in (models_cfg.get("rungs") or []):
        for m in (rung.get("models") or []):
            bare = _bare_id(str(m)).lower()
            known.add(bare)
            pod_sourced.add(bare)
    for tier_entry in (models_cfg.get("tiers") or {}).values():
        for m in ((tier_entry or {}).get("models") or []):
            bare = _bare_id(str(m)).lower()
            known.add(bare)
            pod_sourced.add(bare)

    # 3. Existing oc_full_config per-bot reads (legacy tiers + own rungs).
    for cfg in (bot_configs or {}).values():
        if not isinstance(cfg, dict):
            continue
        tiers = cfg.get("tiers") or {}
        for tier_entry in tiers.values():
            for m in ((tier_entry or {}).get("models") or []):
                bare = _bare_id(str(m)).lower()
                known.add(bare)
                pod_sourced.add(bare)
        # Also fold the bot's own rungs if it carries the new shape.
        for rung in ((cfg.get("models") or {}).get("rungs") or []):
            for m in (rung.get("models") or []):
                bare = _bare_id(str(m)).lower()
                known.add(bare)
                pod_sourced.add(bare)

    return known, degraded, pod_sourced


def load_ignore_list(shared_dir: Path) -> set[str]:
    """Operator-editable ignore list of bare model ids that should never
    surface as discoveries. Stored alongside the other freshness state at
    ``{shared_dir}/model-freshness/discovery-ignore.json`` (consistent with
    ``model_registry``'s dismissals.json / last-check.json location).

    Shape: ``{"ignore": ["openai/o1-pro", "some-model"]}``. Entries may be
    bare or provider-qualified; both normalize to bare-lowercase.
    """
    path = Path(shared_dir) / "model-freshness" / "discovery-ignore.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    out: set[str] = set()
    for entry in (data.get("ignore") or []):
        if isinstance(entry, str) and entry.strip():
            out.add(_bare_id(entry).lower())
    return out


def diff_listing(
    enumerations: list[ProviderEnumeration],
    known_bare: set[str],
    ignore_list: set[str],
    pricing_cache: dict | None = None,
) -> tuple[list[DiscoveryFinding], list[StalenessFinding], int, int]:
    """Partition successful enumerations into discoveries + staleness.

    Returns ``(discoveries, staleness, ignored_count, skipped_count)``. Only
    consults ``ok=True`` enumerations — degraded providers contribute nothing
    here (they're surfaced separately as degraded, never as 'current').

    ``ignored_count`` counts hard out-of-scope models (non-chat, operator
    ignore-listed, snapshot aliases). ``skipped_count`` counts the **frontier
    filter** dropouts: unknown chat-capable models whose family is ALREADY
    adopted — they're not new lines, so they're not discoveries (a newer one is
    a staleness finding on the known member; an older one is stale catalog).
    Both counts are surfaced so the filtering is visible, never silent.

    ``pricing_cache`` (Addendum 8 §C) is the 11b-1 ``model-pricing.json`` doc,
    read once for this run. Each discovery's ``suggested_cost_class`` is then
    **pricing-derived** (exact price → anchor-relative band; else family-map),
    and the finding carries cited pricing evidence (``cost_band_source`` +
    ``cost_band_evidence``). When pricing AND the family map both miss, the band
    falls back to the family-naming heuristic and ``cost_band_source`` is
    ``"heuristic"`` so the proposal flags the placement as un-priced (the
    cite-or-don't-recommend rule).
    """
    discoveries: list[DiscoveryFinding] = []
    staleness: list[StalenessFinding] = []
    ignored = 0
    skipped = 0

    # Families the pod has already adopted a member of — the frontier filter
    # uses this to decide "new line (discover)" vs "within an adopted family
    # (skip)". Built once from the known set.
    known_stems = known_family_stems(known_bare)

    # Every bare id that appears in the listing — used to suppress dated
    # snapshot aliases whose base id also appears in the listing (the base
    # entry already represents the line; the pinned snapshot is noise).
    listing_bare: set[str] = set()
    for enum in enumerations:
        if not enum.ok:
            continue
        for m in enum.models:
            listing_bare.add(m.model_id.lower())

    # Build family → latest-listed-model index per provider for staleness.
    # "latest" = the NUMERICALLY-highest version within a family from the LISTING
    # via ``model_generation_rank`` — never a lexicographic string compare (which
    # sorts ``claude-sonnet-10`` BELOW ``claude-sonnet-4-5``). The listing is the
    # source of truth now.
    family_latest: dict[tuple[str, str], ListedModel] = {}
    for enum in enumerations:
        if not enum.ok:
            continue
        for m in enum.models:
            if not is_chat_capable(m):
                continue
            fam = (m.provider, _family_of(m.model_id))
            cur = family_latest.get(fam)
            if cur is None or model_generation_rank(
                m.model_id, _rank_evidence(m)
            ) > model_generation_rank(cur.model_id, _rank_evidence(cur)):
                family_latest[fam] = m

    for enum in enumerations:
        if not enum.ok:
            continue
        for m in enum.models:
            bare = m.model_id.lower()

            # Out of scope: non-chat models filtered first.
            if not is_chat_capable(m):
                ignored += 1
                continue
            # Out of scope: explicit operator ignore.
            if bare in ignore_list:
                ignored += 1
                continue
            # Out of scope: dated snapshot alias of a known model.
            if is_snapshot_alias_of_known(m, known_bare):
                ignored += 1
                continue
            # Out of scope: dated snapshot alias whose base id also appears in
            # this listing (pinned snapshot of a line already represented by its
            # base entry — e.g. opus-4-5-20251101 alongside opus-4-5). This
            # catches snapshots of UNKNOWN models too, so a single new line with
            # a dated alias surfaces once (via its base), not twice.
            if is_snapshot_alias_of_listing(m, listing_bare):
                ignored += 1
                continue

            if bare in known_bare:
                # Known model → within-family staleness (latest from listing).
                fam = (m.provider, _family_of(m.model_id))
                latest = family_latest.get(fam)
                if latest is not None and latest.model_id.lower() != bare:
                    staleness.append(StalenessFinding(
                        provider=m.provider,
                        family=fam[1],
                        current_model=m.qualified_id,
                        latest_model=latest.qualified_id,
                        evidence={
                            "latest": latest.to_dict(),
                            "current": m.to_dict(),
                        },
                    ))
                continue

            # ── Frontier filter ──────────────────────────────────────────────
            # An unknown chat-capable model is discovery-worthy ONLY if it's on
            # the frontier: its family has NO adopted member. If the family is
            # already adopted, this is a within-family variant —
            #   • a NEWER member is surfaced as a staleness finding on the known
            #     model above (the upgrade nudge), so it must not ALSO discover;
            #   • an OLDER member is stale catalog noise.
            # Either way it's NOT a new capability line → skip (counted).
            #
            # Fail-open: a name that doesn't parse into a family (stem == bare,
            # i.e. no version run to strip — like "o3" or "grok-4") has a stem
            # that won't collide with any adopted ``claude-*``/``gpt-*`` stem, so
            # it falls through to discovery. That's the intended visibility for
            # genuinely alien names.
            stem = _family_of(m.model_id)
            if stem in known_stems:
                skipped += 1
                continue

            # Unknown chat-capable model on the frontier → discovery finding.
            #
            # Cost band is PRICING-DERIVED (Addendum 8 §C), not hand-typed:
            # exact price → anchor-relative band; cache miss → family-map band;
            # both miss → band is None and the rung suggestion falls back to a
            # provider-NEUTRAL size-naming hint / unplaced (cite-or-don't-
            # recommend: cost_band_source="heuristic"). resolve_band never
            # fabricates a band — an unknown family returns None.
            band_res = model_cost_bands.resolve_band(
                m.provider, m.model_id, cache=pricing_cache,
            )

            # The suggested rung is derived from the cost BAND (band → rung via
            # the rung costClass DATA), NOT from the model's provider — so a
            # low-band model from ANY provider suggests fast/haiku-class, a
            # high-band model power/opus-class. No per-provider branches.
            rung, rationale = suggest_rung_placement(m, known_bare, band_res.band)

            if band_res.band is not None:
                cost_class = band_res.band
                band_source = band_res.source
                band_evidence = dict(band_res.evidence)
            else:
                # Un-priced + unknown family: the band is whatever the size-
                # naming fallback yielded (or the default), FLAGGED un-priced so
                # the proposal can't present it as cited.
                cost_class = _band_from_size_naming(m.model_id) or "medium"
                band_source = "heuristic"
                band_evidence = {"family": _family_of(m.model_id)}

            # Capability-aware placement verdict (Addendum 13). Deterministic
            # here; the generator (observe.py) optionally sharpens it with one
            # cheap LLM fit call and authors the operator-facing reason — but a
            # missing LLM leaves THIS verdict standing (fail-open).
            verdict = compute_placement_verdict(
                m, band_res.band, band_source, band_evidence,
                known_stems=known_stems,
            )
            # ``suggested_rung_slug`` mirrors the verdict's recommended slug —
            # the existing/proposed rung for fits_existing/new_tier, or "" for
            # cannot_place (NEVER the ``new-rung`` placeholder; the applier
            # rejects "" so an unplaceable model can't mint a dead rung).
            legacy_slug = verdict.recommended_rung_slug or ""

            discoveries.append(DiscoveryFinding(
                provider=m.provider,
                model_id=m.model_id,
                qualified_id=m.qualified_id,
                evidence=m.to_dict(),
                suggested_rung=rung,
                suggested_rationale=rationale,
                suggested_rung_slug=legacy_slug,
                suggested_cost_class=cost_class,
                cost_band_source=band_source,
                cost_band_evidence=band_evidence,
                # Position is a cost-rank insertion index; the diff engine
                # doesn't carry the pod rungs[] array, so the generator
                # (observe.py) recomputes it against the live catalog via
                # suggest_rung_position. Default 0 = cheapest end until then.
                suggested_position=0,
                placement_verdict=verdict.placement_verdict,
                recommended_role=verdict.recommended_role,
                recommended_rung_slug=verdict.recommended_rung_slug,
                fit_reason=verdict.fit_reason,
                fit_confidence=verdict.fit_confidence,
                fit_evidence=verdict.fit_evidence,
                fit_source=verdict.fit_source,
            ))

    # Dedup staleness by (current_model) — one finding per stale family member.
    seen_stale: set[str] = set()
    deduped_stale: list[StalenessFinding] = []
    for s in staleness:
        if s.current_model in seen_stale:
            continue
        seen_stale.add(s.current_model)
        deduped_stale.append(s)

    return discoveries, deduped_stale, ignored, skipped


# ── Version freshness — the PRIMARY function (spec §Addendum 15) ───────────────
# The everyday operator action is "ride the latest version of the model class I
# already chose" (Sonnet 4-5 → Sonnet 5, Opus 4-7 → 4-8) — NOT discovering a
# brand-new model line. This is the deterministic pass that powers it:
#   1. ``known_model_locations`` — every model the pod runs, mapped to the rung
#      it occupies + the roles that route through that rung (the "what + where").
#   2. ``compute_version_upgrades`` — for each, find the numerically-newest
#      same-(provider, family) chat-capable model in the live listing, and emit
#      an upgrade record when it beats what the pod runs.
# An upgrade carries the rung_slug so it is one-click-appliable via the
# AdoptModel applier (splice the latest model into that rung AHEAD of the
# predecessor it upgrades — the resolver routes to the first credentialed
# cluster member, not the newest, so leading the predecessor is what makes
# routing move; the role re-point is then a no-op).


def known_model_locations(
    network: dict,
    bot_configs: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Map each model the pod runs (bare, lowercased id) → its on-pod location.

    Returns ``{bare_id: {"qualified", "provider", "rung_slug", "roles": [...]}}``.

    Built by walking the MERGED model catalog(s) (``DEFAULT ← pod ← bot``) rung
    clusters DIRECTLY — so a dormant catalog entry (a model in a rung no role
    points at, e.g. one adopted "as dormant") is covered too, not just the
    models a role resolves to. The merged catalog is exactly what the gateway
    routes from and what the ``AdoptModel`` applier extends, so ``rung_slug`` is
    the right apply target for riding the latest version.

    Sources, unioned (first writer wins for a model's rung; roles unioned):
      - the pod-merged catalog (``DEFAULT_MODEL_CATALOG ← network.json::models``);
      - each pod-member bot's ``evolve-tiers.json`` merged over the pod layer.

    Best-effort: if ``primary_bot`` can't be imported, falls back to walking
    ``network.json::models.rungs`` + the default catalog directly (no per-bot
    layer), which still locates every default-ladder and pod-pinned model.
    """
    pod_models = (network or {}).get("models")
    pod_models = pod_models if isinstance(pod_models, dict) else {}

    merged_catalogs: list[dict] = []
    try:
        from primary_bot import merge_model_catalog  # type: ignore

        # Pod-level merged view (defaults ← pod) — authoritative rung/role map.
        merged_catalogs.append(merge_model_catalog(pod_models, {}))
        # Per-bot merged views (defaults ← pod ← bot) — fold any model a bot
        # pins in its own evolve-tiers.json that the pod layer doesn't carry.
        try:
            from primary_bot import (  # type: ignore
                _bot_evolve_tiers_path, _read_bot_owned_json,
            )
        except Exception:
            _bot_evolve_tiers_path = None  # type: ignore
            _read_bot_owned_json = None  # type: ignore
        if _bot_evolve_tiers_path is not None and _read_bot_owned_json is not None:
            for bot_id in (network.get("bots") or {}).keys():
                try:
                    data, _ok = _read_bot_owned_json(
                        _bot_evolve_tiers_path(network, bot_id)
                    )
                    bot_doc = data if isinstance(data, dict) else {}
                except Exception:
                    bot_doc = {}
                try:
                    merged_catalogs.append(merge_model_catalog(pod_models, bot_doc))
                except Exception:
                    continue
    except ImportError:
        # primary_bot unavailable — walk the pod rungs (+ default catalog) raw.
        merged_catalogs.append(_pod_catalog_fallback(pod_models))

    # Also fold any rungs carried directly on bot_configs (the oc_full_config
    # per-bot reads), mirroring known_model_set's source list.
    for cfg in (bot_configs or {}).values():
        if isinstance(cfg, dict) and isinstance(cfg.get("models"), dict):
            merged_catalogs.append(cfg["models"])

    out: dict[str, dict] = {}
    for cat in merged_catalogs:
        if not isinstance(cat, dict):
            continue
        rung_roles = _rung_to_roles(cat)
        for rung in (cat.get("rungs") or []):
            if not isinstance(rung, dict):
                continue
            slug = rung.get("id")
            if not isinstance(slug, str) or not slug:
                continue
            for m in (rung.get("models") or []):
                if not isinstance(m, str) or not m:
                    continue
                bare = _bare_id(m).lower()
                roles = rung_roles.get(slug, [])
                entry = out.get(bare)
                if entry is None:
                    out[bare] = {
                        "qualified": m,
                        "provider": _provider_of_model(m),
                        "rung_slug": slug,
                        "roles": list(roles),
                    }
                else:
                    # Union the roles; keep the first-seen rung/qualified.
                    for r in roles:
                        if r not in entry["roles"]:
                            entry["roles"].append(r)
    return out


def pod_sourced_model_locations(
    network: dict,
    bot_configs: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """:func:`known_model_locations` restricted to models a POD SOURCE actually
    configured (``network.json`` rungs/tiers, per-bot ``evolve-tiers.json``,
    bot_configs) — excluding models present ONLY as code-default seeds.

    This is the set version-freshness acts on (spec §Addendum 15 scope note): a
    model the operator never configured but which ships as a default-catalog
    seed alternative (e.g. an ``openai`` member of a rung on an Anthropic-only
    pod) must NOT generate an "update me" nudge — keeping the default catalog's
    own version numbers current is a maintenance code-PR, not this surface's job
    (and this surface never runtime-writes code). The merged catalog still
    supplies the LOCATION (rung + roles) for the pod-sourced models; we just
    don't surface upgrades for the pure-default rows.
    """
    _known, _degraded, pod_sourced = known_model_set(network, bot_configs)
    all_locations = known_model_locations(network, bot_configs)
    return {k: v for k, v in all_locations.items() if k in pod_sourced}


def _pod_catalog_fallback(pod_models: dict) -> dict:
    """A merged-catalog stand-in when ``primary_bot`` can't be imported: the
    code default catalog with the pod's rungs/roles overlaid by id. Keeps
    ``known_model_locations`` working (defaults + pod-pinned) without the
    per-bot layer."""
    import copy

    cat: dict = {"rungs": [], "roles": {}}
    try:
        from primary_bot import DEFAULT_MODEL_CATALOG  # type: ignore

        cat = copy.deepcopy(DEFAULT_MODEL_CATALOG)
    except Exception:
        cat = {"rungs": [], "roles": {}}
    # Overlay pod rungs by id; pod roles wholesale.
    by_id = {r.get("id"): r for r in cat.get("rungs", []) if isinstance(r, dict)}
    for rung in (pod_models.get("rungs") or []):
        if isinstance(rung, dict) and rung.get("id"):
            by_id[rung["id"]] = rung
    cat["rungs"] = list(by_id.values())
    if isinstance(pod_models.get("roles"), dict):
        cat["roles"] = {**cat.get("roles", {}), **pod_models["roles"]}
    return cat


def _rung_to_roles(catalog: dict) -> dict[str, list[str]]:
    """Invert a catalog's ``roles`` map → ``{rung_slug: [role, ...]}``. A role
    entry may be a bare slug or a ``{"rung": ...}`` object (judge)."""
    out: dict[str, list[str]] = {}
    for role, entry in (catalog.get("roles") or {}).items():
        slug = entry if isinstance(entry, str) else (
            entry.get("rung") if isinstance(entry, dict) else None
        )
        if isinstance(slug, str) and slug:
            out.setdefault(slug, []).append(role)
    return out


def _provider_of_model(qualified: str) -> str:
    """Provider prefix of a qualified model id (``"anthropic/x"`` → ``anthropic``),
    or ``""`` for a bare id."""
    return qualified.split("/", 1)[0].lower() if "/" in qualified else ""


def _is_strictly_newer(candidate_id: str, current_id: str) -> bool:
    """True when ``candidate_id`` is a numerically-newer version than
    ``current_id`` (same family assumed). Compares the generic numeric version
    tuple, then the dated-snapshot stamp — NEVER a lexicographic string compare.
    Capability fields are deliberately NOT consulted: a version bump must be a
    real version bump, not a listing that merely carries more metadata for the
    same model."""
    return (
        (_version_tuple(candidate_id), _date_stamp(candidate_id))
        > (_version_tuple(current_id), _date_stamp(current_id))
    )


def compute_version_upgrades(
    listing_by_provider: dict[str, list["ListedModel"]],
    known_locations: dict[str, dict],
) -> list[VersionUpgrade]:
    """The PRIMARY freshness pass (spec §Addendum 15): for every model the pod
    runs, surface the newest same-class version available in the live listing.

    ``listing_by_provider`` maps each provider → its enumerated ``ListedModel``
    list (``run_discovery`` builds it from the ok enumerations; the card path
    hydrates it from the listings cache via :func:`hydrate_listing_cache`).
    ``known_locations`` is :func:`known_model_locations`'s output.

    For each known model: find the numerically-newest same-(provider, family)
    chat-capable listed member (``model_generation_rank``, never lexicographic);
    if it is strictly newer than what the pod runs, emit a :class:`VersionUpgrade`
    carrying the rung the predecessor occupies so the upgrade is one-click
    appliable. Dated snapshot aliases whose base id also appears in the listing
    are excluded as upgrade targets (the rolling base id is what you ride).

    Deduped to ONE upgrade per (provider, family, rung_slug), keeping the
    MOST-current stale member as ``current_model`` (so the card says "you're on
    Sonnet 4-6 → Sonnet 5", not "…on Sonnet 4-0").
    """
    # Newest chat-capable member per (provider, family), excluding pinned dated
    # snapshots that have a base entry in the same listing.
    listing_bare: dict[str, set[str]] = {}
    for provider, models in listing_by_provider.items():
        listing_bare[provider] = {m.model_id.lower() for m in models}
    newest: dict[tuple[str, str], ListedModel] = {}
    for provider, models in listing_by_provider.items():
        for m in models:
            if not is_chat_capable(m):
                continue
            if is_snapshot_alias_of_listing(m, listing_bare.get(provider, set())):
                continue
            fam = (provider, _family_of(m.model_id))
            cur = newest.get(fam)
            if cur is None or model_generation_rank(
                m.model_id, _rank_evidence(m)
            ) > model_generation_rank(cur.model_id, _rank_evidence(cur)):
                newest[fam] = m

    # One candidate per (provider, family, rung); keep the most-current stale
    # member so the displayed "you're on X" is the newest thing actually run.
    best: dict[tuple[str, str, str], VersionUpgrade] = {}
    for bare, loc in known_locations.items():
        provider = loc.get("provider") or ""
        if not provider:
            continue
        fam = _family_of(bare)
        latest = newest.get((provider, fam))
        if latest is None:
            continue
        if latest.model_id.lower() == bare:
            continue  # pod already runs the newest listed member
        if not _is_strictly_newer(latest.model_id, bare):
            continue  # pod runs same-or-newer than the listing (no downgrade)
        rung_slug = loc.get("rung_slug") or ""
        # If this rung ALREADY carries the family's latest member (e.g. the
        # upgrade was applied earlier and the predecessor lingers as a
        # fallback), the upgrade is done — surfacing it again would make the
        # card nag forever and never clear its count after "Update all to
        # latest". The lingering older member is a harmless fallback, not a
        # pending upgrade. Apply already de-dupes; this de-dupes the SURFACE.
        latest_loc = known_locations.get(latest.model_id.lower())
        if latest_loc is not None and (latest_loc.get("rung_slug") or "") == rung_slug:
            continue
        key = (provider, fam, rung_slug)
        upgrade = VersionUpgrade(
            provider=provider,
            family=fam,
            current_model=loc.get("qualified") or f"{provider}/{bare}",
            latest_model=latest.qualified_id,
            rung_slug=rung_slug,
            roles=list(loc.get("roles") or []),
            evidence={"latest": latest.to_dict()},
        )
        existing = best.get(key)
        if existing is None or _is_strictly_newer(
            bare, _bare_id(existing.current_model)
        ):
            best[key] = upgrade

    return sorted(best.values(), key=lambda u: (u.provider, u.family, u.rung_slug))


def hydrate_listing_cache(cache: dict) -> dict[str, list["ListedModel"]]:
    """Rebuild ``{provider: [ListedModel, ...]}`` from a persisted listings
    cache doc (``build_listings_cache`` output) so the card's version-upgrade
    pass can run off the cache with no live enumeration. Unknown keys in a cache
    record are ignored; the ``family`` / ``is_family_latest`` annotations the
    cache adds are dropped (recomputed deterministically downstream)."""
    out: dict[str, list[ListedModel]] = {}
    fields = {
        "provider", "model_id", "qualified_id", "display_name", "created",
        "context_window", "max_output_tokens", "capabilities",
    }
    for provider, models in (cache.get("providers") or {}).items():
        rows: list[ListedModel] = []
        for rec in (models or []):
            if not isinstance(rec, dict):
                continue
            kwargs = {k: rec[k] for k in fields if k in rec}
            mid = kwargs.get("model_id")
            if not mid:
                continue
            kwargs.setdefault("provider", provider)
            kwargs.setdefault("qualified_id", f"{provider}/{mid}")
            try:
                rows.append(ListedModel(**kwargs))
            except TypeError:
                continue
        out[provider] = rows
    return out


# ── Listings cache (Phase 9 — the validated-picker candidate source) ──────────
# Each discovery run enumerates every credentialed provider's live /v1/models
# (the authoritative, correctly-spelled current list). The diff uses them
# ephemerally; Phase 9 persists them so the admin tier-editors can offer a
# validated picker instead of free-text model entry. The listing IS data — no
# provider/model literals in logic anywhere downstream.

LISTINGS_CACHE_NAME = "model-listings.json"


def listings_cache_path(shared_dir: Path) -> Path:
    """Path of the persisted enumerated-listings cache. Top-level under
    ``{shared_dir}`` (evolve-owned — atomic temp+rename write, no sudo)."""
    return Path(shared_dir) / LISTINGS_CACHE_NAME


def build_listings_cache(
    enumerations: list[ProviderEnumeration], *, refreshed_at: str,
) -> dict:
    """Shape the cache document from a run's per-provider enumerations.

    ``providers`` maps each *successfully enumerated* provider to its list of
    serialized ListedModel dicts (canonical ids + whatever capability fields
    the listing carried). ``degraded`` records providers whose listing call
    failed — recorded with a reason so the UI shows "couldn't refresh X"
    rather than silently dropping it (the silent-monitor lesson).

    Each serialized model carries ``family`` (its ``_family_of`` stem) and
    ``is_family_latest`` (True for the latest-version member of each family
    within its provider's listing). The catalog's exhaustive add-model picker
    bolds the latest-in-family option to reduce version confusion (Phase 10b /
    Addendum 7 item 5). "Latest" reuses the same NUMERIC
    ``model_generation_rank`` rule the staleness / version-upgrade index uses
    above (NEVER a lexicographic compare — which would sort
    ``claude-sonnet-10`` below ``claude-sonnet-4-5``); the family computation is
    NOT reimplemented here, it calls ``_family_of``.
    """
    providers: dict[str, list[dict]] = {}
    degraded: list[dict[str, str]] = []
    for enum in enumerations:
        if not enum.ok:
            degraded.append({"provider": enum.provider, "reason": enum.reason})
            continue
        # Pick the latest-version model per family within this provider's
        # listing (same rule as the staleness family_latest index: numerically
        # newest wins). family_of reused — no family regex duplicated here.
        latest_by_family: dict[str, ListedModel] = {}
        for m in enum.models:
            fam = _family_of(m.model_id)
            cur = latest_by_family.get(fam)
            if cur is None or model_generation_rank(
                m.model_id, _rank_evidence(m)
            ) > model_generation_rank(cur.model_id, _rank_evidence(cur)):
                latest_by_family[fam] = m
        serialized: list[dict] = []
        for m in enum.models:
            fam = _family_of(m.model_id)
            d = m.to_dict()
            d["family"] = fam
            latest = latest_by_family.get(fam)
            d["is_family_latest"] = latest is not None and latest.model_id == m.model_id
            serialized.append(d)
        providers[enum.provider] = serialized
    return {
        "refreshed_at": refreshed_at,
        "providers": providers,
        "degraded": degraded,
    }


def write_listings_cache(
    shared_dir: Path,
    enumerations: list[ProviderEnumeration],
    *,
    refreshed_at: str,
) -> Path:
    """Atomically persist the enumerated listings to the cache.

    Temp-file + rename in ``{shared_dir}`` (same filesystem → atomic rename),
    owned by the evolve user — no /tmp staging or sudo (the dir carries the
    evolve ACL). Returns the cache path. Best-effort: the caller wraps this so
    a write failure never breaks a discovery run, but logs (never silent).
    """
    path = listings_cache_path(shared_dir)
    doc = build_listings_cache(enumerations, refreshed_at=refreshed_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return path


def listing_model_ids(cache: dict, providers: set[str] | None = None) -> dict[str, str]:
    """Index a cache document to ``{lowercased-id: canonical-id}`` across every
    listed model in every provider's listing. Both the bare ``model_id`` and
    the ``{provider}/{model_id}`` qualified form map to the canonical
    qualified id, so a free-text entry typed either way validates.

    ``providers`` (when given) restricts to that set — the credentialed filter
    arrives pre-computed from the route (no provider-name literals here).
    """
    out: dict[str, str] = {}
    for provider, models in (cache.get("providers") or {}).items():
        if providers is not None and provider not in providers:
            continue
        for m in (models or []):
            if not isinstance(m, dict):
                continue
            canonical = m.get("qualified_id") or m.get("model_id")
            if not canonical:
                continue
            bare = m.get("model_id") or ""
            for variant in (canonical, bare):
                if variant:
                    out.setdefault(variant.lower(), canonical)
    return out


def _record_is_chat_capable(record: dict) -> bool:
    """``is_chat_capable`` over a serialized cache record (dict), not a
    ``ListedModel``. The cache stores ``ListedModel.to_dict()`` rows, so reuse
    the same capability/substring rule without re-hydrating the dataclass.
    """
    if not isinstance(record, dict):
        return False
    caps = record.get("capabilities") or []
    model_id = str(record.get("model_id") or "")
    low = model_id.lower()
    if "chat" in caps:
        return not any(sub in low for sub in _NON_CHAT_SUBSTRINGS)
    if "non-chat" in caps or "embedding" in caps:
        return False
    return not any(sub in low for sub in _NON_CHAT_SUBSTRINGS)


def llm_providers_from_listings(
    cache: dict, providers: set[str] | None = None,
) -> set[str]:
    """Providers in the listings cache that expose at least one chat-capable
    model — i.e. providers that actually offer an LLM.

    This is the discovery-side complement to
    ``primary_bot.llm_providers_from_catalog`` (catalog clusters): a credentialed
    provider such as DeepSeek that lists chat models but is not (yet) in any rung
    cluster still counts as an LLM provider. Non-LLM auth-profile providers
    (Brave, Runway) list NO chat models, so they fall out here WITHOUT a
    provider-name literal — the set is data-derived from the listing capabilities.

    ``providers`` (when given) restricts the scan to that set — the credentialed
    filter arrives pre-computed from the route.
    """
    out: set[str] = set()
    for provider, models in (cache.get("providers") or {}).items():
        if providers is not None and provider not in providers:
            continue
        if any(_record_is_chat_capable(m) for m in (models or [])):
            out.add(provider)
    return out


def validate_against_listing(
    typed: str, cache: dict, providers: set[str] | None = None,
) -> dict:
    """Validate a free-text model id against the cached listings.

    Returns ``{"ok": True, "canonical": <id>}`` when ``typed`` matches a listed
    model (either bare or qualified spelling), normalizing to the canonical
    qualified id. Otherwise ``{"ok": False, "suggestion": <id|None>}`` where the
    suggestion is the nearest listed id by simple string distance (difflib —
    no LLM), or ``None`` when nothing is close.
    """
    index = listing_model_ids(cache, providers)
    needle = (typed or "").strip()
    if not needle:
        return {"ok": False, "suggestion": None}
    hit = index.get(needle.lower())
    if hit:
        return {"ok": True, "canonical": hit}
    # Nearest-match over the canonical ids (and their bare forms) by ratio.
    import difflib

    candidates = sorted(set(index.values()) | set(index.keys()))
    close = difflib.get_close_matches(needle.lower(), candidates, n=1, cutoff=0.6)
    suggestion = None
    if close:
        # Map a matched lowercased-key back to its canonical id.
        suggestion = index.get(close[0], close[0])
    return {"ok": False, "suggestion": suggestion}


def read_listings_cache(shared_dir: Path) -> dict | None:
    """Read the persisted listings cache, or ``None`` if absent/unreadable.

    A corrupt or missing cache returns ``None`` so the caller can render an
    honest "no listings yet — run a refresh" state rather than crashing.
    """
    path = listings_cache_path(shared_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ── Top-level run ─────────────────────────────────────────────────────────────

def run_discovery(
    *,
    network: dict,
    bot_users: list[str],
    bot_configs: dict[str, dict] | None,
    shared_dir: Path,
    keys: dict[str, str] | None = None,
    credentialed_providers: set[str] | None = None,
    enumerator=None,
    pricing_fetcher=None,
) -> DiscoveryResult:
    """Run the full discovery pipeline and return a DiscoveryResult.

    Parameters allow test injection:
      - ``keys`` — pre-resolved {provider: key} for the LISTING-capable
        providers; when None, resolved from bot auth-profiles via
        ``discover_provider_keys``.
      - ``credentialed_providers`` — the FULL set of providers any bot holds an
        api_key for, used for the credentialed-but-no-adapter gap check; when
        None, resolved from bot auth-profiles via
        ``discover_credentialed_providers``. A provider in this set that is an
        LLM provider (in ``_LLM_PROVIDERS``) but NOT in ``_LISTING_PROVIDERS``
        lands in ``uncovered_providers``. Non-LLM credentialed providers
        (brave/runway/…) are filtered out — they never need a model-listing
        adapter, so flagging them would be a false advisory.
      - ``enumerator`` — callable (provider, key) -> ProviderEnumeration;
        when None, uses ``enumerate_provider`` (real HTTP). Tests inject a
        mock so CI never makes live API calls.
      - ``pricing_fetcher`` — callable (url) -> dict for the pricing-catalog
        mirror (Addendum 8 §B); when None, uses the real HTTP getter. Tests
        inject a stub so CI never fetches pricing catalogs.

    A provider that is credentialed but whose listing call fails is recorded
    in ``degraded_providers`` with a reason — NEVER silently dropped. That is
    the cardinal invariant of this rework. A provider that is credentialed but
    has NO listing adapter at all lands in ``uncovered_providers`` (the gap
    advisory) — also never silently dropped.
    """
    enumerate_fn = enumerator or enumerate_provider
    if keys is None:
        keys = discover_provider_keys(bot_users)

    # The credentialed-but-no-adapter gap: every LLM provider any bot holds an
    # api_key for, minus the providers we have a listing adapter for. We only
    # read auth-profiles for this when the caller didn't inject the set AND we
    # actually have bot_users to read (tests pass keys + [] bot_users and get an
    # empty gap, as expected). The intersection with ``_LLM_PROVIDERS`` is what
    # keeps non-LLM credentialed providers (brave/runway/elevenlabs/…) OUT of
    # the advisory — they have no models to enumerate, so "add a listing
    # adapter" would be nonsensical for them. A genuinely-uncovered LLM provider
    # (deepseek/mistral/groq, even before it reaches the catalog) still lands
    # here. Sorted for stable Signal output.
    if credentialed_providers is None:
        credentialed_providers = (
            discover_credentialed_providers(bot_users) if bot_users else set()
        )
    uncovered_providers = sorted(
        (set(credentialed_providers) & _LLM_PROVIDERS) - set(_LISTING_PROVIDERS)
    )

    enumerations: list[ProviderEnumeration] = []
    enumerated: list[str] = []
    degraded: list[dict[str, str]] = []

    for provider in sorted(keys.keys()):
        key = keys[provider]
        enum = enumerate_fn(provider, key)
        enumerations.append(enum)
        if enum.ok:
            enumerated.append(provider)
        else:
            degraded.append({"provider": provider, "reason": enum.reason})

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Persist the enumerated listings (Phase 9 picker candidate source). This
    # rides the existing run — no new provider calls — so the daily-sweep path
    # and the operator "Check now"/refresh path both refresh the cache. A write
    # failure must never break discovery itself, but it must LOG (a silently
    # stale picker is the silent-monitor failure mode).
    try:
        write_listings_cache(
            Path(shared_dir), enumerations, refreshed_at=checked_at,
        )
    except OSError as exc:
        _log.warning(
            "model_discovery: failed to write listings cache at %s: %s",
            listings_cache_path(Path(shared_dir)), exc,
        )

    # Mirror the model-pricing catalog alongside the listings (Addendum 8 §B).
    # The listing carries identity but no price; this fetch adds the per-token
    # pricing the band layer (§C) joins to compute cost bands. It rides the same
    # sweep, degrades gracefully (a failed source keeps the stale cache), and
    # never breaks discovery — a bad fetch logs and returns None. Network access
    # lives ONLY here, behind ``pricing_fetcher`` (None → real HTTP) so tests
    # inject a stub and CI makes zero pricing calls.
    try:
        model_pricing.refresh_pricing_cache(
            Path(shared_dir),
            refreshed_at=checked_at,
            fetcher=pricing_fetcher,
        )
    except Exception as exc:  # noqa: BLE001 — pricing must never break discovery.
        _log.warning(
            "model_discovery: pricing-cache refresh failed (non-fatal): %s", exc,
        )

    # Read the (just-refreshed, or stale-but-served) pricing cache back so the
    # diff can derive each discovery's cost band from real prices and cite it
    # (Addendum 8 §C). A missing/corrupt cache returns None — discovery still
    # runs; bands then fall back to the family map / naming heuristic, never a
    # hard failure (the band layer degrades, it does not block discovery).
    pricing_cache: dict | None = None
    try:
        pricing_cache = model_pricing.read_pricing_cache(Path(shared_dir))
    except Exception as exc:  # noqa: BLE001 — pricing read must never break discovery.
        _log.warning(
            "model_discovery: pricing-cache read failed (non-fatal): %s", exc,
        )

    known, known_set_degraded, pod_sourced_known = known_model_set(
        network, bot_configs,
    )
    ignore = load_ignore_list(shared_dir)

    # Failure posture for the known-set source (the live-canary bug class):
    # a POD-SOURCED known set that is empty *because* a READ ERROR occurred must
    # NEVER drive emissions — every non-default listed model would look "new",
    # flooding the Improvements page with already-adopted models. So when the
    # pod's own adopted set is empty (the code defaults don't count — they ship
    # in code, not from the degraded source) AND a source errored, we suppress
    # discoveries by recording a degraded provider-shaped entry and skipping
    # diff emission.
    #
    # The defaults are always present (spec §Addendum 2.4), so testing the
    # full ``known`` set for emptiness would silently DISABLE this guard — the
    # suppression keys off the pod-sourced set (the readable pod/bot config
    # sources), which ``known_model_set`` reports separately. A genuinely empty
    # pod-sourced set with NO read errors (a fresh, rung-less pod) is valid —
    # discovery proceeds so the operator gets their first rungs.
    suppress_for_empty_degraded = known_set_degraded and not pod_sourced_known
    if suppress_for_empty_degraded:
        degraded.append({
            "provider": "known-set",
            "reason": (
                "The pod's adopted-model set could not be read from any "
                "bot's evolve-tiers.json (degraded read) and is empty. "
                "Discovery is suppressed to avoid flagging already-adopted "
                "models as new. Fix the unreadable tiers file(s) and re-run."
            ),
        })
        discoveries: list[DiscoveryFinding] = []
        staleness: list[StalenessFinding] = []
        upgrades: list[VersionUpgrade] = []
        ignored = 0
        skipped = 0
    else:
        discoveries, staleness, ignored, skipped = diff_listing(
            enumerations, known, ignore, pricing_cache,
        )
        # Version freshness — the PRIMARY result (spec §Addendum 15): every
        # model the pod runs that has a newer same-class version listed, with
        # the rung it occupies so the card offers a one-click "update to latest".
        listing_by_provider = {
            e.provider: e.models for e in enumerations if e.ok
        }
        upgrades = compute_version_upgrades(
            listing_by_provider,
            pod_sourced_model_locations(network, bot_configs),
        )
        # A partial degraded read (some bots readable, some not) is still
        # surfaced so the run is never silently "all current" on a stale set.
        if known_set_degraded:
            degraded.append({
                "provider": "known-set",
                "reason": (
                    "At least one bot's evolve-tiers.json could not be read "
                    "(degraded/partial known set). Discoveries below were "
                    "computed against the readable bots only — a model "
                    "adopted solely by an unreadable bot could surface as a "
                    "false discovery. Fix the unreadable tiers file(s)."
                ),
            })

    return DiscoveryResult(
        checked_at=checked_at,
        enumerated_providers=enumerated,
        degraded_providers=degraded,
        discoveries=discoveries,
        staleness=staleness,
        upgrades=upgrades,
        known_model_count=len(known),
        ignored_count=ignored,
        skipped_count=skipped,
        uncovered_providers=uncovered_providers,
    )
