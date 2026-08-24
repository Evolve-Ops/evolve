"""model_auto_upgrade — the automatic model-version-upgrade ELIGIBILITY engine.

Spec: [internal/spec-model-auto-upgrade-2026-07-30.md](../../internal/spec-model-auto-upgrade-2026-07-30.md)
— **Phase 1: the eligibility engine, shipped dark.** Extends
``internal/spec-model-rungs-and-roles-2026-06-09.md`` §Addendum 15 (the DETECTION
half, ``model_discovery.compute_version_upgrades``, shipped in #3359).

Detection works and adoption does not happen: the reference pod's
``model_discovery`` generator emitted 57 proposals and applied 0, because every
version bump needs a human to open the AI Optimization page and accept. This
module is the first half of closing that gap — it decides, deterministically and
**without mutating anything**, which detected version upgrades would be safe to
apply unattended, and *why each of the others would not be*.

**Nothing here writes.** No config, no rung, no file. ``compute_auto_upgrades``
is a pure function of (live provider listings, pod/bot config, pricing cache),
which is precisely what makes the eligibility decision unit-testable against
synthetic listings (spec §Apply path: "steps 2-6 are pure; only step 7
mutates"). The apply path, the liveness probe, the weekly job and the card's
action buttons are Phases 2-4 and are deliberately absent.

The **reasons are the product**. A held-back upgrade whose reason reads wrong is
the failure mode — the whole point of this feature is that the pod stopped being
silently invisible about what it was and was not doing, so every held item
carries a machine-readable ``code`` AND one plain operator-facing sentence. A
run that finds nothing eligible still reports everything it considered.

Three hard rules, each of which has drawn blood before:

1. **Unknown price ⇒ NOT eligible** (§Cost guard). Absence of a price is not
   evidence of parity. The 2026-07-31 cost runaway was a cost-class question
   resolved the wrong way by automation nobody was watching.
2. **An empty or unavailable provider listing is NEVER "nothing newer"**
   (§Failure and safety posture). That provider is reported as UNCHECKED
   (:class:`SkippedProvider`), never flattened into "you're up to date".
3. **Version ranking is numeric, never lexicographic** — every comparison here
   delegates to ``model_discovery.model_generation_rank`` / ``_version_tuple``
   so ``claude-sonnet-10`` sorts above ``claude-sonnet-4-5``. No provider or
   model literal appears in logic anywhere in this module.

One thing this module must NOT be read as promising: an ``eligible`` decision
here is eligible **on the guards that exist today**. The §Liveness guard (a real
round-trip through the embedded-agent path) is Phase 2 and has not run, so every
eligible decision carries ``pending_guards=("liveness",)``. Phase 3 must not
apply a decision whose ``pending_guards`` is non-empty.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import model_discovery as _md

# ── Policy (spec §Config shape) ───────────────────────────────────────────────
# Pod-wide default lives in network.json::models.autoUpgrade; a Custom bot's
# override lives in that bot's evolve-tiers.json::autoUpgrade. Phase 1 only
# READS these keys — no write path, so no oc_model write-whitelist entry yet
# (that lands with the Phase 3 apply path / Phase 4 toggle).

AUTO_UPGRADE_KEY = "autoUpgrade"

#: Config-key → policy-field map. DATA, so a new knob is a one-row edit.
_POLICY_FIELDS: tuple[tuple[str, str], ...] = (
    ("enabled", "enabled"),
    ("applyDay", "apply_day"),
    ("requireCostNonRegression", "require_cost_non_regression"),
    ("requireGA", "require_ga"),
    ("minVisibleDays", "min_visible_days"),
)


@dataclass(frozen=True)
class AutoUpgradePolicy:
    """The resolved auto-upgrade policy for one config scope.

    Defaults ARE the code defaults (spec §Config shape): ``enabled: false`` must
    survive a config that omits the block entirely — the pod does not change its
    own model config until an operator turns this on.

    ``enabled_source`` records WHICH layer decided ``enabled``
    (``code-default`` | ``pod`` | ``bot``) so the card can say "off because
    nobody turned it on" distinctly from "off because this bot's own toggle is
    off" — the two read identically today and mean very different things.
    """

    enabled: bool = False
    apply_day: str = "tuesday"
    require_cost_non_regression: bool = True
    require_ga: bool = True
    min_visible_days: int = 7
    enabled_source: str = "code-default"

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "applyDay": self.apply_day,
            "requireCostNonRegression": self.require_cost_non_regression,
            "requireGA": self.require_ga,
            "minVisibleDays": self.min_visible_days,
            "enabledSource": self.enabled_source,
        }


DEFAULT_POLICY = AutoUpgradePolicy()


def _coerce_policy_block(block: Any) -> dict[str, Any]:
    """Normalize one raw ``autoUpgrade`` block into policy-field kwargs.

    Junk (non-dict block, wrong-typed value) is DROPPED rather than raising: a
    malformed knob must never crash the freshness card, and dropping it leaves
    the safer default in place. ``enabled`` accepts only a real bool — a string
    ``"true"`` is not a decision to change the pod's model config.
    """
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    for cfg_key, field_name in _POLICY_FIELDS:
        if cfg_key not in block:
            continue
        val = block[cfg_key]
        if field_name in ("enabled", "require_cost_non_regression", "require_ga"):
            if isinstance(val, bool):
                out[field_name] = val
        elif field_name == "min_visible_days":
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                out[field_name] = val
        elif field_name == "apply_day":
            if isinstance(val, str) and val.strip():
                out[field_name] = val.strip().lower()
    return out


def pod_policy(network: Mapping[str, Any] | None) -> AutoUpgradePolicy:
    """The pod-wide policy from ``network.json::models.autoUpgrade``."""
    models = (network or {}).get("models")
    block = models.get(AUTO_UPGRADE_KEY) if isinstance(models, dict) else None
    kwargs = _coerce_policy_block(block)
    source = "pod" if "enabled" in kwargs else "code-default"
    return AutoUpgradePolicy(**kwargs, enabled_source=source)


def bot_policy(
    pod: AutoUpgradePolicy, bot_doc: Mapping[str, Any] | None, *, custom: bool,
) -> AutoUpgradePolicy:
    """The policy governing one bot, per spec §"Scope: the toggle rides the
    config scope, not the bot".

    - A **Use-pod-defaults** bot has no toggle of its own; it follows the pod
      wholesale, exactly as it already follows the pod's rungs.
    - A **Custom** bot owns its own toggle. Its ``enabled`` therefore does NOT
      inherit from the pod ("the pod toggle does not reach a Custom bot"), so a
      Custom bot that has never set the key resolves to the CODE default
      (``false``) — the fail-safe reading, and the one the §"migration-era
      hazard" narrative depends on. The subordinate knobs (apply day, GA/cost
      requirements, visible-days floor) still layer code ← pod ← bot, so a bot
      that writes only ``{"enabled": true}`` inherits the pod's cadence rather
      than silently reverting it.
    """
    own = _coerce_policy_block((bot_doc or {}).get(AUTO_UPGRADE_KEY))
    if not custom:
        return pod
    knobs = {k: v for k, v in dataclasses.asdict(pod).items()
             if k not in ("enabled", "enabled_source")}
    knobs.update({k: v for k, v in own.items() if k != "enabled"})
    if "enabled" in own:
        return AutoUpgradePolicy(**knobs, enabled=own["enabled"], enabled_source="bot")
    return AutoUpgradePolicy(**knobs, enabled=False, enabled_source="code-default")


# ── Hold reasons (spec §"What auto-upgrade means" 1-7 + §Failure posture) ─────
# Every code here is stable and machine-readable; the message beside it is the
# operator-facing sentence. Both are part of the contract — a held-back upgrade
# that reads wrong is this phase's failure mode, so the strings are treated as a
# deliverable, not as logging.

HOLD_PROVIDER_MISMATCH = "provider_mismatch"
HOLD_FAMILY_MISMATCH = "family_mismatch"
HOLD_NOT_NEWER = "not_strictly_newer"
HOLD_TARGET_NOT_GA = "target_not_ga"
HOLD_TARGET_SNAPSHOT_ALIAS = "target_is_snapshot_alias"
HOLD_TARGET_PRICE_UNKNOWN = "target_price_unknown"
HOLD_CURRENT_PRICE_UNKNOWN = "current_price_unknown"
HOLD_COST_REGRESSION = "cost_regression"
HOLD_COST_BAND_UNKNOWN = "cost_band_unknown"
HOLD_COST_BAND_REGRESSION = "cost_band_regression"
HOLD_RUNG_UNKNOWN = "predecessor_rung_unknown"
HOLD_RUNG_AMBIGUOUS = "predecessor_in_multiple_rungs"
HOLD_SCOPE_UNKNOWN = "config_scope_unknown"
HOLD_NOT_VISIBLE_LONG_ENOUGH = "not_visible_long_enough"

#: Held reasons that will not change on their own — the "nag once, then go
#: quiet" class (spec §Permanently-held upgrades). A repriced-upward version is
#: excluded indefinitely, so its row is surfaced once and then suppressed for
#: that exact version; a temporarily-unpriced or too-young one is not.
#: Consumed by the card (Phase 4 owns the acknowledgement state); exported here
#: because whether a reason is durable is an engine fact, not a UI opinion.
DURABLE_HOLD_CODES: frozenset[str] = frozenset({
    HOLD_COST_REGRESSION,
    HOLD_COST_BAND_REGRESSION,
    HOLD_TARGET_NOT_GA,
    HOLD_TARGET_SNAPSHOT_ALIAS,
})


@dataclass(frozen=True)
class HoldReason:
    """One reason an upgrade is not eligible: a stable ``code``, one plain
    sentence, and the evidence behind it."""

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def durable(self) -> bool:
        return self.code in DURABLE_HOLD_CODES

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": dict(self.detail),
            "durable": self.durable,
        }


# ── GA guard (spec condition 4) ───────────────────────────────────────────────
# Pre-release markers are GENERIC release-stage words, never provider or model
# names — a new provider needs no edit here. Matched as whole dash/dot/@
# separated tokens so a model whose id merely CONTAINS the letters (…"express"…)
# is not misread as experimental.

#: token → the phrase an operator reads, with its article already attached.
#: DATA, so a new marker is a one-row edit and no message ever reads "a exp".
_PRERELEASE_TOKENS: dict[str, str] = {
    "preview": "a preview",
    "exp": "an experimental",
    "experimental": "an experimental",
    "beta": "a beta",
    "alpha": "an alpha",
    "rc": "a release-candidate",
    "preproduction": "a pre-production",
    "early": "an early-access",
    "test": "a test",
}
_TOKEN_SPLIT_RE = re.compile(r"[-_.@:/]+")


def prerelease_markers(model_id: str) -> list[str]:
    """The pre-release tokens in a model id (empty ⇒ reads as generally
    available). Token-exact, so ``…-preview`` matches and ``…-express`` does
    not."""
    bare = _md._bare_id(model_id).lower()
    return [t for t in _TOKEN_SPLIT_RE.split(bare) if t in _PRERELEASE_TOKENS]


# ── Cost guard (spec §Cost guard — the load-bearing one) ──────────────────────
# "The target's cost band must be ≤ the predecessor's on every axis it reports
# (input, output, cache read, cache write). Unknown or unpriced target ⇒ NOT
# eligible — absence of a price is not evidence of parity."
#
# The pricing cache's normalized record (model_pricing.PricedModel) carries the
# input and output axes today; the cache-read / cache-write axes are named here
# so that if ``model_pricing`` starts normalizing them the guard picks them up
# with no code change. REQUIRED axes must be priced on BOTH models or the
# upgrade is held as unpriced; OPTIONAL axes are compared when both sides carry
# them, and hold when the TARGET reports one the predecessor does not (we cannot
# prove parity on an axis we can only see one side of).

_REQUIRED_COST_AXES: tuple[tuple[str, str], ...] = (
    ("input_cost_per_token", "input"),
    ("output_cost_per_token", "output"),
)
_OPTIONAL_COST_AXES: tuple[tuple[str, str], ...] = (
    ("cache_read_input_token_cost", "cache read"),
    ("cache_creation_input_token_cost", "cache write"),
)


def _price(rec: Mapping[str, Any] | None, key: str) -> float | None:
    if not isinstance(rec, Mapping):
        return None
    val = rec.get(key)
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num >= 0 else None


def _per_million(value: float | None) -> str:
    """Format a $/token price as the $/1M figure operators actually read."""
    if value is None:
        return "unknown"
    return f"${value * 1_000_000:,.2f}/1M"


def _band_rank(band: str | None) -> int | None:
    try:
        from model_cost_bands import COST_BANDS
    except Exception:  # pragma: no cover — analyzer sibling, always importable
        return None
    if band in COST_BANDS:
        return COST_BANDS.index(band)
    return None


def _resolve_band(provider: str, model_id: str, pricing_cache: dict | None):
    """``model_cost_bands.resolve_band`` with a fail-soft import — returns
    ``(band, source)``; ``(None, "unknown")`` when the module or the price is
    unavailable. Never invents a band."""
    try:
        import model_cost_bands
    except Exception:  # pragma: no cover
        return None, "unknown"
    try:
        res = model_cost_bands.resolve_band(
            provider, _md._bare_id(model_id), cache=pricing_cache,
        )
    except Exception:
        return None, "unknown"
    return res.band, res.source


def cost_guard(
    *,
    provider: str,
    current_model: str,
    latest_model: str,
    pricing_cache: dict[str, Any] | None,
) -> tuple[list[HoldReason], dict[str, Any]]:
    """Compare the target's price against its predecessor's, per §Cost guard.

    Returns ``(holds, evidence)``. ``holds`` is empty ONLY when both models are
    priced on every required axis and the target is ≤ the predecessor on every
    axis compared. An unpriced target is held — never waved through.
    """
    import model_pricing

    cur_bare = _md._bare_id(current_model)
    new_bare = _md._bare_id(latest_model)
    cur_rec = model_pricing.lookup_price(pricing_cache, provider, cur_bare)
    new_rec = model_pricing.lookup_price(pricing_cache, provider, new_bare)

    evidence: dict[str, Any] = {
        "current_model": cur_bare,
        "latest_model": new_bare,
        "axes": {},
        "pricing_source": (new_rec or {}).get("source") or "",
    }
    holds: list[HoldReason] = []

    missing_target = [
        label for key, label in _REQUIRED_COST_AXES
        if _price(new_rec, key) is None
    ]
    missing_current = [
        label for key, label in _REQUIRED_COST_AXES
        if _price(cur_rec, key) is None
    ]
    if missing_target:
        holds.append(HoldReason(
            code=HOLD_TARGET_PRICE_UNKNOWN,
            message=(
                f"No published {', '.join(missing_target)} price for {new_bare} "
                f"yet, so there is no way to tell whether it costs more than "
                f"{cur_bare}. An unknown price is not evidence of the same "
                f"price, so this one waits for you."
            ),
            detail={"model": new_bare, "unpriced_axes": missing_target},
        ))
    if missing_current:
        holds.append(HoldReason(
            code=HOLD_CURRENT_PRICE_UNKNOWN,
            message=(
                f"No published {', '.join(missing_current)} price for the model "
                f"you run today ({cur_bare}), so there is nothing to compare "
                f"{new_bare} against. Automatic updates need a priced "
                f"before-and-after."
            ),
            detail={"model": cur_bare, "unpriced_axes": missing_current},
        ))

    regressions: list[dict[str, Any]] = []
    unknown_axes: list[str] = []
    for key, label in _REQUIRED_COST_AXES + _OPTIONAL_COST_AXES:
        cur_val = _price(cur_rec, key)
        new_val = _price(new_rec, key)
        if new_val is None:
            continue  # required-axis absence already held above
        if cur_val is None:
            if (key, label) in _OPTIONAL_COST_AXES:
                unknown_axes.append(label)
            continue
        evidence["axes"][label] = {"current": cur_val, "latest": new_val}
        if new_val > cur_val:
            regressions.append({"axis": label, "current": cur_val, "latest": new_val})

    if regressions:
        bits = ", ".join(
            f"{r['axis']} {_per_million(r['current'])} → {_per_million(r['latest'])}"
            for r in regressions
        )
        holds.append(HoldReason(
            code=HOLD_COST_REGRESSION,
            message=(
                f"{new_bare} costs more than {cur_bare} ({bits}). Moving to a "
                f"pricier model is a cost decision, not a freshness one — "
                f"automatic updates never raise your bill. Update it yourself if "
                f"you want it."
            ),
            detail={"regressions": regressions},
        ))
    if unknown_axes:
        holds.append(HoldReason(
            code=HOLD_CURRENT_PRICE_UNKNOWN,
            message=(
                f"{new_bare} is priced for {', '.join(unknown_axes)} but "
                f"{cur_bare} is not, so those axes cannot be compared. Automatic "
                f"updates need a priced before-and-after on every axis."
            ),
            detail={"model": cur_bare, "unpriced_axes": unknown_axes},
        ))

    # Band check — the coarse backstop behind the per-axis compare. Within one
    # family the band is usually identical (that is the point of a version
    # bump); a band JUMP means the family got repriced into another class.
    cur_band, cur_band_src = _resolve_band(provider, cur_bare, pricing_cache)
    new_band, new_band_src = _resolve_band(provider, new_bare, pricing_cache)
    evidence["bands"] = {
        "current": {"band": cur_band, "source": cur_band_src},
        "latest": {"band": new_band, "source": new_band_src},
    }
    cur_rank, new_rank = _band_rank(cur_band), _band_rank(new_band)
    if new_rank is None:
        # Distinguish "no price at all" from "priced, but the cost-CLASS scale
        # could not be built" (the anchor models the scale is relative to are
        # themselves unpriced). Telling an operator their model is unpriced
        # while the card shows them its price is exactly the kind of reason this
        # phase exists to get right.
        priced = not missing_target
        holds.append(HoldReason(
            code=HOLD_COST_BAND_UNKNOWN,
            message=(
                (
                    f"{new_bare} is priced, and it does not cost more than "
                    f"{cur_bare}, but its cost class could not be worked out — "
                    f"the reference models the classes are measured against have "
                    f"no price yet. Automatic updates wait for a full picture."
                ) if priced else (
                    f"{new_bare} could not be placed in a cost band from any "
                    f"pricing source. Automatic updates never move to a model "
                    f"whose cost class is unknown."
                )
            ),
            detail={"model": new_bare, "band_source": new_band_src,
                    "priced": priced},
        ))
    elif cur_rank is not None and new_rank > cur_rank:
        holds.append(HoldReason(
            code=HOLD_COST_BAND_REGRESSION,
            message=(
                f"{new_bare} sits in the {new_band} cost band while {cur_bare} "
                f"sits in {cur_band} — that is a step up in cost class, not a "
                f"version bump. It waits for you."
            ),
            detail={"current_band": cur_band, "latest_band": new_band},
        ))
    return holds, evidence


# ── Config scopes (spec §"Scope: the toggle rides the config scope") ──────────


@dataclass
class ScopeState:
    """One tier-config scope and whether auto-upgrade governs it.

    A pod scope plus one entry per pod-member bot. ``governed_by`` is ``"pod"``
    for a Use-pod-defaults bot (it has no toggle of its own) and ``"self"`` for a
    Custom bot.

    ``custom_origin`` is the §"migration-era hazard" field and the reason this
    class exists. The 2026-06-09 ``migrate_model_roles`` one-shot rewrote every
    bot's legacy ``tiers`` block into a per-bot ``rungs`` array, so bots that
    predate the Use-defaults/Custom toggle read as Custom **though no operator
    ever chose that** — and a pod-scope toggle would silently not govern them.
    ``operator-chosen`` vs ``migration-residue`` (derived from
    ``primary_bot.bot_redundant_vs_default``: the bot's override resolves
    identically with and without itself) makes the two read differently instead
    of papering over the difference.
    """

    scope_id: str                 # "pod", or the bot id
    kind: str                     # "pod" | "bot"
    policy: AutoUpgradePolicy
    custom: bool = False
    custom_origin: str = ""       # "" | "operator-chosen" | "migration-residue"
    governed_by: str = "self"     # "pod" | "self"
    # False when the bot's tiers file EXISTS but could not be read or parsed.
    # A degraded read must never be reported as "follows the pod" — that is the
    # opposite claim, made silently, about a bot whose config we cannot see
    # (ACL drift on .openclaw/ is a recurring incident class here). Same posture
    # as :class:`SkippedProvider`: say we could not look.
    readable: bool = True
    # True when a Use-pod-defaults bot carries its own ``autoUpgrade`` key. The
    # key does nothing (the toggle rides the config scope, and this bot has no
    # scope of its own), so the operator has a setting that is being ignored —
    # reported rather than dropped.
    ignored_own_toggle: bool = False

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    @property
    def note(self) -> str:
        """One operator-facing sentence about this scope's governance."""
        if self.kind == "pod":
            return (
                "Pod defaults have automatic updates on."
                if self.enabled else
                "Pod defaults have automatic updates off."
            )
        if not self.readable:
            return (
                f"Could not read {self.scope_id}'s model config, so there is no "
                f"way to say whether the pod setting governs it. Check the file "
                f"permissions on that bot's .openclaw directory."
            )
        if not self.custom:
            if self.ignored_own_toggle:
                return (
                    f"{self.scope_id} follows the pod defaults, so the pod "
                    f"toggle governs it — its own automatic-update setting is "
                    f"ignored while it follows the pod. Customize the bot's "
                    f"models if you want it to decide for itself."
                )
            return (
                f"{self.scope_id} follows the pod defaults, so the pod toggle "
                f"governs it."
            )
        if self.custom_origin == "migration-residue":
            return (
                f"{self.scope_id} has its own model config, but it resolves "
                f"identically to the pod defaults — most likely left over from "
                f"the tier migration rather than a choice. The pod toggle does "
                f"NOT reach it; reset it to pod defaults to have it follow."
            )
        return (
            f"{self.scope_id} has its own model config, so it keeps its own "
            f"automatic-update setting — the pod toggle does not reach it."
        )

    def to_dict(self) -> dict:
        return {
            "scope": self.scope_id,
            "kind": self.kind,
            "enabled": self.enabled,
            "custom": self.custom,
            "customOrigin": self.custom_origin,
            "governedBy": self.governed_by,
            "readable": self.readable,
            "ignoredOwnToggle": self.ignored_own_toggle,
            "note": self.note,
            "policy": self.policy.to_dict(),
        }


