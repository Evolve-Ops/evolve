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

export const TIERS: Record<EvolveTier, TierCapabilities> = {
  off:     { observer: false, injectPodConduct: false, injectKeywords: false, modelRouting: false, deferTool: false, recordApplicationTool: false },
  monitor: { observer: true,  injectPodConduct: false, injectKeywords: false, modelRouting: false, deferTool: false, recordApplicationTool: false },
  manage:  { observer: true,  injectPodConduct: true,  injectKeywords: false, modelRouting: true,  deferTool: false, recordApplicationTool: true  },
  full:    { observer: true,  injectPodConduct: true,  injectKeywords: true,  modelRouting: true,  deferTool: true,  recordApplicationTool: true  },
};

export interface ClassifierHints {
  // Additional keywords that signal productive sessions for this deployment.
  // E.g. project names, product names, team member names, domain vocabulary.
  productive_extra: string[];
  // Additional keywords that signal maintenance sessions for this deployment.
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
  // Absolute path to the deploy checkout (the read-only repo daemons load
  // from). Used to locate the packages/analyzer scripts the plugin spawns
  // (session_surface.py, task_extractor.py). Injected by deploy from
  // platform_profile.deploy_checkout_default — /Users/Shared/evolve-repo on
  // macOS, /var/lib/evolve/repo on Linux. Optional for back-compat: bots
  // whose openclaw.json predates this key fall back to the legacy
  // dirname(sharedDir)/evolve-repo derivation (see resolveAnalyzerDir).
  repoRoot?: string;
  classifierModel: string;
  defaultModel?: string;            // fallback model name for annotations
  tierClassification: "session" | "turn";
  dashboardEnabled: boolean;
  enableLLMSummarization: boolean;  // use LLM to extract session outcome (tier3)
  enableLLMExtraction: boolean;     // used by task_extractor.py (passed via network.json)
  enableTaskExtraction: boolean;    // run task_extractor at session end
  // Deployment-specific classifier keywords (set in network.json classifierHints)
  classifierHints: ClassifierHints;
  // Deployment-specific application detection patterns (set in network.json applicationPatterns)
  applicationPatterns: ApplicationPattern[];

  // ── Budget Hawk v2 — cost hygiene tunables ────────────────────────────────
  // Skip the per-session LLM outcome extraction below this turn count.
  // Sessions with <minTurns turns almost never justify the extra tier3 call;
  // inferOutcome() keyword fallback is used instead. Default 2.
  summarizerMinTurns: number;
  // Keyword classifier confidence above which we skip the LLM tier-classifier
  // entirely. Bumped from the prior 0.75 hard-code; 0.80 keeps keyword-confident
  // cases out of the LLM path without hurting recall on truly ambiguous inputs.
  classifierKeywordConfidenceFloor: number;
  // Emit `type: "cost_event"` records into the annotations JSONL for every
  // llm_output hook. Cheap (one append per LLM call), but disable-able if a
  // gateway has IO pressure. Default true.
  costLedgerEnabled: boolean;

  /** Per-bot integration tier (off/monitor/manage/full). */
  tier: EvolveTier;
  /** Resolved capability flags for the active tier. */
  capabilities: TierCapabilities;
}

export function resolveConfig(
  pluginConfig: Record<string, unknown>,
  _gatewayConfig: OpenClawConfig
): EvolveConfig {
  // Role normalization. openclaw.json is hand-edited / wizard-generated and
  // case slippage is realistic (`"Primary"` instead of `"primary"`). Cast
  // alone doesn't validate at runtime — a typoed role would silently fall
  // through the `=== "primary"` checks in index.ts and session_surface.py,
  // dropping the primary-bot tools and scaffold without any operator-
  // visible signal. Lowercase + validate against the known literals.
  const roleRaw = ((pluginConfig.role as string) ?? "member").toLowerCase();
  const role: EvolveConfig["role"] = roleRaw === "primary" ? "primary" : "member";

  // Tier resolution. Default `full` preserves pre-tier behavior for any bot
  // whose openclaw.json has no `tier` key. Unknown values fall back to `full`
  // rather than `off` — fail-safe (bot stays managed) over fail-quiet (bot
  // silently drops out of Evolve).
  const tierRaw = (pluginConfig.tier as string) ?? "full";
  const tier: EvolveTier = (Object.keys(TIERS) as EvolveTier[]).includes(tierRaw as EvolveTier)
    ? (tierRaw as EvolveTier)
    : "full";

  return {
    botId: (pluginConfig.botId as string) ?? "unknown",
    role,
    networkId: (pluginConfig.networkId as string) ?? "default",
    sharedDir: (pluginConfig.sharedDir as string) ?? "/Users/Shared/evolve",
    // Left undefined when absent — resolveAnalyzerDir then falls back to the
    // legacy dirname(sharedDir)/evolve-repo derivation for back-compat.
    repoRoot: (pluginConfig.repoRoot as string) || undefined,
    classifierModel:
      (pluginConfig.classifierModel as string) ??
      "anthropic/claude-haiku-4-5",
    tierClassification:
      (pluginConfig.tierClassification as EvolveConfig["tierClassification"]) ??
      "session",
    dashboardEnabled:
      (pluginConfig.dashboardEnabled as boolean) ?? role === "primary",
    enableLLMSummarization: (pluginConfig.enableLLMSummarization as boolean) ?? true,
    enableLLMExtraction: (pluginConfig.enableLLMExtraction as boolean) ?? true,
    enableTaskExtraction: (pluginConfig.enableTaskExtraction as boolean) ?? true,
    classifierHints: {
      productive_extra: ((pluginConfig.classifierHints as any)?.productive_extra as string[]) ?? [],
      maintenance_extra: ((pluginConfig.classifierHints as any)?.maintenance_extra as string[]) ?? [],
    },
    applicationPatterns: (pluginConfig.applicationPatterns as ApplicationPattern[]) ?? [],
    summarizerMinTurns:
      typeof pluginConfig.summarizerMinTurns === "number"
        ? (pluginConfig.summarizerMinTurns as number)
        : 2,
    classifierKeywordConfidenceFloor:
      typeof pluginConfig.classifierKeywordConfidenceFloor === "number"
        ? (pluginConfig.classifierKeywordConfidenceFloor as number)
        : 0.80,
    costLedgerEnabled: (pluginConfig.costLedgerEnabled as boolean) ?? true,
    tier,
    capabilities: TIERS[tier],
  };
}
