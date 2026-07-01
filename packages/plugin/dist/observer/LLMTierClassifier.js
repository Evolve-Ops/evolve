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
import { classifyTierByKeywords } from "./TierClassifier.js";
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
    config;
    logger;
    api;
    constructor(config, logger, api) {
        this.config = config;
        this.logger = logger;
        this.api = api;
    }
    async classify(firstUserMessage, hints) {
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
            const prompt = CLASSIFIER_PROMPT.replace("{first_message}", firstUserMessage.slice(0, 500) // cap to keep costs minimal
            );
            const result = await this.api.runtime.subagent.run({
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
            }
            else if (text.includes("MAINTENANCE")) {
                return { class: "maintenance", signals: ["llm-classified"], confidence: 0.80 };
            }
            else {
                return { class: "ambiguous", signals: ["llm-ambiguous"], confidence: 0.50 };
            }
        }
        catch (err) {
            this.logger.warn(`Evolve: LLM tier classification failed, using keyword fallback: ${err}`);
            return keywordResult;
        }
    }
}
//# sourceMappingURL=LLMTierClassifier.js.map