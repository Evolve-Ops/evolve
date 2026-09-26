"""power_cap — the Python side of "what per-bot Power cap does routing use?".

THE ONE PYTHON RESOLVER for the per-role daily cap, the twin of
``ModelRouter.sanitizeDailyCap`` / ``_mergeRoleCaps`` / ``_roleCap`` on the
gateway side (packages/plugin/src/observer/ModelRouter.ts). Standing rule
``cost-cap-readers-must-share-one-resolver`` (#3498): a config key with more
than one consumer gets ONE resolver per stack, same contract, each naming the
other. Do not add a second cap-reading walk anywhere in ``evolve_admin``.

WHY THIS MODULE EXISTS — ``userTierOverride.dailyCap`` reaches routing NOWHERE
------------------------------------------------------------------------------
PR #4023 fixed the file-path half of the ``models cap`` bug (the CLI wrote a
``tiers.json`` the plugin never reads; see ``user_tier_override``) and its
review recorded a second half: ``dailyCap`` is a LEGACY key that any explicit
``roleCaps`` block overrides, so the fix reached routing "only on un-migrated
pods". Measured against the compiled router, that is understated — the real
answer is **no pods**:

* the gateway's models source is ``mergeModelCatalog(network.models,
  tiersFile)``, which folds ``DEFAULT_MODEL_CATALOG`` in as its BASE layer;
* ``DEFAULT_MODEL_CATALOG.roleCaps`` ships ``power: {maxPerDayPerBot: 10}``
  in code — so the merged ``roleCaps`` is non-empty for **every** bot, whether
  or not anyone ever wrote one;
* ``_mergeRoleCaps`` returns that explicit block *before* it looks at
  ``dailyCap``, and ``_roleCap`` then finds ``roleCaps.power.maxPerDayPerBot``
  as a number and never reaches its own legacy branch either.

Both legacy folds are therefore unreachable from the production config-load
path, and an operator running ``evolve-admin models cap <bot> 3`` on a pod
with no ``roleCaps`` anywhere still routed on the code default of 10. That is
the silent divergence between what the operator was told and what the machine
does that ``decision-cost-cap-checkpoint-2026-09-04`` exists to end, so
``models cap`` now writes ``roleCaps.power.maxPerDayPerBot`` — the key
:func:`resolve_effective_power_cap` (and the router) actually reads.

The legacy key is still written, as a mirror, for the readers that have not
moved (``home_chat_routes._read_user_tier_override`` reads ``dailyCap`` and
nothing else). Both legacy branches stay in the router for a hand-rolled
config that carries ``roleCaps: {}``; they are simply not how a pod is
configured.

NOT A CAP READER: ``home_chat_routes._read_user_tier_override``
--------------------------------------------------------------
It reads ``{sharedDir}/{bot}/tiers.json::userTierOverride.dailyCap`` and gates
the admin chat composer's Power chip with it. It deliberately does NOT use
this resolver: its uid-trust gate (``O_NOFOLLOW`` fstat +
``_UNTRUSTED_DAILY_CAP_CEILING``, #3566 A-1) is written against a shared-dir
file the bot holds a write ACE on, and re-deriving it for the bot-OWNED
canonical file is the follow-up the spec addendum already records. The two
answers agree whenever the mirror is in sync, which every write through
``user_tier_override`` keeps true — including, since 2026-09-10, the admin
UI's ``PUT /api/admin/config/<bot>/user-tier-override``, which used to write
the canonical file alone and leave that reader a version behind.
"""

from __future__ import annotations

from typing import Any

from .telemetry import get_logger

log = get_logger("power_cap")

#: The role whose cap the CLI's ``models cap`` sets. ``max`` has a cap too
#: (``roleCaps.max.maxPerDayPerBot``, pod-level in network.json) but no CLI
#: surface — it is deliberately not bot-writable (home_chat_routes §Max gate).
POWER_ROLE = "power"

#: Field name inside a ``roleCaps.<role>`` entry. Hard-coded on both stacks;
#: named here so the read-merge and the resolver cannot drift apart.
CAP_FIELD = "maxPerDayPerBot"

#: Inclusive bounds a cap must satisfy to be honored. Mirrors
#: ``sanitizeDailyCap``'s ``raw >= 0 && raw <= 100`` and the CLI's own
#: range check. 0 is valid — the documented "role disabled" sentinel.
CAP_MIN = 0
CAP_MAX = 100


def default_power_cap() -> int:
    """The product-default Power cap — twin of TS ``defaultRoleCap("power")``.

    Read live from ``primary_bot.DEFAULT_MODEL_CATALOG`` rather than restated,
    for the same reason the TS reads it from its own catalog: a default bump
    must not leave a stale literal at one fold site disagreeing with the
    enforcement point. The two catalogs carry a KEEP IN SYNC banner and
    ``test_power_cap_resolver`` pins this number against the TS source.
    """
    from primary_bot import DEFAULT_MODEL_CATALOG  # type: ignore[import-not-found]

    return int(
        ((DEFAULT_MODEL_CATALOG.get("roleCaps") or {}).get(POWER_ROLE) or {})
        [CAP_FIELD]
    )


