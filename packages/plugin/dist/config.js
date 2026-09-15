/**
 * Evolve plugin configuration resolution.
 */
export const TIERS = {
    off: { observer: false, injectPodConduct: false, injectKeywords: false, modelRouting: false, deferTool: false, recordApplicationTool: false },
    monitor: { observer: true, injectPodConduct: false, injectKeywords: false, modelRouting: false, deferTool: false, recordApplicationTool: false },
    manage: { observer: true, injectPodConduct: true, injectKeywords: false, modelRouting: true, deferTool: false, recordApplicationTool: true },
    full: { observer: true, injectPodConduct: true, injectKeywords: true, modelRouting: true, deferTool: true, recordApplicationTool: true },
};
/**
 * Resolve the Layer-2 arming flag from the plugin config, fail-SAFE and LOUD.
 *
 * Accepts the nested ``layer2: { enforce: … }`` shape (canonical) or the flat
 * ``layer2Enforce: …`` for hand edits. ONLY the exact boolean ``true`` arms
 * enforcement — any other present value (a string ``"true"``, ``1``, ``"yes"``,
 * ``null``, an object) stays OBSERVE-ONLY so a typo can never silently arm a
 * below-LLM blocker. A present-but-non-boolean value additionally returns a
 * ``warning`` so the caller can log it prominently (audit finding: a
 * ``"true"`` string that silently did nothing is worse than a loud refusal).
 */
export function resolveLayer2Enforce(pluginConfig) {
    const nested = pluginConfig.layer2?.enforce;
    const flat = pluginConfig.layer2Enforce;
    // Prefer the canonical nested key when present; fall back to the flat key.
    const present = nested !== undefined ? nested : flat;
    return resolveStrictBooleanArm(present, (value) => `Evolve Layer-2 gate: layer2.enforce is set to a NON-BOOLEAN value ` +
        `(${JSON.stringify(value)}); refusing to arm — the gate stays OBSERVE-ONLY ` +
        `(fail-safe). Set it to the boolean true (not "true" / 1 / "yes") to arm ` +
        `below-LLM enforcement.`);
}
/**
 * The shared strict-boolean arming contract (layer2Enforce,
 * execFailureAbsorb, and any future behavior-arming flag): absent/false →
 * not armed; the exact boolean ``true`` → armed; any OTHER present value →
 * not armed PLUS a loud warning built by the caller — a ``"true"`` string
 * that silently did nothing is worse than a loud refusal.
 */
export function resolveStrictBooleanArm(present, buildWarning) {
    if (present === undefined)
        return { armed: false, warning: null }; // absent → observe
    if (present === true)
        return { armed: true, warning: null };
    if (present === false)
        return { armed: false, warning: null };
    // PRESENT but not a strict boolean → stay observe-only, but LOUD.
    return { armed: false, warning: buildWarning(present) };
}
/** Exec-failure absorber arming (design-exec-failure-hygiene-2026-08-31 A1).
 *  Flat key only; same strict-boolean discipline as resolveLayer2Enforce. */
export function resolveExecFailureAbsorb(pluginConfig) {
    return resolveStrictBooleanArm(pluginConfig.execFailureAbsorb, (value) => `Evolve exec-failure absorber: execFailureAbsorb is set to a ` +
        `NON-BOOLEAN value (${JSON.stringify(value)}); refusing to arm — ` +
        `staying OBSERVE-ONLY (fail-safe). Set it to the boolean true ` +
        `(not "true" / 1 / "yes") to arm absorption.`);
}
export function resolveConfig(pluginConfig, _gatewayConfig) {
    // Role normalization. openclaw.json is hand-edited / wizard-generated and
    // case slippage is realistic (`"Primary"` instead of `"primary"`). Cast
    // alone doesn't validate at runtime — a typoed role would silently fall
    // through the `=== "primary"` checks in index.ts and session_surface.py,
    // dropping the primary-bot tools and scaffold without any operator-
    // visible signal. Lowercase + validate against the known literals.
    const roleRaw = (pluginConfig.role ?? "member").toLowerCase();
    const role = roleRaw === "primary" ? "primary" : "member";
    // Tier resolution. Default `full` preserves pre-tier behavior for any bot
    // whose openclaw.json has no `tier` key. Unknown values fall back to `full`
    // rather than `off` — fail-safe (bot stays managed) over fail-quiet (bot
    // silently drops out of Evolve).
    const tierRaw = pluginConfig.tier ?? "full";
    const tier = Object.keys(TIERS).includes(tierRaw)
        ? tierRaw
        : "full";
    return {
        botId: pluginConfig.botId ?? "unknown",
        role,
        networkId: pluginConfig.networkId ?? "default",
        sharedDir: pluginConfig.sharedDir ?? "/Users/Shared/evolve",
        // Left undefined when absent — resolveAnalyzerDir then falls back to the
        // legacy dirname(sharedDir)/evolve-repo derivation for back-compat.
        repoRoot: pluginConfig.repoRoot || undefined,
        classifierModel: pluginConfig.classifierModel ??
            "anthropic/claude-haiku-4-5",
        tierClassification: pluginConfig.tierClassification ??
            "session",
        dashboardEnabled: pluginConfig.dashboardEnabled ?? role === "primary",
        enableLLMSummarization: pluginConfig.enableLLMSummarization ?? true,
        classifierHints: {
            productive_extra: pluginConfig.classifierHints?.productive_extra ?? [],
            maintenance_extra: pluginConfig.classifierHints?.maintenance_extra ?? [],
        },
        applicationPatterns: pluginConfig.applicationPatterns ?? [],
        summarizerMinTurns: typeof pluginConfig.summarizerMinTurns === "number"
            ? pluginConfig.summarizerMinTurns
            : 2,
        classifierKeywordConfidenceFloor: typeof pluginConfig.classifierKeywordConfidenceFloor === "number"
            ? pluginConfig.classifierKeywordConfidenceFloor
            : 0.80,
        costLedgerEnabled: pluginConfig.costLedgerEnabled ?? true,
        // Phase 0 ships dark: only the exact boolean true enables (mirrors the
        // layer2Enforce posture — a typo must not silently start writing files).
        prefixHashLedgerEnabled: pluginConfig.prefixHashLedgerEnabled === true,
        // Exec-failure absorber arming. Same exact-boolean discipline as
        // layer2Enforce (shared resolveStrictBooleanArm contract).
        ...(() => {
            const efa = resolveExecFailureAbsorb(pluginConfig);
            return { execFailureAbsorb: efa.armed, execFailureAbsorbWarning: efa.warning };
        })(),
        tier,
        capabilities: TIERS[tier],
        // Layer-2 gate arming. DEFAULT false (observe-only). Only the exact
        // boolean ``true`` arms enforcement — any other value (missing, null,
        // string, 0) stays observe-only, so a typo can never silently arm a
        // below-LLM blocker. A present-but-non-boolean value stays observe-only
        // AND carries a loud warning (surfaced by the gate's registration log).
        // Accepts the nested ``layer2: { enforce: true }`` shape (canonical) or a
        // flat ``layer2Enforce: true`` for hand edits.
        ...(() => {
            const l2 = resolveLayer2Enforce(pluginConfig);
            return { layer2Enforce: l2.armed, layer2EnforceWarning: l2.warning };
        })(),
    };
}
//# sourceMappingURL=config.js.map