/**
 * ModelRouter — wires session classification to OC's before_model_resolve hook.
 *
 * Reads session type from TurnObserver's in-memory state and returns the
 * appropriate model override based on tier configuration.
 *
 * Design principles:
 * - NEVER changes model for productive or ambiguous sessions (use default)
 * - Only overrides for: maintenance, background, cron
 * - Respects user-explicit overrides (/model command already set)
 * - Fails open: if classification unavailable, return {} (no override)
 * - No LLM calls, no async I/O in the hot path — pure in-memory lookup
 *
 * Config source priority:
 *   1. ~/.openclaw/evolve-tiers.json   — written by admin UI's AI
 *                                        Optimization page (canonical)
 *   2. {sharedDir}/{botId}/tiers.json  — legacy / hand-rolled fallback
 *   3. network.json models.*           — pod-wide fallback
 *   4. Fail open (empty config → bot default model wins everything)
 *
 * NOTE: tiers are NOT read from openclaw.json. OC's schema validator rejects
 * unknown fields under agents.defaults.model, so Evolve stores tier/routing
 * config in its own evolve-tiers.json file under the bot's .openclaw dir.
 */
export interface AccountTier {
    description?: string;
    profiles: string[];
    for_session_types: string[];
}
/**
 * Canonical role IDs — the single namespace code, users, and telemetry
 * speak (spec-model-rungs-and-roles-2026-06-09 §Roles). `judge` is a
 * structured role (provider-diversity constraint); the rest are plain
 * rung pointers.
 */
export type RoleId = "fast" | "standard" | "power" | "max" | "judge";
/** A capability/cost cluster in the rung catalog (cheapest first). */
export interface Rung {
    id: string;
    models: string[];
    costClass?: "low" | "medium" | "high" | "premium";
}
/** Structured form of the `judge` role: a rung + a provider constraint. */
export interface JudgeRole {
    rung: string;
    provider: "not-standard";
}
export interface ModelRouterConfig {
    rungs: Rung[];
    roles: {
        fast?: string;
        standard?: string;
        power?: string;
        max?: string;
        judge?: string | JudgeRole;
    };
    roleCaps?: {
        power?: {
            maxPerDayPerBot?: number;
        };
        max?: {
            maxPerDayPerBot?: number;
        };
    };
    routing?: {
        maintenanceRole?: string;
        backgroundRole?: string;
        ambiguousRole?: string | null;
        enabled?: boolean;
        confidenceThreshold?: number;
    };
    accountTiers?: Record<string, AccountTier>;
    accountRouting?: {
        enabled?: boolean;
    };
    runawayRateCap?: {
        enabled?: boolean;
        dollarsPerWindow?: number;
        windowMinutes?: number;
        criticalTripsPer24h?: number;
    };
    cascade?: {
        enabled?: boolean;
    };
    userTierOverride?: {
        enabled?: boolean;
        dailyCap?: number;
        allowBotInitiated?: boolean | {
            power?: boolean;
            max?: boolean;
        };
        defaultRole?: string;
        defaultTier?: string;
    };
    userTierPrefs?: {
        users: Record<string, {
            defaultRole?: string;
            defaultTier?: string;
        }>;
    };
}
/**
 * DEFAULT_MODEL_CATALOG — Evolve's blessed model ladder, shipped in code.
 *
 * KEEP IN SYNC with `DEFAULT_MODEL_CATALOG` in
 * packages/analyzer/primary_bot.py — the two must resolve a given (pod, bot)
 * override pair to byte-identical merged catalogs. A reviewer traces parity
 * rule-by-rule; the parity fixtures on both sides enforce it.
 *
 * Spec: docs/spec-model-rungs-and-roles-2026-06-09.md §Addendum 2 (2026-06-10):
 * product capabilities ship as code defaults; proposals/config carry instance
 * state. This is the BASE layer of the keyed merge:
 *
 *     code defaults (this) ← network.json (pod) ← evolve-tiers.json (bot)
 *
 * Max ships ARMED, not dormant — cost safety holds via pull-only routing, the
 * per-role daily cap (roleCaps.max.maxPerDayPerBot), and the per-bot breaker.
 *
 * MODEL LAUNCH: update this catalog (and the Python mirror) at each release
 * when the blessed ladder changes — new frontier SKU, new fast-band entry, a
 * retired model. Discovery surfaces market drift when this lags (by design).
 */
