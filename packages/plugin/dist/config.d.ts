/**
 * Evolve plugin configuration resolution.
 */
import type { OpenClawConfig } from "openclaw/plugin-sdk/types";
/**
 * Per-bot Evolve integration tier — controls how deeply Evolve hooks into
 * the bot's runtime. Set via `tier` in the plugin config block of openclaw.json.
 *
 *   off     — Evolve does not touch this bot. No hooks, no injections, no
 *             captures. Use for bots that should stay pure-OpenClaw but still
 *             show up in pod-level monitoring (gateway health, repo puller, etc.).
 *   monitor — Captures only. TurnObserver records cost/transcript/observation
 *             data so the bot appears in dashboards, but produces zero
 *             systemAppend and no plugin tools. The bot's behavior is unchanged.
 *   manage  — monitor + pod conduct injection (POD_CONDUCT.md) + model routing
 *             (cost-tier optimization). No keyword/recommendation injection,
 *             no defer tool. The user-facing surface is unchanged.
 *   full    — Everything: pod conduct, model routing, defer tool, evo keyword
 *             handler, recommendation injection. Default for backward-compat.
 */
export type EvolveTier = "off" | "monitor" | "manage" | "full";
export interface TierCapabilities {
    /** Construct TurnObserver and register capture-side hooks. */
    observer: boolean;
    /** session_start: emit pod conduct + pending tasks as systemAppend. */
    injectPodConduct: boolean;
    /** before_agent_run + before_model_resolve: keyword/recommendation injection. */
    injectKeywords: boolean;
    /** before_model_resolve: ModelRouter overrides per-turn model selection. */
    modelRouting: boolean;
    /** Register the `defer` plugin tool (Continuity Engine). */
    deferTool: boolean;
    /** Register the `record_application` plugin tool (manifest reflex). */
    recordApplicationTool: boolean;
}
export declare const TIERS: Record<EvolveTier, TierCapabilities>;
export interface ClassifierHints {
    productive_extra: string[];
    maintenance_extra: string[];
}
export interface ApplicationPattern {
    keywords: string[];
    tag: string;
}
export interface EvolveConfig {
    botId: string;
    role: "primary" | "member";
    networkId: string;
    sharedDir: string;
    repoRoot?: string;
    classifierModel: string;
    defaultModel?: string;
    tierClassification: "session" | "turn";
    dashboardEnabled: boolean;
    enableLLMSummarization: boolean;
    classifierHints: ClassifierHints;
    applicationPatterns: ApplicationPattern[];
    summarizerMinTurns: number;
    classifierKeywordConfidenceFloor: number;
    costLedgerEnabled: boolean;
    prefixHashLedgerEnabled: boolean;
    execFailureAbsorb: boolean;
    /** Non-null when ``execFailureAbsorb`` was present but not a strict
     *  boolean — carries the loud refusal the absorber logs at registration. */
    execFailureAbsorbWarning: string | null;
    /** Per-bot integration tier (off/monitor/manage/full). */
    tier: EvolveTier;
    /** Resolved capability flags for the active tier. */
    capabilities: TierCapabilities;
    layer2Enforce: boolean;
    /**
     * Non-null when the operator SET ``layer2.enforce`` (or the flat
     * ``layer2Enforce``) to a value that is PRESENT but not a strict boolean
     * (e.g. the string ``"true"``, ``1``, ``"yes"``). In that case ``layer2Enforce``
     * stays false (fail-safe: never silently arm a below-LLM blocker on a typo),
     * and this string carries a LOUD explanation the gate surfaces in its
     * registration log so the operator can see WHY enforcement did not arm.
     */
    layer2EnforceWarning: string | null;
}
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
export declare function resolveLayer2Enforce(pluginConfig: Record<string, unknown>): {
    armed: boolean;
    warning: string | null;
};
/**
 * The shared strict-boolean arming contract (layer2Enforce,
 * execFailureAbsorb, and any future behavior-arming flag): absent/false →
 * not armed; the exact boolean ``true`` → armed; any OTHER present value →
 * not armed PLUS a loud warning built by the caller — a ``"true"`` string
 * that silently did nothing is worse than a loud refusal.
 */
export declare function resolveStrictBooleanArm(present: unknown, buildWarning: (present: unknown) => string): {
    armed: boolean;
    warning: string | null;
};
/** Exec-failure absorber arming (design-exec-failure-hygiene-2026-08-31 A1).
 *  Flat key only; same strict-boolean discipline as resolveLayer2Enforce. */
export declare function resolveExecFailureAbsorb(pluginConfig: Record<string, unknown>): {
    armed: boolean;
    warning: string | null;
};
export declare function resolveConfig(pluginConfig: Record<string, unknown>, _gatewayConfig: OpenClawConfig): EvolveConfig;
//# sourceMappingURL=config.d.ts.map