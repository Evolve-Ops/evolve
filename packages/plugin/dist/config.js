/**
 * Evolve plugin configuration resolution.
 */
export const TIERS = {
    off: { observer: false, injectPodConduct: false, injectKeywords: false, modelRouting: false, deferTool: false, recordApplicationTool: false },
    monitor: { observer: true, injectPodConduct: false, injectKeywords: false, modelRouting: false, deferTool: false, recordApplicationTool: false },
    manage: { observer: true, injectPodConduct: true, injectKeywords: false, modelRouting: true, deferTool: false, recordApplicationTool: true },
    full: { observer: true, injectPodConduct: true, injectKeywords: true, modelRouting: true, deferTool: true, recordApplicationTool: true },
};
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
        enableLLMExtraction: pluginConfig.enableLLMExtraction ?? true,
        enableTaskExtraction: pluginConfig.enableTaskExtraction ?? true,
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
        tier,
        capabilities: TIERS[tier],
    };
}
//# sourceMappingURL=config.js.map