def sanitize_daily_cap(raw: Any, fallback: int) -> int:
    """Python twin of ``ModelRouter.sanitizeDailyCap``.

    A cap is honored only when it is a non-boolean finite number in
    [0, 100]; it is truncated toward zero. Anything else — ``1e9``, ``-1``,
    ``NaN``, ``"20"``, ``True`` — returns ``fallback`` (the role's product
    default), NOT a boundary clamp, exactly as the TS does.

    ``bool`` is excluded explicitly because it subclasses ``int`` in Python
    (``dailyCap: true`` must not read as 1); the TS has no such hazard, which
    is why the two bodies are not line-for-line identical. The same exclusion
    is in ``home_chat_routes._read_user_tier_override`` and the
    ``routes_admin_config`` PUT validator.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    # NaN fails every comparison, so this one guard covers NaN, ±inf,
    # negatives and >100 — the same single guard the TS uses.
    if not (CAP_MIN <= raw <= CAP_MAX):
        return fallback
    return int(raw)


def merge_power_cap(role_caps: Any, value: int) -> dict[str, Any]:
    """Return ``role_caps`` with the Power cap set to ``value`` — READ-MERGE.

    ``json_full_config_set``'s ``roleCaps`` key is a WHOLESALE replace (it is
    the "Customize this bot" payload's shape), so a caller that sends only
    ``{"power": ...}`` silently DELETES every other role's cap — on a
    customized bot that is a real widening of ``max`` back to the default.
    Every writer of a single role cap must merge the existing block first;
    this is that merge, in one place.

    Copies at both levels: the caller's block is config read back off disk and
    must not be mutated in place, and the per-role entry keeps any sibling
    fields a future schema adds.
    """
    out: dict[str, Any] = {}
    if isinstance(role_caps, dict):
        for role, entry in role_caps.items():
            out[role] = dict(entry) if isinstance(entry, dict) else entry
    entry = out.get(POWER_ROLE)
    out[POWER_ROLE] = {**entry, CAP_FIELD: value} if isinstance(entry, dict) \
        else {CAP_FIELD: value}
    return out


def resolve_effective_power_cap(
    network: dict[str, Any], tiers_doc: dict[str, Any]
) -> int:
    """The Power cap the gateway would enforce for a bot, from its config.

    Python twin of ``ModelRouter._mergeRoleCaps`` + ``_roleCap("power")``,
    in the same order the router applies them:

      1. ``mergeModelCatalog(network.models, tiers_doc).roleCaps.power.
         maxPerDayPerBot`` — the ``DEFAULT_MODEL_CATALOG ← pod ← bot`` keyed
         merge, which is why a bot that defines nothing still resolves to the
         code default rather than to ``dailyCap``;
      2. the legacy ``userTierOverride.dailyCap`` (bot doc, then pod), reached
         only when that merged block carries no ``power`` entry — i.e. only
         for a hand-rolled catalog, never for a pod configured by Evolve;
      3. :func:`default_power_cap`.

    Every leg goes through :func:`sanitize_daily_cap`, as on the TS side: this
    reads a bot-OWNED file, so its numbers are untrusted input.

    ``network`` is the parsed ``network.json``; ``tiers_doc`` is the RAW
    per-bot ``evolve-tiers.json`` (``primary_bot.read_bot_tiers_doc``, or the
    ``json_full_config`` view's ``roleCaps``/``userTierOverride`` — the merge
    only reads keys both carry).
    """
    from primary_bot import merge_model_catalog  # type: ignore[import-not-found]

    fallback = default_power_cap()
    pod_models = (network or {}).get("models")
    merged = merge_model_catalog(
        pod_models if isinstance(pod_models, dict) else {},
        tiers_doc if isinstance(tiers_doc, dict) else {},
    )
    caps = merged.get("roleCaps")
    entry = (caps or {}).get(POWER_ROLE) if isinstance(caps, dict) else None
    if isinstance(entry, dict) and CAP_FIELD in entry:
        return sanitize_daily_cap(entry[CAP_FIELD], fallback)

    for source in (tiers_doc, network):
        override = (source or {}).get("userTierOverride")
        if isinstance(override, dict) and "dailyCap" in override:
            return sanitize_daily_cap(override["dailyCap"], fallback)
    return fallback


def resolve_from_config_view(
    network: dict[str, Any], config: dict[str, Any]
) -> int:
    """:func:`resolve_effective_power_cap` against a ``json_full_config`` view.

    The two shapes are not the same object: the resolver wants the RAW
    ``evolve-tiers.json`` document, and ``oc_model.json_full_config`` returns a
    view of it that carries ``roleCaps`` and ``userTierOverride`` as sibling
    top-level keys alongside a dozen things the resolver must not see (a
    ``tiers`` list, ``catalog``, ``tiersKeysWritten``). Projecting here rather
    than at each call site is what keeps "which keys does the cap come from"
    a single fact — the same reason this module exists at all.

    A dict-typed ``roleCaps`` is not assumed: ``json_full_config`` normalizes
    it, but a write result stubbed in a test (or a future caller passing the
    raw doc) may not, and ``merge_model_catalog`` would fold a list straight
    into the merged block.
    """
    caps = config.get("roleCaps")
    override = config.get("userTierOverride")
    return resolve_effective_power_cap(network, {
        "roleCaps": caps if isinstance(caps, dict) else {},
        "userTierOverride": override if isinstance(override, dict) else {},
    })


def reported_power_cap(
    network: dict[str, Any], config: dict[str, Any]
) -> "int | None":
    """:func:`resolve_from_config_view` for a REPORTING surface, or ``None``.

    Used where the write has already landed and the resolver is the only thing
    left that can fail — the admin UI's PUT response. It imports ``primary_bot``
    out of the analyzer package to read the product default and the catalog
    merge; a process where that import or that catalog is not intact must not
    turn a successful write into a 500.

    ``None`` means "this process could not work the answer out", and every
    caller must render it as that, NEVER as an absent cap or as 0 — the
    difference between "unknown" and "Power disabled" is the whole control.
    Enforcement-side callers use :func:`resolve_from_config_view` and let it
    raise; see ``feedback_shared_fallback_erases_broken_vs_unavailable``.
    """
    try:
        return resolve_from_config_view(network, config)
    except Exception:
        log.warning(
            "could not resolve the effective Power cap; reporting it as "
            "unknown", exc_info=True,
        )
        return None
