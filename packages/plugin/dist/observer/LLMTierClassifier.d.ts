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
import { type SessionClassResult } from "./TierClassifier.js";
export declare class LLMTierClassifier {
    private config;
    private logger;
    private api;
    constructor(config: EvolveConfig, logger: PluginLogger, api: any);
    classify(firstUserMessage: string, hints?: {
        productive_extra?: string[];
        maintenance_extra?: string[];
    }): Promise<SessionClassResult>;
}
//# sourceMappingURL=LLMTierClassifier.d.ts.map