def _bot_ids(network: Mapping[str, Any] | None) -> list[str]:
    bots = (network or {}).get("bots")
    return sorted(bots.keys()) if isinstance(bots, dict) else []


def read_bot_docs(network: Mapping[str, Any]) -> tuple[dict[str, dict], set[str]]:
    """Every pod-member bot's RAW ``evolve-tiers.json`` document, plus the ids
    whose file exists but could NOT be read or parsed.

    Raw, never merged: "does this bot follow the pod?" is decided from the raw
    doc's ``rungs`` presence (``primary_bot.bot_has_custom_tiers``), and the
    ``autoUpgrade`` key is likewise the scope's OWN value, not an inherited one.

    The degraded set is load-bearing. ``read_bot_tiers_doc`` swallows the ok
    flag, so an unreadable file is indistinguishable from a bot that has no
    tiers of its own — which would make the card claim "follows the pod
    defaults, so the pod toggle governs it" about a bot whose config nobody can
    see. Read one layer lower so an unreadable bot can say so.
    """
    try:
        from primary_bot import (  # type: ignore
            _bot_evolve_tiers_path, _read_bot_owned_json,
        )
    except Exception:  # pragma: no cover — analyzer sibling
        return {}, set()
    out: dict[str, dict] = {}
    degraded: set[str] = set()
    for bot_id in _bot_ids(network):
        try:
            data, ok = _read_bot_owned_json(_bot_evolve_tiers_path(dict(network), bot_id))
        except Exception:
            data, ok = None, False
        if not ok:
            degraded.add(bot_id)
        out[bot_id] = data if isinstance(data, dict) else {}
    return out, degraded


