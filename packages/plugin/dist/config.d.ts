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
    enableLLMExtraction: boolean;
    enableTaskExtraction: boolean;
    classifierHints: ClassifierHints;
    applicationPatterns: ApplicationPattern[];
    summarizerMinTurns: number;
    classifierKeywordConfidenceFloor: number;
    costLedgerEnabled: boolean;
    /** Per-bot integration tier (off/monitor/manage/full). */
    tier: EvolveTier;
    /** Resolved capability flags for the active tier. */
    capabilities: TierCapabilities;
}
export declare function resolveConfig(pluginConfig: Record<string, unknown>, _gatewayConfig: OpenClawConfig): EvolveConfig;
//# sourceMappingURL=config.d.ts.map