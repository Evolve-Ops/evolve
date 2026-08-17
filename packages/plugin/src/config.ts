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
  // (session_surface.py). Injected by deploy from
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
  // Context-observability Phase 0 (spec-context-observability-2026-07-30.md):
  // emit one prefix-hash record per before_prompt_build invocation to
  // {sharedDir}/{botId}/turns/prefix-hashes-YYYY-MM-DD.jsonl. DARK BY DEFAULT
  // — the spec ships Phase 0 behind a flag; enable per-bot to run the
  // root-cause confirming test. Default false.
  prefixHashLedgerEnabled: boolean;

  /** Per-bot integration tier (off/monitor/manage/full). */
  tier: EvolveTier;
  /** Resolved capability flags for the active tier. */
  capabilities: TierCapabilities;

  // ── Layer-2 per-role tool-loading gate (below-LLM enforcement) ────────────
  // The before_tool_call gate resolves the SPEAKER's role per tool call and,
  // for a tool in the built-in capability→tool table, blocks the call when the
  // speaker's role does not grant the required capability. Enforced below the
  // LLM, so a jailbreak / prompt-injection cannot invoke a tool the speaker's
  // role forbids.
  //
  // DEFAULT false = OBSERVE-ONLY: the gate computes the decision and records
  // every would-block (log + per-bot decision ledger) but always ALLOWS the
  // call, so an operator can see what enforcement WOULD do before arming it.
  // Set network.json → plugin config ``layer2.enforce: true`` to ARM real
  // blocking. Merging the code is non-arming; arming is a deliberate config
  // flip. Spec: docs/spec-user-roster-and-roles-2026-06-07.md §8 (Layer 2);
  // design: docs/design-layer2-tool-loading-filter-2026-07-16.md.
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
export function resolveLayer2Enforce(
  pluginConfig: Record<string, unknown>,
): { armed: boolean; warning: string | null } {
  const nested = (pluginConfig.layer2 as { enforce?: unknown } | undefined)?.enforce;
  const flat = pluginConfig.layer2Enforce as unknown;
  // Prefer the canonical nested key when present; fall back to the flat key.
  const present = nested !== undefined ? nested : flat;
  if (present === undefined) return { armed: false, warning: null }; // absent → observe
  if (present === true) return { armed: true, warning: null };
  if (present === false) return { armed: false, warning: null };
  // PRESENT but not a strict boolean → stay observe-only, but LOUD.
  return {
    armed: false,
    warning:
      `Evolve Layer-2 gate: layer2.enforce is set to a NON-BOOLEAN value ` +
      `(${JSON.stringify(present)}); refusing to arm — the gate stays OBSERVE-ONLY ` +
      `(fail-safe). Set it to the boolean true (not "true" / 1 / "yes") to arm ` +
      `below-LLM enforcement.`,
  };
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
    // Phase 0 ships dark: only the exact boolean true enables (mirrors the
    // layer2Enforce posture — a typo must not silently start writing files).
    prefixHashLedgerEnabled: pluginConfig.prefixHashLedgerEnabled === true,
    tier,
    capabilities: TIERS[tier],
    // Layer-2 gate arming. DEFAULT false (observe-only). Only the exact
    // boolean ``true`` arms enforcement — any other value (missing, null,
    // string, 0) stays observe-only, so a typo can never silently arm a
    // below-LLM blocker. A present-but-non-boolean value stays observe-only
    // AND carries a loud warning (surfaced by the gate's registration log).
    // Accepts the nested ``layer2: { enforce: true }`` shape (canonical) or a
    // flat ``layer2Enforce: true`` for hand edits.
    ...((): { layer2Enforce: boolean; layer2EnforceWarning: string | null } => {
      const l2 = resolveLayer2Enforce(pluginConfig);
      return { layer2Enforce: l2.armed, layer2EnforceWarning: l2.warning };
    })(),
  };
}