def _doc_is_custom(doc: Mapping[str, Any] | None) -> bool:
    """``bot_has_custom_tiers`` as a pure function of the already-read doc — a
    bot is Custom iff its raw tiers doc carries a non-empty ``rungs`` array."""
    rungs = (doc or {}).get("rungs")
    return isinstance(rungs, list) and bool(rungs)


def _doc_is_redundant(pod_models: Mapping[str, Any], doc: Mapping[str, Any]) -> bool:
    """``primary_bot.bot_redundant_vs_default`` as a pure function of the
    already-read doc: True when the bot's override resolves identically with and
    without itself, i.e. clearing it would change nothing. Those are the
    migration-residue bots (spec §"The migration-era hazard").

    Fail-safe: any resolution error returns False — never label a bot's
    deliberate config "leftover" on the strength of a read we could not do.
    """
    try:
        from primary_bot import (  # type: ignore
            ROLE_ORDER, _resolve_role_to_rung, _rung_models, merge_model_catalog,
        )

        cat_with = merge_model_catalog(dict(pod_models), dict(doc))
        cat_without = merge_model_catalog(dict(pod_models), {})
        for role in ROLE_ORDER:
            rung_w = _resolve_role_to_rung(cat_with, role)
            rung_wo = _resolve_role_to_rung(cat_without, role)
            models_w = _rung_models(cat_with, rung_w) if rung_w else []
            models_wo = _rung_models(cat_without, rung_wo) if rung_wo else []
            if (rung_w, models_w) != (rung_wo, models_wo):
                return False
        return True
    except Exception:
        return False