export declare const DEFAULT_MODEL_CATALOG: any;
/** Deep copy of DEFAULT_MODEL_CATALOG — never hand out the shared constant. */
export declare function defaultModelCatalog(): any;
/**
 * Synthesize the rungs/roles config from whichever shape the source
 * carries. Accepts both:
 *   - new shape: { rungs: [...], roles: {...}, roleCaps: {...} }
 *   - legacy shape: { tiers: { tier0..tier3: {models} } } — fail-open
 *     back-compat for an un-migrated pod (spec §Legacy-shape fallback).
 *     Synthesizes one rung per tier (preserving cost order
 *     tier3<tier2<tier1<tier0-onto-sonnet) and a role map per
 *     _LEGACY_TIER_TO_ROLE, logging a one-time deprecation warning.
 *
 * Returns `{ rungs, roles, roleCaps }` — never throws. An empty/absent
 * source yields empty rungs + roles (the loader then falls through to
 * bot defaults exactly as before).
 *
 * `legacyCaps` carries the old `userTierOverride.dailyCap` (power-role
 * cap) so the caller can fold it into roleCaps.power when the new
 * roleCaps block is absent.
 *
 * NOTE: legacy `tiers.tierN.fallbacks` are read on the Python side only
 * (primary_bot._normalize_legacy_layer folds them into the cluster) — by
 * design, see F2 of the #2561 review. TS reads `tiers.tierN.models` only and
 * routes legacy fallbacks via the profile-fallback chain; don't "fix" this to
 * fold fallbacks here.
 */
export declare function synthesizeRungsRoles(source: any): {
    rungs: Rung[];
    roles: ModelRouterConfig["roles"];
    roleCaps?: ModelRouterConfig["roleCaps"];
};
export declare function mergeModelCatalog(base: any, override: any, opts?: {
    includeDefaults?: boolean;
}): any;
/**
 * True iff `override` EXPLICITLY speaks for `tier` but yields no usable model.
 *
 * The companion to the empty-rung-is-a-no-op merge rule (`mergeOneRung`). The
 * merge intentionally lets the code-default / pod base fill a tier whose
 * per-bot override is empty, so the bot still resolves and works. But an
 * operator who hand-wrote `tier2: {models: []}` (or all-whitespace) authored
 * BROKEN config, not absent config — the Evolve admin onboarding /
 * setup-checklist surfaces flag it so they fix it, even though the runtime
 * falls back gracefully.
 *
 * Inspects the RAW per-bot override (never the merged catalog): the merge has
 * already hidden the breakage by design. "Explicitly speaks" = the override
 * carries an entry keyed to this tier's role/rung, via either shape:
 *   - legacy: `tiers.<tierN>` present (an object) but its `models` are nothing
 *     usable.
 *   - new shape: a `rungs` entry whose id is the role's rung (e.g.
 *     `sonnet-class` for tier2->standard), present but with no usable models.
 * Returns false when the override simply OMITS the tier (absent → defaulted,
 * which is configured per spec §Addendum 2).
 *
 * Mirrors `tier_override_is_broken` in primary_bot.py — keep the two in sync.
 */
export declare function tierOverrideIsBroken(override: any, tier: string): boolean;
/**
 * Normalize a routing block to the role-shaped field names, tolerating
 * the legacy `*Tier` keys. Maps a legacy `tierN` value to its role via
 * _LEGACY_TIER_TO_ROLE. Never throws.
 */
export declare function normalizeRouting(raw: any): ModelRouterConfig["routing"];
/**
 * Operator-selected tier for a session. Per
 * docs/spec-user-tier-control-2026-05-26.md, the operator can override
 * the classifier on a per-turn basis from the admin-UI chat composer.
 * The choice arrives at the plugin via the EVOLVE_TIER_PREFERENCE env
 * var set on the openclaw subprocess by the admin server's proxy.
 *
 * "auto" is sentinel-equivalent to "no override" — the map entry is
 * deleted when the operator selects Auto, so the classifier wins.
 */
