/**
 * LLMTierClassifier
 *
 * Uses a cheap LLM (Haiku by default) to classify session intent as
 * Tier 1 (objective work) or Tier 2 (maintenance/infrastructure work).
 *
 * Only called once per session (at the start), not on every turn.
 * Falls back to keyword classifier if the LLM call fails.
 *
 * PRODUCTIVE: moves the user's actual goals forward
 *   — research, writing, decisions, task completion, personal assistance
 *
 * Tier 2: maintains the bot system itself
 *   — config fixes, debugging, gateway restarts, permission errors
 *   — NOTE: Tier 2 is not "bad" — it signals something in the system
 *     made this session necessary. The root cause is what gets fixed.
 *
 * v0.3: LLM-backed, with keyword fallback
 */

import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import { classifyTierByKeywords, type SessionClassResult } from "./TierClassifier.js";
import { runPinnedSubagent } from "./subagentRun.js";

const CLASSIFIER_PROMPT = `You are classifying an AI assistant interaction.

Classify this session as TIER1 or TIER2:

PRODUCTIVE = The human is getting real work done or getting real help:
- Research, information lookup, writing, analysis
- Planning, decisions, brainstorming  
- Personal tasks (calendar, health, travel, family)
- Creative work (novel, design, projects)
- Business work (deployment-specific projects and team coordination)
- Building something new (code, documents, systems)

MAINTENANCE = The session is about fixing or maintaining the bot system itself:
- Config errors, permission issues, gateway problems
- Debugging bot behavior or scripts
- Fixing something that was broken by a prior change
- Long back-and-forth where the human runs commands and reports errors
- Infrastructure setup or troubleshooting

AMBIGUOUS = Genuinely unclear, or a mix of both.

First user message: {first_message}

Reply with exactly one word: PRODUCTIVE, MAINTENANCE, or AMBIGUOUS`;


export class LLMTierClassifier {
  private config: EvolveConfig;
  private logger: PluginLogger;
  private api: any;

  constructor(config: EvolveConfig, logger: PluginLogger, api: any) {
    this.config = config;
    this.logger = logger;
    this.api = api;
  }

  async classify(
    firstUserMessage: string,
    hints?: { productive_extra?: string[]; maintenance_extra?: string[] }
  ): Promise<SessionClassResult> {
    // Fast path: keyword classifier for obvious cases.
    // Threshold is config-driven (default 0.80 — bumped from the prior 0.75
    // hard-code based on Budget Hawk v2 forensics showing ~70% of classifier
    // LLM calls resolve to the keyword-confident tier anyway, wasting a Haiku
    // call per ambiguous-ish session).
    const keywordResult = classifyTierByKeywords(firstUserMessage, "", "", hints);
    const floor = this.config.classifierKeywordConfidenceFloor ?? 0.80;
    if (keywordResult.confidence > floor) {
      return keywordResult;
    }

    // LLM classification for ambiguous cases
    try {
      const prompt = CLASSIFIER_PROMPT.replace(
        "{first_message}",
        firstUserMessage.slice(0, 500) // cap to keep costs minimal
      );

      // runPinnedSubagent adapts to OC >=2026.7's override authorization:
      // pinned first, unpinned retry (loud, once) when the pin is denied.
      const result = await runPinnedSubagent(this.api, this.logger, {
        idempotencyKey: `evolve:tier-classifier:${Date.now()}`,
        message: prompt,
        model: this.config.classifierModel,
        maxTurns: 1,
      });

      const response = await this.api.runtime.subagent.waitForRun({
        runId: result.runId,
        timeoutMs: 10000,
      });

      const text = response?.lastMessage?.trim().toUpperCase() ?? "";

      if (text.includes("PRODUCTIVE")) {
        return { class: "productive", signals: ["llm-classified"], confidence: 0.80 };
      } else if (text.includes("MAINTENANCE")) {
        return { class: "maintenance", signals: ["llm-classified"], confidence: 0.80 };
      } else {
        return { class: "ambiguous", signals: ["llm-ambiguous"], confidence: 0.50 };
      }
    } catch (err) {
      // FAIL LOUD (2026-07-31 incident): a dead LLM classifier silently
      // degraded the whole fleet to keyword-only routing for a day. Keep
      // the keyword fallback (routing must not die with the classifier)
      // but log at error so log-scanning monitors and operators see the
      // degradation, and record what the fallback actually chose.
      const loud = (this.logger as { error?: (m: string) => void }).error ?? this.logger.warn;
      loud.call(
        this.logger,
        `Evolve: LLM tier classification failed, using keyword fallback ` +
        `(class=${keywordResult.class}, confidence=${keywordResult.confidence}): ${err}`,
      );
      return keywordResult;
    }
  }
}