def resolve_scopes(
    network: Mapping[str, Any],
    bot_tier_docs: Mapping[str, Mapping[str, Any]] | None = None,
    unreadable_bots: Iterable[str] = (),
) -> list[ScopeState]:
    """The pod scope plus one scope per pod-member bot, each with its resolved
    policy and its governance story. ``bot_tier_docs`` may be injected (tests,
    or a caller that has already read them); otherwise the raw per-bot docs are
    read through ``primary_bot``. ``unreadable_bots`` names bots whose tiers file
    could not be read — they are reported as unknown rather than as followers."""
    pod_models = (network or {}).get("models")
    pod_models = pod_models if isinstance(pod_models, dict) else {}
    pod = pod_policy(network)
    scopes: list[ScopeState] = [
        ScopeState(scope_id="pod", kind="pod", policy=pod, governed_by="self"),
    ]
    if bot_tier_docs is not None:
        docs: Mapping[str, Mapping[str, Any]] = bot_tier_docs
        degraded = set(unreadable_bots)
    else:
        docs, degraded = read_bot_docs(network)
        degraded |= set(unreadable_bots)
    for bot_id in _bot_ids(network):
        doc = docs.get(bot_id) or {}
        readable = bot_id not in degraded
        custom = _doc_is_custom(doc)
        origin = ""
        if custom:
            origin = (
                "migration-residue" if _doc_is_redundant(pod_models, doc)
                else "operator-chosen"
            )
        scopes.append(ScopeState(
            scope_id=bot_id,
            kind="bot",
            policy=bot_policy(pod, doc, custom=custom),
            custom=custom,
            custom_origin=origin,
            governed_by="self" if custom else "pod",
            readable=readable,
            ignored_own_toggle=(
                not custom and AUTO_UPGRADE_KEY in (doc or {})
            ),
        ))
    return scopes