export type UserTierChoice = "fast" | "standard" | "power" | "max";
export interface ParseTierDirectiveOpts {
    /**
     * Whether the current surface legitimately receives a server-emitted
     * session-context tier directive. True only for the admin/home-chat
     * gateway surface (ModelRouterConfig.role === "primary"). False (the
     * default) for member bots and any other surface, where NO
     * message-borne directive is honored — those surfaces route via the
     * EVOLVE_TIER_PREFERENCE env var instead. Keeping the default false is
     * fail-closed: a caller that forgets to declare its surface cannot be
     * privilege-escalated by untrusted message text.
     */
    trustMessageDirective?: boolean;
}
export declare function parseTierDirective(message: string | null | undefined, opts?: ParseTierDirectiveOpts): UserTierChoice | null;
export declare class ModelRouter {
    private config;
    private sessionTypes;
    private sessionUserTiers;
    private sessionConsentSources;
    private sessionUserKeys;
    private sharedDir;
    private botId;
    private sessionCostHistory;
    private sessionRunawayTripped;
    private _runawayTripsToday;
    private _tier1ActiveSessions;
    private _tier1ActiveFileInitialized;
    private _tier1CallsToday;
    private _maxCallsToday;
    private sessionCascadeVerdicts;
    private sessionLastDecisionDriver;
    private sessionPreflightDecisions;
    private _authProfilesCache;
    private static readonly _AUTH_PROFILES_TTL;
    private _credentialedProvidersOverride;
    /** @internal test-only — inject the credentialed-provider set. */
    _setCredentialedProvidersForTest(providers: Iterable<string> | null): void;
    /**
     * Normalize a config passed to the constructor. Accepts both the new
     * {rungs, roles} shape and the legacy {tiers:{tierN}} shape (the latter
     * synthesized via synthesizeRungsRoles). Routing keys are normalized to
     * the *Role form (legacy *Tier tolerated). A legacy
     * userTierOverride.dailyCap is folded into roleCaps.power when no
     * explicit roleCaps is present. This makes both production loaders and
     * direct test construction tolerate either shape.
     */
    private static _normalizeConfig;
    constructor(config: ModelRouterConfig, sharedDir?: string, botId?: string);
    /** Lookup a rung by slug; null when absent. */
    private _rung;
    /** Provider prefix of a model string ("anthropic/claude-..." -> "anthropic"). */
    private _providerOf;
    /**
     * Resolve a role ID to a concrete model string (or null when the role,
     * its rung, or the rung's models are unconfigured). The single
     * translation point between the role namespace code/users speak and
     * the model string OC consumes.
     *
     *   fast/standard/power/max → roles[role] is a rung slug → rung.models[0]
     *   judge → structured {rung, provider:"not-standard"}: prefers the first
     *           rung model whose provider differs from standard's, but falls
     *           back to the first model (same vendor) so judge still routes —
     *           diversity is a preference, not a hard constraint.
     */
    resolveRoleToModel(role: string): string | null;
    /**
     * Resolve the judge model honoring provider diversity vs standard's
     * resolved provider as a PREFERENCE, not a hard constraint (2026-06-19).
     * Prefers the first rung model whose provider differs from standard's; if
     * none exists, falls back to the first rung model (same vendor) so judge
     * STILL ROUTES — diversity is a recommendation. Null only when the rung is
     * empty. Mirrors primary_bot._resolve_judge_availability (None-creds path).
     */
    private _resolveJudgeModel;
    /** Providers naming ≥1 model in any rung cluster (the LLM-capable set). */
    private _llmProvidersFromCatalog;
    /**
     * The set of providers that have a usable credential in auth-profiles,
     * keyed by each profile's own `provider` field (no provider-name literal).
     * A profile counts only when it carries a non-empty key/token/api_key.
     */
    private _credentialedProviders;
    /**
     * Providers role resolution may pick from: credentialed ∩ llm-capable.
     * Intersecting against the catalog-derived LLM set drops non-LLM
     * credentials (brave, runway, …) without naming any provider.
     */
    availableProviders(): Set<string>;
    /**
     * Resolve a role to a concrete model, degrading down the ladder when no
     * provider in its rung is available, tagging the outcome with a unified
     * reason. judge resolves via its own diversity machinery and never
     * degrades through this ladder.
     */
    resolveRoleAvailability(role: string): {
        requestedRole: string;
        resolvedRole: string | null;
        model: string | null;
        degraded: boolean;
        reason: "uncredentialed" | "unconfigured" | "same_vendor_as_standard" | null;
        providers: string[];
    };
    /** Sorted provider set of a rung's models. */
    private _rungProviders;
    /**
     * Downward degradation step shared by cap-exhaustion and availability:
     * max→power→standard→fast, fast/judge terminal (null). The cap path's
     * degradeRoleOnCap terminates at standard; this one continues to fast so
     * an uncredentialed standard can still reach a cheaper credentialed rung.
     */
    private _degradeRole;
    /**
     * Judge model honoring diversity as a PREFERENCE plus availability.
     * Returns `{model, sameVendor}`:
     *   - Pass 1 — an available provider OTHER than standard's → `sameVendor:false`.
     *   - Pass 2 — an available provider even if it equals standard's → routes
     *     with `sameVendor:true` (the soft `same_vendor_as_standard` advisory).
     *   - none available → `{model:null, sameVendor:false}` (genuine hard break).
     * Mirrors primary_bot._resolve_judge_availability's two-pass ladder.
     */
    private _resolveJudgeModelAvailable;
    /**
     * Map a user/role choice to the cascade controller's internal Tier
     * symbol (tier0-tier3), which the controller's state machine still
     * speaks. `max` has NO cascade Tier — it is pull-only and the cascade
     * can never produce it (spec §max semantics #2). Returns null for
     * `max` and any unknown role so callers drop it from cascade inputs.
     */
    private _roleToCascadeTier;
    /**
     * Build the effective roleCaps, preferring the explicit new-shape
     * block (tiersFile then network.models) and folding a legacy
     * `userTierOverride.dailyCap` into roleCaps.power when no explicit
     * power cap is present. Used only by reloadConfig.
     */
    private _mergeRoleCaps;
    /** Inverse of _roleToCascadeTier for reading cascade verdicts back. */
    private _cascadeTierToRole;
    /**
     * Return the model the safety-net branches should downgrade to when
     * they fire. Prefers tier3's configured model; when tier3 is
     * unconfigured or empty, returns ``_SAFETY_NET_REFUSE_SENTINEL`` —
     * an unresolvable model id that causes OC to fail the turn entirely.
     *
     * Refusing the turn is the correct behavior when the breaker fires
     * with no configured downgrade target. The previous chain:
     *   Pre-#1767:   returned null → OC used bot default → lying telemetry
     *                (breaker reported as fired; actually no-op cost-wise)
     *   #1767:       returned hardcoded haiku → cost capped but violated
     *                "no hardcoded models in code" principle
     *   This change: returns sentinel → OC fails → bot stops spending →
     *                loud operator-visible signal in gateway.log
     *
     * Telemetry honesty: ``sessionLastDecisionDriver`` is still stamped
     * ``runaway`` / ``spend_cap`` so audits can attribute the refusal to
     * the breaker that triggered it. The model field carries the sentinel
     * so audits can distinguish "breaker fired successfully (tier3
     * downgrade)" from "breaker fired but turn refused (tier3 empty)".
     *
     * Logs a warning the first time a refusal happens so operators see
     * the cause in the gateway log. Repeated refusals in the same
     * process are silent (avoid spamming during a runaway session).
     */
    private _safetyNetRefusalWarned;
    private _safetyNetDowngradeModel;
    /**
     * Startup-time validation: if a safety net is wired up (runawayRateCap
     * enabled, or sharedDir+botId set such that spend-cap can fire) but the
     * `fast` role has no models, warn the operator. The breaker will REFUSE
     * turns when it fires (via the unresolvable sentinel) instead of
     * silently routing to bot default. Operator gets the heads-up at
     * boot, before any turn has to be refused.
     */
    private _warnIfSafetyNetWithoutFastRole;
    /**
     * Startup-time advisory (spec §judge provider-diversity): note when the judge
     * role prefers a provider different from `standard` but its rung contains ONLY
     * models from standard's provider. Diversity is a PREFERENCE (2026-06-19):
     * _resolveJudgeModel still returns standard's vendor so judge ROUTES — this is
     * a soft nudge to add a cross-vendor model for independent evaluation, not a
     * routing failure. (An empty rung / genuinely unroutable judge is handled by
     * the resolver's `unconfigured` / `uncredentialed` reasons, not here.)
     */
    private _warnIfJudgeSameVendor;
    /**
     * Resolve the per-user OR operator default tier (audit #69 Phase A +
     * Phase C).
     *
     * Precedence within the "fall-back-to-bot-default" branch:
     *   1. Per-user pref (Phase C) — when sessionUserKeys has an entry
     *      for this session AND userTierPrefs.users contains an entry
     *      for that user_key with a non-"auto" defaultTier, that wins.
     *   2. Operator default (Phase A) — userTierOverride.defaultTier.
     *
     * Choice mapping for both sources:
     *   • "auto" / missing / unknown → fall through (next source, then
     *     OC bot default)
     *   • "fast"     → ``[tier3.models[0], "tier3"]``
     *   • "standard" → ``[tier2.models[0], "tier2"]``
     *   • "power"    → ``[tier1.models[0], "tier1"]``
     *
     * Called from _resolveModelAndTier's classifier branch (sessionType
     * unset / productive / ambiguous) BEFORE the bot-default fallback.
     * Slots BELOW the chip / session_set_tier user override (level 2)
     * and BELOW the cascade verdict (level 3). Both per-user and
     * operator defaults sit at level 4a; the per-user pref simply wins
     * the tie-break.
     *
     * Returns ``[null, null]`` when both sources resolve to no override
     * (auto / missing / unknown / empty tier — same conservative
     * behavior as the operator default in Phase A).
     *
     * Implementation note: the resolver scans the per-user side first.
     * Callers can't distinguish source from the return value alone —
     * the driver string ("user_default" vs "operator_default") is set
     * by the caller using ``getLastResolvedDefaultSource()``.
     */
    private _resolveOperatorDefaultRole;
    private _lastResolvedDefaultSource;
    /**
     * Resolve a role-choice string to [model, roleId]. "auto" / missing /
     * unknown → [null, null] (fall through). `max` resolves only when
     * allowMax is true — classifier / operator-default callers pass false
     * (pull-only). `judge` is never a default-routing choice.
     */
    private _resolveRoleFromChoice;
    /**
     * Pin a user identity onto a session (audit #69 Phase C). Called by
     * TurnObserver on every user turn with ``ext:<channel>:<sender_external_id>``
     * (when both pieces of identity are present). Subsequent routing
     * decisions for this session consult userTierPrefs[user_key] before
     * falling back to the operator default.
     *
     * Pass ``null`` / empty string to clear the binding — used when a
     * surface lacks identity context (heartbeat, anonymous flows). The
     * session then routes per the operator default only.
     *
     * No-op when sessionKey is empty (defense in depth — same posture as
     * setUserTier).
     */
    setSessionUserKey(sessionKey: string, userKey: string | null | undefined): void;
    /**
     * Read the user_key pinned on a session, or null if none.
     * Test helper / diagnostic surface.
     */
    getSessionUserKey(sessionKey: string): string | null;
    /**
     * Reverse-lookup: given a model string (e.g. "claude-sonnet-4-6" or
     * "anthropic/claude-sonnet-4-6"), return the ROLE ID ("fast" |
     * "standard" | "power" | "max" | "judge") whose rung this model
     * belongs to per the pod's config.
     *
     * Used by cascade telemetry (Phase 1 of spec-tier-cascade-2026-05-26)
     * to record what role was *actually* used by OC, not the role the
     * classifier intended. See spec § 6.3 and the failure-mode review
     * finding F8 — the calibration loop must read what was used from
     * reality, not intent.
     *
     * Returns null when the model isn't found in any role's rung (unknown
     * model, stale config, OC override of a model not in our catalog).
     * Callers should fall back to the intended role with an
     * "unknown" marker so audits can flag the case.
     *
     * Match logic: the model string in OC may include a provider prefix
     * ("anthropic/claude-sonnet-4-6"), a bare model name
     * ("claude-sonnet-4-6"), or a versioned alias. We try exact match
     * first, then suffix/prefix match (provider-prefix tolerance), then
     * substring match.
     *
     * Tie-break order when multiple roles tie on specificity + length:
     *   standard → power → max → fast → judge (see _rolePreferenceRank).
     *
     * Rationale: operators routinely list the same model in multiple rungs
     * (e.g. claude-sonnet-4-6 in the standard rung AND as a judge
     * fallback). Without an operational preference, iteration order could
     * pick judge — but judge is reserved for cross-provider validation,
     * NOT the typical operational role. Prefer the most-operationally-
     * likely role so spans attribute as the operator expects.
     */
    getRoleForModel(modelString: string | undefined | null): string | null;
    /**
     * Legacy reverse-lookup alias: returns the old tier key ("tier0".."tier3")
     * for a model. Kept so any un-migrated external caller still resolves;
     * derived from getRoleForModel via the role->tier map. Prefer
     * getRoleForModel in new code.
     */
    getTierForModel(modelString: string | undefined | null): string | null;
    /**
     * Read auth-profiles.json from the bot's home directory (cached, 1-min TTL).
     * Returns the "profiles" object (profile_id → profile entry).
     * Never throws — returns {} on any error.
     */
    private _loadAuthProfiles;
    /**
     * Return true if the given auth profile ID has a non-empty key or token
     * in the bot's auth-profiles.json.
     *
     * Profile ID format matches the keys in auth-profiles.json
     * (e.g. "anthropic_token", "anthropic_api_key", or the full
     * "anthropic:user@example.com" format openclaw uses for Max accounts).
     */
    private _profileHasKey;
    /**
     * Called by TurnObserver when session type is classified/updated.
     * Stores classification in memory for use by before_model_resolve.
     *
     * Most callers should prefer ``setSessionTypeIfMoreSpecific`` — this
     * raw setter unconditionally overwrites, which is correct for fresh
     * sessions or explicit reclassification, but wrong for the agent_end
     * classifier path (see the IfMoreSpecific docstring for the failure
     * mode this guards against).
     */
    setSessionType(sessionKey: string, sessionType: string): void;
    /**
     * Set the session class IFF it would not downgrade specificity.
     *
     * Specificity ranking (low → high):
     *   undefined < "ambiguous" < {"productive", "maintenance", "background"}
     *
     * Rules:
     *   • Existing undefined  → always write (no info loss)
     *   • Existing ambiguous  → write iff new is specific (upgrade)
     *   • Existing specific   → write iff new is also specific (lateral
     *     reclassification is allowed; downgrade to ambiguous is not)
     *
     * WHY THIS EXISTS (the L4 audit / PR #1737 follow-up):
     *   The agent_end keyword classifier returns ``ambiguous`` when given
     *   empty user/assistant text — which is exactly the shape of a
     *   heartbeat session ("", ""). On turn 1 the trigger anchor sets
     *   sessionType=background; turn 1's agent_end then USED to unconditionally
     *   overwrite it with ``ambiguous`` (raw setSessionType call). On turn 2,
     *   the trigger-anchor guard at TurnObserver.resolveModelRouting saw a
     *   truthy existing class and skipped, then resolveModelOverride read
     *   ``ambiguous`` and returned null (bot default = Sonnet). Net effect:
     *   every turn 2+ of every heartbeat session silently ran on primary
     *   instead of the configured tier3 floor — same user-visible symptom
     *   as the PR #1737 bug, recreated by the post-hoc classifier.
     */
    setSessionTypeIfMoreSpecific(sessionKey: string, newType: string): void;
    /**
     * Read the current session classification, or undefined when none is
     * cached yet. Used by the trigger-kind pre-classification path in
     * TurnObserver.resolveModelRouting to decide whether to anchor a
     * new session's class on its trigger before model selection runs —
     * a prior turn's classifier verdict (set via setSessionType from
     * agent_end) wins over the trigger anchor when present.
     */
    getSessionType(sessionKey: string): string | undefined;
    /**
     * Set the operator's tier preference for this session. Used by the
     * admin-UI chat composer: each turn carries a tier choice (Auto / Fast
     * / Standard / Power); the admin proxy forwards it via the
     * EVOLVE_TIER_PREFERENCE env var, and TurnObserver calls this on
     * every before_model_resolve fire so the override always reflects the
     * latest choice (not the first turn's).
     *
     * "auto" / null / unknown values clear the entry — the classifier
     * then drives routing as before.
     */
    setUserTier(sessionKey: string, choice: string | null | undefined, consentSource?: "ui_chip" | "ask_hint_agreed" | "bot_initiated" | "evo_keyword"): void;
    /**
     * Read the operator's per-role bot-initiated permission. The
     * `allowBotInitiated` config is now per-role
     * ({power, max}); a legacy boolean maps to {power: <value>, max: false}
     * (§max semantics #4 — max defaults to false even under a legacy
     * `allowBotInitiated: true`, because a bot may forward a user's
     * explicit ask but never unilaterally pin Fable). Default for an
     * absent value is power=true (legacy unrestricted), max=false.
     */
    private _allowBotInitiated;
    /** Per-role daily cap (turns/bot/day). power default 10, max default 5. */
    private _roleCap;
    /** Per-role used-today counter, rolled at pod-local midnight. */
    private _roleUsedToday;
    /**
     * Check whether escalating this session to `role` is allowed under the
     * operator's config + current daily usage, generalizing the old
     * `canEscalateToTier1` to per-role caps (spec §max semantics #6).
     *
     * Roles `fast`/`standard` have no cap and are always allowed. For
     * `power` and `max`:
     *   1. ``userTierOverride.enabled`` (default true) — global kill-switch
     *      for the operator-tier-control feature.
     *   2. per-role ``allowBotInitiated`` (power default true, max default
     *      false) — operator can forbid bot self-escalation per role.
     *   3. per-role daily cap (``roleCaps.<role>.maxPerDayPerBot``;
     *      power default 10, max default 5). On exhaustion the caller
     *      degrades down the chain (max→power→standard) via
     *      degradeRoleOnCap().
     *
     * Returns ``{allowed: true}`` when no gate trips.
     */
    canEscalateToRole(role: string): {
        allowed: boolean;
        reason?: "feature_disabled" | "bot_initiated_disabled" | "daily_cap_exhausted";
        detail?: string;
    };
    /**
     * Back-compat alias for the old single-tier gate. SetTierTool and any
     * un-migrated caller can keep calling this; it delegates to the
     * power-role gate.
     */
    canEscalateToTier1(): {
        allowed: boolean;
        reason?: "feature_disabled" | "bot_initiated_disabled" | "daily_cap_exhausted";
        detail?: string;
    };
    /**
     * Degradation chain for a capped role (spec §max semantics #6):
     * max→power→standard, power→standard. Returns the next role to try.
     * `standard` (and any uncapped role) degrades to itself — the chain
     * terminates at the workhorse. Pure function; the caller re-checks
     * canEscalateToRole on the returned role.
     */
    degradeRoleOnCap(role: string): string;
    /**
     * Read the consent source for the active user-tier override, or null if
     * no override is set. Used by:
     *   - Cascade controller (Phase 2+) for de-escalation gating per spec § 2.2
     *   - Cascade telemetry to record consent_source on each span
     *
     * Defaults to "ui_chip" when not explicitly set (back-compat with the
     * pre-Phase-2 setUserTier calls from api_home_chat which doesn't pass
     * a consent_source).
     */
    getConsentSource(sessionKey: string): "ui_chip" | "ask_hint_agreed" | "bot_initiated" | "evo_keyword" | null;
    /**
     * Read the active user-requested ROLE for a session, or null if no
     * override is set. Returns a role ID
     * ("fast" | "standard" | "power" | "max").
     */
    getUserRole(sessionKey: string): UserTierChoice | null;
    /**
     * Read the active user-requested tier for a session in the cascade
     * controller's internal Tier form ("tier1" | "tier2" | "tier3"), or
     * null when there's no override OR the override is `max` (pull-only,
     * §max semantics #2 — the cascade never sees `max`, so a max-pinned
     * session reads as "no cascade-visible request"; the user-override
     * branch in resolveModelOverride already applied it above the cascade).
     *
     * Used by CascadeController's shadow-mode integration in TurnObserver.
     */
    getUserTier(sessionKey: string): "tier1" | "tier2" | "tier3" | null;
    /**
     * True when the operator has opted this bot in to cascade-driven
     * routing. Reads `config.cascade.enabled` (loaded from
     * `{shared}/{bot}/tiers.json::cascade.enabled`). Default false:
     * config-omitted bots stay on the classifier post-cutover until
     * the operator explicitly flips the flag per-bot.
     */
    isCascadeEnabled(): boolean;
    /**
     * Store the cascade controller's verdict for application on the
     * NEXT turn of this session. Called by TurnObserver at the end of
     * each turn (in agent_end) after `cascadeController.decide()`.
     *
     * Pure write — no I/O, no routing side effects. The verdict is
     * applied only when `isCascadeEnabled()` is true AND the next
     * turn's `resolveModelOverride()` reaches the cascade branch (i.e.,
     * not pre-empted by runaway / spend-cap / user-override).
     *
     * Passing `null` clears the verdict (used in tests + on session end).
     */
    setCascadeVerdict(sessionKey: string, verdict: {
        tier: "tier1" | "tier2" | "tier3";
    } | null, tsMs?: number): void;
    /**
     * Read the current cascade verdict for a session. Returns null
     * when no verdict has been recorded (e.g., before the first turn
     * completes) — caller should fall through to the classifier.
     */
    getCascadeVerdict(sessionKey: string): {
        tier: "tier1" | "tier2" | "tier3";
    } | null;
    /**
     * Store a pre-flight intent router decision for application on THIS
     * turn (the one whose `before_model_resolve` hook just fired). Called
     * by TurnObserver after `PreflightIntentRouter.classify()`.
     *
     * Pure write. The decision is applied only when `_resolveModelAndTier`
     * reaches the productive/ambiguous branch AND no explicit operator /
     * user default has fired above it.
     *
     * Passing `null` clears the slot (used on session end + when the
     * router returns ABSTAIN, so we don't keep a stale decision around).
     *
     * Phase 1: this map stays empty in production because the router
     * always returns ABSTAIN (tier=null) and the caller doesn't store
     * abstains. The setter wiring exists so Phase 2/3+ are router-only
     * changes.
     */
    setSessionPreflightDecision(sessionKey: string, decision: {
        tier: "tier1" | "tier2" | "tier3";
        reason: string;
    } | null): void;
    /**
     * Read the current pre-flight decision for a session, or null when
     * none was recorded. Used by `_resolveModelAndTier` to consult the
     * slot, and by TurnObserver to mirror the decision onto the cascade
     * span as `cascade.preflight.tier` + `cascade.preflight.reason`.
     */
    getSessionPreflightDecision(sessionKey: string): {
        tier: "tier1" | "tier2" | "tier3";
        reason: string;
    } | null;
    /**
     * Read what drove the last `resolveModelOverride()` call for a
     * session. Returns null if no resolution has happened yet (or the
     * session has been cleared). Used by TurnObserver to set
     * `cascade.tier_chosen_by` on the telemetry span — when this
     * returns "cascade", the audit layer knows the controller drove
     * routing (vs. shadow mode where it would have but didn't).
     */
    getLastDecisionDriver(sessionKey: string): "runaway" | "spend_cap" | "user_request" | "cascade" | "preflight" | "classifier" | "operator_default" | "user_default" | null;
    /**
     * Called by TurnObserver when a session ends. Cleans up memory.
     */
    clearSession(sessionKey: string): void;
    /**
     * Update the in-process power-role set for a session, given the ROLE
     * the turn resolved to (or null when we fell through to bot-default).
     * Idempotent: a session that flips from power → power across turns
     * produces no I/O. Also bumps the per-day power/max counters on
     * transition into the respective role.
     *
     * The watchdog's tier1_active.json tracks the `power` role (formerly
     * tier1); the file name is kept for back-compat with the reader.
     */
    private _markSessionTier;
    private _lastResolvedRoleWasMax;
    /**
     * Atomically write the current tier1-active count to
     * {sharedDir}/{botId}/cascade/tier1_active.json. Best-effort: any
     * filesystem error is swallowed. Logs a hint via the silent fail-
     * open path so a misconfigured sharedDir/botId is recoverable on
     * the next call.
     *
     * Made `protected` so test subclasses can override and assert on
     * call frequency without needing real filesystem state.
     */
    protected _writeTier1ActiveFile(): void;
    private _tier1ActiveWarnedEACCES;
    /**
     * Reset the tier1 file to active_count=0 once on plugin startup.
     * If a prior plugin process crashed mid-flight, its stale file
     * could be permanently reporting an inflated count to the watchdog
     * (the spec's intra-process counter has no PID-aliveness check on
     * the reader side, by design — keeps the reader trivial). Calling
     * this from reloadConfig closes the gap: the FIRST reload after
     * startup wipes the stale value.
     *
     * Idempotent — only writes the first time per process via the
     * `_tier1ActiveFileInitialized` flag.
     */
    private _clearTier1ActiveFileOnce;
    /** Map a capped role to the `tier` value the Python reader counts. */
    private _roleToDiskTierField;
    /** Path to today's tier-usage JSONL for this bot. */
    private _tierUsageLogPath;
    /**
     * Append one tier-usage record to today's JSONL for a transition into
     * `role`. Best-effort + no-throw; first failure logs a warn so the
     * cap's disk counter silently failing to record surfaces.
     */
    private _appendTierUsageRecord;
    private _tierUsageWarned;
    /**
     * Seed _tier1CallsToday / _maxCallsToday from today's on-disk JSONL at
     * boot so a plugin restart doesn't reset the daily cap to zero. Counts
     * records by their `tier` field (tier1 → power, max → max), matching the
     * server-side reader. Tolerates a missing/unreadable file (count stays 0)
     * and a torn final line (per-line parse, skip on error). No-throw.
     */
    private _seedRoleCountersFromDisk;
    /**
     * Record cost incurred by a turn in the given session. Called by
     * TurnObserver after each agent_end completes. Bounded memory: old
     * entries outside the window are pruned on each call.
     */
    recordTurnCost(sessionKey: string, costUsd: number, tsMs?: number): void;
    /**
     * Check whether the session's rolling-window spend has exceeded the
     * runaway-rate cap. Returns `{tripped: true, ...}` when it has, with
     * the suggested Signal severity (escalates to CRITICAL after N trips
     * in 24h).
     *
     * Pure function over the in-memory cost history — no I/O.
     */
    checkRunawayRate(sessionKey: string, tsMs?: number): {
        tripped: boolean;
        totalUsd: number;
        severity?: "warning" | "critical";
        tripsToday?: number;
    };
    /**
     * True if the session has been tripped by runaway-rate at any point
     * during its lifetime. Sticky — once tripped, stays tripped until
     * the session ends (clearSession).
     */
    isRunawayTripped(sessionKey: string): boolean;
    /**
     * Returns true if the bot is currently forced to tier3 by either:
     *   - the per-session runaway-rate hard cap (sticky once tripped), OR
     *   - today's daily spend-cap flag (downgrade-tier action).
     *
     * Exposed for callers that need to know about the forced state outside
     * of the resolveModelOverride hot path — chiefly the cascade telemetry
     * span, which records `tier_chosen_by="spend_cap"` when forced. Without
     * this, the audit-layer Labeler (Signal #1 — UI-chip override) sees
     * every span as `tier_chosen_by="classifier"` even when a cap is in
     * play, defeating ground-truth attribution.
     */
    isSpendCapForced(sessionKey: string): boolean;
    /**
     * Returns model override for this session, or null if no override needed.
     * This is called from the before_model_resolve hook handler.
     *
     * Returns null (no override) when:
     * - routing is disabled in config
     * - session type is 'productive' or 'ambiguous'
     * - session type is unknown
     * - target tier has no models configured
     *
     * Hard-spend-cap enforcement: if a downgrade-tier cap flag is active for
     * this bot today, ALL sessions are forced to tier3 regardless of type.
     */
    resolveModelOverride(sessionKey: string): string | null;
    /**
     * Internal — same logic as resolveModelOverride but ALSO returns the
     * ROLE we picked (or null when we fell through to bot-default). Split
     * out so the public hot-path function can write the power-role counter
     * as a side-effect without duplicating branch logic.
     */
    private _resolveModelAndTier;
    /**
     * Force the `fast` role for this turn regardless of session
     * classification. Used by the `evo` keyword surface: every dispatched
     * evo turn is either a stay-silent ack or a verbatim echo of
     * dispatcher-supplied content — neither justifies the bot's default
     * (Sonnet/Opus) model.
     *
     * Returns the first model in the `fast` rung, or null when:
     *   - routing is disabled in config
     *   - the `fast` role is unconfigured (legacy pod without tiers.json)
     */
    resolveFastRoleOverride(): string | null;
    /**
     * Back-compat alias for resolveFastRoleOverride — kept for the
     * `evo`-keyword caller in TurnObserver until it is renamed.
     */
    resolveTier3Override(): string | null;
    /**
     * Returns authProfileOverride for this session, or null if no override.
     * Only returns a value if account routing is enabled AND session type
     * matches a configured account tier AND at least one profile in that tier
     * has a valid key in auth-profiles.json.
     *
     * Profile fallback within a tier: profiles[] is tried in order; the first
     * profile that has a non-empty key/token in auth-profiles.json is returned.
     * If NO profile in the matching tier has a valid key, returns null — causing
     * openclaw to use its default auth profile (the API key) rather than failing
     * the model call and cascading to the next model in the fallback list.
     */
    resolveAuthProfileOverride(sessionKey: string): string | null;
    /**
     * Re-reads config in place.
     *
     * Priority:
     *   1. Explicit tiersJsonPath arg (test/programmatic override)
     *   2. ~/.openclaw/evolve-tiers.json — admin UI canonical location
     *   3. {sharedDir}/{botId}/tiers.json — legacy / hand-rolled fallback
     *   4. network.json models.*         — pod-wide fallback
     *   5. Keep existing config on any failure — always fail open
     */
    reloadConfig(networkPath: string, tiersJsonPath?: string, sharedDir?: string, botId?: string): void;
    /**
     * Get routing status for diagnostics/UI.
     */
    getRoutingStatus(): {
        enabled: boolean;
        accountRoutingEnabled: boolean;
        sessions: Record<string, string>;
        userTiers: Record<string, string>;
    };
}
//# sourceMappingURL=ModelRouter.d.ts.map