def _doc_places(
    doc: Mapping[str, Any],
    *,
    bare_model: str,
    rung_slug: str,
) -> bool:
    """True when this scope's OWN document places ``bare_model`` in
    ``rung_slug`` — i.e. this scope is one of the places an apply would edit."""
    for rung in (doc.get("rungs") or []):
        if not isinstance(rung, dict) or rung.get("id") != rung_slug:
            continue
        for m in (rung.get("models") or []):
            if isinstance(m, str) and _md._bare_id(m).lower() == bare_model:
                return True
    return False


# ── Rung occupancy (spec condition 7) ─────────────────────────────────────────


def apply_editable_documents(
    network: Mapping[str, Any],
    bot_configs: Mapping[str, dict] | None = None,
    bot_tier_docs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[tuple[str, dict]]:
    """Every document an apply could actually EDIT, as ``(label, doc)`` pairs:
    the pod's own ``network.json::models``, each Custom bot's raw
    ``evolve-tiers.json``, and any rungs carried on ``bot_configs``.

    Deliberately NOT the merged catalog. The merged view folds
    ``DEFAULT_MODEL_CATALOG`` in, and the shipped ladder puts one model in
    several rungs on purpose (the judge rung is by design an alias of
    Standard's cluster, and each rung carries the same cross-provider
    alternates). Judging condition 7 against the merge therefore reported the
    single most common real upgrade — Sonnet N → N+1 — as "sits in more than one
    tier", while the row's own Update button had exactly one target: a card
    saying there is no place to put it next to a button that puts it there.
    Occupancy has to be measured where the write would land, which is the same
    place :func:`_scopes_carrying` looks.
    """
    docs: list[tuple[str, dict]] = []
    pod_models = (network or {}).get("models")
    if isinstance(pod_models, dict):
        docs.append(("pod", dict(pod_models)))
    for bot_id, doc in (bot_tier_docs or {}).items():
        if isinstance(doc, Mapping) and doc.get("rungs"):
            docs.append((bot_id, dict(doc)))
    for bot_id, cfg in (bot_configs or {}).items():
        models = cfg.get("models") if isinstance(cfg, Mapping) else None
        if isinstance(models, Mapping) and models.get("rungs"):
            docs.append((f"{bot_id}:config", dict(models)))
    return docs


def rung_occupancy(
    network: Mapping[str, Any],
    bot_configs: Mapping[str, dict] | None = None,
    bot_tier_docs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, set[str]]]:
    """One ``{bare_model_id: {rung_slug, ...}}`` map PER apply-editable document.

    Per-document, not unioned, because ambiguity is a property of ONE document:
    an apply edits a single config, so a predecessor sitting in two rungs *of the
    same document* has no unambiguous replacement site (condition 7), whereas the
    pod and a Custom bot each placing it in their own single rung is two
    unambiguous applies, not an ambiguity. The union across documents is what
    tells us it is located at all.
    """
    out: list[dict[str, set[str]]] = []
    for _label, doc in apply_editable_documents(network, bot_configs, bot_tier_docs):
        seen: dict[str, set[str]] = {}
        for rung in (doc.get("rungs") or []):
            if not isinstance(rung, dict):
                continue
            slug = rung.get("id")
            if not isinstance(slug, str) or not slug:
                continue
            for m in (rung.get("models") or []):
                if isinstance(m, str) and m:
                    seen.setdefault(_md._bare_id(m).lower(), set()).add(slug)
        out.append(seen)
    return out


# ── Decisions + report ────────────────────────────────────────────────────────


@dataclass
class UpgradeDecision:
    """One detected version upgrade, judged.

    ``eligible`` means it passed every guard THAT EXISTS TODAY. ``pending_guards``
    names the ones that have not run — Phase 1 always leaves ``liveness`` pending
    (spec §Liveness guard is Phase 2), and Phase 3 must refuse to apply a
    decision with a non-empty ``pending_guards``.

    ``would_auto_apply`` is the stricter, honest answer to the card's question:
    eligible AND at least one scope that carries this model has the policy turned
    on. The two are reported separately on purpose — while the feature ships dark
    every decision has ``would_auto_apply=False``, and collapsing them would hide
    whether the ENGINE is right, which is the entire point of the read-only week.
    """

    provider: str
    family: str
    current_model: str
    latest_model: str
    rung_slug: str
    roles: list[str] = field(default_factory=list)
    eligible: bool = False
    holds: list[HoldReason] = field(default_factory=list)
    pending_guards: tuple[str, ...] = ()
    scopes: list[str] = field(default_factory=list)          # scopes carrying it
    enabled_scopes: list[str] = field(default_factory=list)  # …with the policy on
    cost_evidence: dict[str, Any] = field(default_factory=dict)
    visible_days: float | None = None

    @property
    def would_auto_apply(self) -> bool:
        return bool(self.eligible and self.enabled_scopes)

    @property
    def primary_hold(self) -> HoldReason | None:
        return self.holds[0] if self.holds else None

    @property
    def durable(self) -> bool:
        """True when ANY reason this is held will not resolve on its own — the
        "nag once, then go quiet" class (spec §Permanently-held upgrades).
        Keyed off every hold, not just the first: an item held for both a
        transient reason and a repricing is still permanently held once the
        transient one clears."""
        return any(h.durable for h in self.holds)

    @property
    def summary(self) -> str:
        """The one-line verdict the card's read-only column renders."""
        if self.eligible and not self.enabled_scopes:
            return (
                "Would update automatically once you turn automatic updates on."
            )
        if self.eligible:
            return "Would update automatically."
        hold = self.primary_hold
        return hold.message if hold else "Held back."

    def key(self) -> str:
        """Stable identity for this upgrade — (provider, rung, target version).
        Phase 3's first-seen ledger and Phase 4's per-version acknowledgement
        both key on this."""
        return upgrade_key(
            self.provider, self.rung_slug, self.latest_model,
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key(),
            "provider": self.provider,
            "family": self.family,
            "current_model": self.current_model,
            "latest_model": self.latest_model,
            "rung_slug": self.rung_slug,
            "roles": list(self.roles),
            "eligible": self.eligible,
            "wouldAutoApply": self.would_auto_apply,
            "summary": self.summary,
            "holds": [h.to_dict() for h in self.holds],
            "pendingGuards": list(self.pending_guards),
            "scopes": list(self.scopes),
            "enabledScopes": list(self.enabled_scopes),
            "costEvidence": self.cost_evidence,
            "visibleDays": self.visible_days,
        }


@dataclass
class SkippedProvider:
    """A provider whose listing could not be read this run.

    THE load-bearing honesty field (spec §Failure and safety posture): a missing
    listing means "we do not know", never "nothing newer". Every pod model on
    that provider went unchecked, and the count says how many.
    """

    provider: str
    reason: str
    model_count: int = 0

    @property
    def message(self) -> str:
        checked = (
            f" {self.model_count} model{'' if self.model_count == 1 else 's'} you "
            f"run there {'was' if self.model_count == 1 else 'were'} not checked."
            if self.model_count else ""
        )
        return (
            f"Could not read {self.provider}'s model list this run"
            f"{f' ({self.reason})' if self.reason else ''}."
            f"{checked} This is not the same as being up to date."
        )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "reason": self.reason,
            "modelCount": self.model_count,
            "message": self.message,
        }


@dataclass
class AutoUpgradeReport:
    """The full result of one eligibility run — every upgrade considered, each
    either eligible or held with its reason, plus the providers that could not be
    checked and the governance picture.

    A run that finds nothing eligible still says what it considered and why each
    item was held: the 57-emitted/0-applied invisibility this feature exists to
    fix must not reappear one layer down.
    """

    computed_at: str
    eligible: list[UpgradeDecision] = field(default_factory=list)
    held: list[UpgradeDecision] = field(default_factory=list)
    skipped_providers: list[SkippedProvider] = field(default_factory=list)
    scopes: list[ScopeState] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return len(self.eligible) + len(self.held)

    @property
    def decisions(self) -> list[UpgradeDecision]:
        return self.eligible + self.held

    @property
    def degraded(self) -> bool:
        return bool(self.skipped_providers)

    @property
    def pod_scope(self) -> ScopeState | None:
        return next((s for s in self.scopes if s.kind == "pod"), None)

    def by_key(self) -> dict[str, UpgradeDecision]:
        return {d.key(): d for d in self.decisions}

    def governance(self) -> dict[str, Any]:
        """The §"migration-era hazard" summary: what the pod toggle actually
        governs, and which bots it does not reach — named, and split by whether
        being Custom looks like a choice or like migration residue."""
        pod = self.pod_scope
        bots = [s for s in self.scopes if s.kind == "bot"]
        readable = [s for s in bots if s.readable]
        unreadable = [s.scope_id for s in bots if not s.readable]
        follows = [s.scope_id for s in readable if not s.custom]
        residue = [s.scope_id for s in readable
                   if s.custom_origin == "migration-residue"]
        chosen = [s.scope_id for s in readable
                  if s.custom_origin == "operator-chosen"]
        ignored = [s.scope_id for s in readable if s.ignored_own_toggle]
        return {
            "podEnabled": bool(pod and pod.enabled),
            "podPolicy": (pod.policy.to_dict() if pod else DEFAULT_POLICY.to_dict()),
            "botsFollowingPod": follows,
            "botsCustomOperatorChosen": chosen,
            "botsCustomMigrationResidue": residue,
            "botsUnreadable": unreadable,
            "botsWithIgnoredToggle": ignored,
            "enabledScopes": [s.scope_id for s in self.scopes if s.enabled],
            "note": _governance_note(pod, follows, chosen, residue,
                                     unreadable, ignored),
        }

    def to_dict(self) -> dict:
        return {
            "computedAt": self.computed_at,
            "considered": self.considered,
            "eligible": [d.to_dict() for d in self.eligible],
            "held": [d.to_dict() for d in self.held],
            "skippedProviders": [p.to_dict() for p in self.skipped_providers],
            "degraded": self.degraded,
            "scopes": [s.to_dict() for s in self.scopes],
            "governance": self.governance(),
        }


def _plural_bots(ids: Sequence[str]) -> str:
    n = len(ids)
    return f"{n} bot{'' if n == 1 else 's'}"


def _governance_note(
    pod: ScopeState | None,
    follows: Sequence[str],
    chosen: Sequence[str],
    residue: Sequence[str],
    unreadable: Sequence[str] = (),
    ignored: Sequence[str] = (),
) -> str:
    """One paragraph the toggle surface can render verbatim: how many bots the
    pod setting governs, how many it does not, and which of those look like
    migration leftovers rather than deliberate choices."""
    on = bool(pod and pod.enabled)
    head = (
        f"Pod defaults ({'on' if on else 'off'}) govern {_plural_bots(follows)}"
        f"{': ' + ', '.join(follows) if follows else ''}."
    )
    extras = ""
    if unreadable:
        extras += (
            f" {_plural_bots(unreadable)} could not be read at all "
            f"({', '.join(unreadable)}), so whether this setting governs them is "
            f"unknown — not a yes."
        )
    if ignored:
        extras += (
            f" {_plural_bots(ignored)} ({', '.join(ignored)}) carry their own "
            f"automatic-update setting while following the pod, so that setting "
            f"is ignored."
        )
    excluded = list(chosen) + list(residue)
    if not excluded:
        return head + " No bot is excluded." + extras
    tail = (
        f" {_plural_bots(excluded)} keep their own model config and are NOT "
        f"governed by this setting: {', '.join(excluded)}."
    )
    if residue:
        tail += (
            f" {_plural_bots(residue)} ({', '.join(residue)}) resolve identically "
            f"to the pod defaults — most likely left over from the tier "
            f"migration rather than a choice; resetting them to pod defaults "
            f"would bring them under this setting."
        )
    return head + tail + extras


def upgrade_key(provider: str, rung_slug: str, latest_model: str) -> str:
    """Stable identity of an upgrade: (provider, rung, target version).

    Per-rung, because the same version bump landing in two rungs is two separate
    decisions; per exact target version, because spec §Permanently-held upgrades
    requires suppression to be per-version, not per-family — a pricier 5.0 must
    not silence 5.1.
    """
    return (
        f"{(provider or '').lower()}:{rung_slug}:"
        f"{_md._bare_id(latest_model).lower()}"
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _listing_providers(
    listing_by_provider: Mapping[str, Sequence[Any]] | None,
) -> set[str]:
    """Providers with a NON-EMPTY live listing. An empty list is not evidence of
    "nothing newer" — it is a provider we could not see (rule 2)."""
    return {
        p for p, models in (listing_by_provider or {}).items() if models
    }


def compute_auto_upgrades(
    network: Mapping[str, Any],
    *,
    listing_by_provider: Mapping[str, Sequence[Any]] | None = None,
    pricing_cache: dict[str, Any] | None = None,
    listing_degraded: Iterable[Mapping[str, Any]] = (),
    bot_configs: Mapping[str, dict] | None = None,
    bot_tier_docs: Mapping[str, Mapping[str, Any]] | None = None,
    upgrades: Sequence[Any] | None = None,
    first_seen: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> AutoUpgradeReport:
    """Decide which detected version upgrades would auto-apply, and why the rest
    would not. **Pure — nothing is written and no config is changed.**

    ``listing_by_provider`` is the live listing (``run_discovery``'s
    per-provider ``ListedModel`` lists, or ``hydrate_listing_cache`` off the
    persisted cache). ``listing_degraded`` is the cache's ``degraded`` list
    (``[{provider, reason}]``) — providers whose listing CALL failed; they are
    reported as unchecked, never silently treated as current. A provider the pod
    runs models on that has no non-empty listing is likewise reported.

    ``upgrades`` defaults to a fresh ``compute_version_upgrades`` pass over the
    pod-sourced locations (spec §Apply path step 2: "recompute fresh — never act
    on a cached row"); it is injectable so callers that already computed the
    card's rows judge exactly those rows.

    ``first_seen`` maps :func:`upgrade_key` → an ISO timestamp of when the
    upgrade was first detected. It is an OPTIONAL Phase-3 input: the ledger that
    populates it is written by the apply path, so with no ledger the visibility
    floor cannot be evaluated and is not held against an upgrade (``visible_days``
    stays ``None``). Where a timestamp IS present, an upgrade younger than the
    scope's ``minVisibleDays`` is held — "visible before it happens" is a hard
    precondition (spec §Cadence).
    """
    now = now or datetime.now(timezone.utc)
    pod_models_raw = (network or {}).get("models")
    pod_models: dict[str, Any] = (
        dict(pod_models_raw) if isinstance(pod_models_raw, dict) else {}
    )
    if bot_tier_docs is not None:
        docs: Mapping[str, Mapping[str, Any]] = bot_tier_docs
        unreadable: set[str] = set()
    else:
        docs, unreadable = read_bot_docs(network)
    scopes = resolve_scopes(network, docs, unreadable)
    scope_by_id = {s.scope_id: s for s in scopes}

    locations = _md.pod_sourced_model_locations(
        dict(network), {k: dict(v) for k, v in (bot_configs or {}).items()},
    )
    if upgrades is None:
        upgrades = _md.compute_version_upgrades(
            {p: list(ms) for p, ms in (listing_by_provider or {}).items()},
            locations,
        )

    # ── Providers we could NOT check (rule 2) ────────────────────────────────
    live = _listing_providers(listing_by_provider)
    pod_providers: dict[str, int] = {}
    for loc in locations.values():
        prov = (loc.get("provider") or "").lower()
        if prov:
            pod_providers[prov] = pod_providers.get(prov, 0) + 1
    degraded_reason = {
        str(d.get("provider") or "").lower(): str(d.get("reason") or "")
        for d in (listing_degraded or ()) if isinstance(d, Mapping)
    }
    skipped: list[SkippedProvider] = []
    for prov in sorted(set(pod_providers) | set(degraded_reason)):
        if prov in live:
            continue
        skipped.append(SkippedProvider(
            provider=prov,
            reason=degraded_reason.get(prov, "no listing available"),
            model_count=pod_providers.get(prov, 0),
        ))

    occupancy = rung_occupancy(network, bot_configs, docs)
    listing_bare = {
        p: {getattr(m, "model_id", "").lower() for m in ms}
        for p, ms in (listing_by_provider or {}).items()
    }

    eligible: list[UpgradeDecision] = []
    held: list[UpgradeDecision] = []
    for up in upgrades or ():
        decision = _judge(
            up,
            pod_models=pod_models,
            docs=docs,
            scope_by_id=scope_by_id,
            occupancy=occupancy,
            listing_bare=listing_bare,
            pricing_cache=pricing_cache,
            first_seen=first_seen or {},
            now=now,
        )
        (eligible if decision.eligible else held).append(decision)

    return AutoUpgradeReport(
        computed_at=now.isoformat(),
        eligible=eligible,
        held=held,
        skipped_providers=skipped,
        scopes=scopes,
    )


def _judge(
    up: Any,
    *,
    pod_models: Mapping[str, Any],
    docs: Mapping[str, Mapping[str, Any]],
    scope_by_id: Mapping[str, ScopeState],
    occupancy: Sequence[Mapping[str, set[str]]],
    listing_bare: Mapping[str, set[str]],
    pricing_cache: dict[str, Any] | None,
    first_seen: Mapping[str, Any],
    now: datetime,
) -> UpgradeDecision:
    """Judge ONE :class:`model_discovery.VersionUpgrade` against §"What
    auto-upgrade means" conditions 1-5 and 7 (6, liveness, is Phase 2).

    Conditions 1-3 are already guaranteed by ``compute_version_upgrades``; they
    are re-checked rather than assumed, because a caller may inject rows and
    because an eligibility verdict that TRUSTS its input is how a same-family
    invariant quietly becomes a cross-family apply.
    """
    provider = (getattr(up, "provider", "") or "").lower()
    current_model = getattr(up, "current_model", "") or ""
    latest_model = getattr(up, "latest_model", "") or ""
    rung_slug = getattr(up, "rung_slug", "") or ""
    cur_bare = _md._bare_id(current_model).lower()
    new_bare = _md._bare_id(latest_model).lower()

    decision = UpgradeDecision(
        provider=provider,
        family=getattr(up, "family", "") or _md._family_of(cur_bare),
        current_model=current_model,
        latest_model=latest_model,
        rung_slug=rung_slug,
        roles=list(getattr(up, "roles", []) or []),
    )
    holds: list[HoldReason] = []

    # ── 1. same provider / 2. same family / 3. strictly newer ────────────────
    latest_provider = (latest_model.split("/", 1)[0].lower() if "/" in latest_model
                       else provider)
    if latest_provider != provider:
        holds.append(HoldReason(
            code=HOLD_PROVIDER_MISMATCH,
            message=(
                f"{new_bare} comes from a different provider than {cur_bare}. "
                f"Switching providers changes how you authenticate and what you "
                f"pay, so it is never automatic."
            ),
            detail={"from": provider, "to": latest_provider},
        ))
    if _md._family_of(new_bare) != _md._family_of(cur_bare):
        holds.append(HoldReason(
            code=HOLD_FAMILY_MISMATCH,
            message=(
                f"{new_bare} is a different model line than {cur_bare}, not a "
                f"newer version of it. Adopting a new line is your call."
            ),
            detail={"from": _md._family_of(cur_bare), "to": _md._family_of(new_bare)},
        ))
    elif not _md._is_strictly_newer(new_bare, cur_bare):
        holds.append(HoldReason(
            code=HOLD_NOT_NEWER,
            message=(
                f"{new_bare} is not a newer version than {cur_bare} — nothing "
                f"to update to."
            ),
            detail={"current": cur_bare, "candidate": new_bare},
        ))

    # ── 4. target is GA ──────────────────────────────────────────────────────
    markers = prerelease_markers(new_bare)
    if markers:
        holds.append(HoldReason(
            code=HOLD_TARGET_NOT_GA,
            message=(
                f"{new_bare} is {_PRERELEASE_TOKENS[markers[0]]} release, not a "
                f"general one. "
                f"Automatic updates only move to versions the provider has "
                f"released for everyone — try it yourself first if you want it."
            ),
            detail={"markers": markers},
        ))
    if _md._strip_date_suffix(new_bare) != new_bare and (
        _md._strip_date_suffix(new_bare) in listing_bare.get(provider, set())
    ):
        holds.append(HoldReason(
            code=HOLD_TARGET_SNAPSHOT_ALIAS,
            message=(
                f"{new_bare} is a dated snapshot of "
                f"{_md._strip_date_suffix(new_bare)}, which the provider also "
                f"lists. Riding the rolling id keeps you current without pinning "
                f"you to one build."
            ),
            detail={"base": _md._strip_date_suffix(new_bare)},
        ))

    # ── 7. predecessor occupies exactly one rung ─────────────────────────────
    # Measured PER apply-editable document (see :func:`rung_occupancy`): a model
    # in two rungs of one document has no single replacement site; the pod and a
    # Custom bot each placing it in their own rung is two unambiguous applies.
    located = sorted({
        slug for per_doc in occupancy for slug in per_doc.get(cur_bare, set())
    })
    ambiguous = next(
        (sorted(per_doc[cur_bare]) for per_doc in occupancy
         if len(per_doc.get(cur_bare, ())) > 1),
        None,
    )
    if not rung_slug or not located or rung_slug not in located:
        # Includes the disagreement case (the row names a rung the predecessor
        # does not actually occupy) — an apply target we cannot confirm is not a
        # target we act on unattended.
        holds.append(HoldReason(
            code=HOLD_RUNG_UNKNOWN,
            message=(
                f"Could not tell which tier {cur_bare} sits in, so there is no "
                f"single place to put {new_bare}. Update it from Settings → "
                f"Models."
            ),
            detail={"model": cur_bare, "rung": rung_slug, "rungs": located},
        ))
    elif ambiguous:
        holds.append(HoldReason(
            code=HOLD_RUNG_AMBIGUOUS,
            message=(
                f"{cur_bare} sits in more than one tier ({', '.join(ambiguous)}) "
                f"in the same config, so there is no single place its replacement "
                f"belongs. Update it from Settings → Models."
            ),
            detail={"model": cur_bare, "rungs": ambiguous},
        ))

    # ── 5. cost non-regression (the load-bearing guard) ──────────────────────
    scopes_carrying = _scopes_carrying(
        pod_models, docs, scope_by_id,
        bare_model=cur_bare, rung_slug=rung_slug,
    )
    decision.scopes = [s.scope_id for s in scopes_carrying]
    decision.enabled_scopes = [s.scope_id for s in scopes_carrying if s.enabled]
    # The knobs come from the scopes that would actually apply this upgrade; a
    # scope that turned a guard OFF only relaxes it for its OWN bots, so the
    # guard is only skipped when EVERY carrying scope waived it (and never when
    # no scope carries it at all).
    if rung_slug and not scopes_carrying:
        # Located somewhere the pod routes from, but not in a document an apply
        # could edit under this rung id (e.g. it reaches the merge through a
        # legacy per-bot ``tiers`` block, or only through the code defaults).
        # No document to write means no automatic update, however good the
        # version looks.
        holds.append(HoldReason(
            code=HOLD_SCOPE_UNKNOWN,
            message=(
                f"{cur_bare} is not written into your pod defaults or any bot's "
                f"own model config, so there is no setting that would carry this "
                f"update. Update it from Settings → Models."
            ),
            detail={"model": cur_bare, "rung": rung_slug},
        ))
    knob_scopes = scopes_carrying or [scope_by_id.get("pod")]
    knob_scopes = [s for s in knob_scopes if s is not None]
    require_cost = any(s.policy.require_cost_non_regression for s in knob_scopes) \
        if knob_scopes else DEFAULT_POLICY.require_cost_non_regression
    require_ga = any(s.policy.require_ga for s in knob_scopes) \
        if knob_scopes else DEFAULT_POLICY.require_ga

    cost_holds, cost_evidence = cost_guard(
        provider=provider,
        current_model=current_model,
        latest_model=latest_model,
        pricing_cache=pricing_cache,
    )
    decision.cost_evidence = cost_evidence
    if require_cost:
        holds.extend(cost_holds)
    else:
        cost_evidence["waived"] = True
        cost_evidence["waived_holds"] = [h.to_dict() for h in cost_holds]
    if not require_ga:
        # Spec condition 4 is one condition covering both shapes (pre-release
        # markers AND a dated snapshot whose rolling base also lists), so the
        # knob that waives it waives both rather than half.
        holds = [h for h in holds
                 if h.code not in (HOLD_TARGET_NOT_GA, HOLD_TARGET_SNAPSHOT_ALIAS)]

    # ── Visibility floor (spec §Cadence; ledger is a Phase-3 input) ──────────
    seen = _parse_iso(first_seen.get(decision.key()))
    if seen is not None:
        days = (now - seen).total_seconds() / 86400.0
        decision.visible_days = round(days, 3)
        floor = max(
            (s.policy.min_visible_days for s in knob_scopes),
            default=DEFAULT_POLICY.min_visible_days,
        )
        if days < floor:
            waits = max(1, math.ceil(floor - days))
            holds.append(HoldReason(
                code=HOLD_NOT_VISIBLE_LONG_ENOUGH,
                message=(
                    f"{new_bare} was first seen {days:.1f} day"
                    f"{'' if 0.95 <= days < 1.05 else 's'} ago. Updates stay "
                    f"visible for {floor} days before they can apply on their "
                    f"own, so you always have time to say no — about "
                    f"{waits} day{'' if waits == 1 else 's'} to go."
                ),
                detail={"visible_days": decision.visible_days, "floor": floor},
            ))

    decision.holds = holds
    decision.eligible = not holds
    # Liveness (condition 6) is Phase 2 and has NOT run. An eligible verdict here
    # is "eligible on the guards that exist"; the apply path must refuse to act
    # while a guard is still pending.
    decision.pending_guards = ("liveness",) if decision.eligible else ()
    return decision


def _scopes_carrying(
    pod_models: Mapping[str, Any],
    docs: Mapping[str, Mapping[str, Any]],
    scope_by_id: Mapping[str, ScopeState],
    *,
    bare_model: str,
    rung_slug: str,
) -> list[ScopeState]:
    """The config scopes whose OWN document places this model in this rung — the
    scopes an apply would edit, and therefore the ones whose toggle decides
    whether it happens.

    A Use-pod-defaults bot never appears here: it has no document of its own, so
    the pod scope IS its scope (which is exactly the inheritance §Scope reuses).
    """
    if not rung_slug:
        return []
    out: list[ScopeState] = []
    pod = scope_by_id.get("pod")
    if pod is not None and _doc_places(
        pod_models, bare_model=bare_model, rung_slug=rung_slug,
    ):
        out.append(pod)
    for bot_id, doc in (docs or {}).items():
        scope = scope_by_id.get(bot_id)
        if scope is None or not scope.custom:
            continue
        if _doc_places(doc, bare_model=bare_model, rung_slug=rung_slug):
            out.append(scope)
    return out
