/**
 * SessionSummarizer
 *
 * Generates a structured session-level summary when a session ends.
 * Written to the same annotation JSONL as turn annotations, as a
 * "session_summary" record.
 *
 * Design goals:
 *   - Capture outcome, complexity, capabilities invoked, promises made
 *   - Detect efficiency problems (high turn count for low complexity)
 *   - Use keyword heuristics for speed/cost; LLM for outcome extraction only
 *   - Keep cost minimal: one optional tier3 call per session, only last 2 turns
 *
 * Schema (type: "session_summary"):
 * {
 *   schema_version: 2,
 *   type: "session_summary",
 *   session_id: string,
 *   ts: string,
 *   bot_id: string,
 *   turn_count: number,
 *   session_class: "productive" | "maintenance" | "ambiguous",
 *   tier_confidence: number,
 *   outcome: string,              — what was accomplished (LLM-extracted or inferred)
 *   complexity: "low"|"medium"|"high",
 *   applications_invoked: string[],
 *   promises_made: string[],      — things the bot committed to do
 *   correction_count: number,
 *   efficiency_flag: boolean,     — true if turns >> expected for complexity
 *   total_input_tokens: number,
 *   total_output_tokens: number,
 *   recurring_request?: {label, requester, hour},  — conversation-only
 *                        evidence (design §7.1a). Present ONLY for
 *                        human-initiated sessions carrying a keyable
 *                        ask, from a requester who has not opted out.
 *                        Absent is the common case and means nothing
 *                        was observed — never "observed nothing".
 * }
 */
import { runPinnedSubagent } from "./subagentRun.js";
import { buildRecurringRequest, firstUserTurn, } from "./recurringRequest.js";
import { getPodTimezone } from "../util/podTimezone.js";
// Application tag patterns — keyword → application name.
// These are GENERIC defaults. Deployment-specific patterns
// (project names, team members, domain vocabulary) should be
// added via network.json applicationPatterns array:
// [{"keywords": ["acme", "project x"], "tag": "acme-project"}]
const APPLICATION_PATTERNS = [
    {
        keywords: ["protein", "fiber", "calories", "nutrition", "diet", "food log", "supplement"],
        tag: "health-nutrition",
    },
    {
        keywords: ["workout", "fitness", "exercise", "recovery", "sleep score", "steps"],
        tag: "health-fitness",
    },
    {
        keywords: ["calendar", "event", "appointment", "schedule", "meeting"],
        tag: "calendar",
    },
    {
        keywords: ["email", "inbox", "reply to", "message from", "vendor email"],
        tag: "email",
    },
    {
        keywords: ["travel", "itinerary", "flight", "hotel", "trip", "booking"],
        tag: "travel",
    },
    {
        keywords: ["home", "repair", "appliance", "contractor", "plumber", "electrician"],
        tag: "home-management",
    },
    {
        keywords: ["task", "project", "deadline", "milestone", "deliverable", "status update"],
        tag: "task-management",
    },
    {
        keywords: ["evolve", "openclaw config", "gateway", "plugin", "bot network"],
        tag: "evolve-system",
    },
    {
        keywords: ["novel", "chapter", "story", "creative writing", "draft"],
        tag: "creative-writing",
    },
    {
        keywords: ["slack", "team message", "weekly report", "channel update"],
        tag: "slack-comms",
    },
    {
        keywords: ["document", "report", "write up", "summary", "docx"],
        tag: "document-generation",
    },
];
// Phrases that signal the bot made a future commitment
const PROMISE_PATTERNS = [
    "i'll remember",
    "i'll write",
    "i'll note",
    "i'll track",
    "i'll check",
    "i'll follow up",
    "i'll monitor",
    "i'll keep an eye",
    "i'll let you know",
    "i'll update",
    "i'll add",
    "i'll save",
    "i'll log",
];
function detectApplications(turns, extraPatterns) {
    const combined = turns
        .map((t) => t.userMessage + " " + t.assistantMessage)
        .join(" ")
        .toLowerCase();
    const allPatterns = [...APPLICATION_PATTERNS, ...(extraPatterns ?? [])];
    const found = new Set();
    for (const { keywords, tag } of allPatterns) {
        if (keywords.some((kw) => combined.includes(kw))) {
            found.add(tag);
        }
    }
    return [...found];
}
function detectPromises(turns) {
    const promises = [];
    for (const turn of turns) {
        const asst = turn.assistantMessage.toLowerCase();
        for (const pattern of PROMISE_PATTERNS) {
            if (asst.includes(pattern)) {
                // Extract the sentence containing the promise (heuristic)
                const sentences = turn.assistantMessage.split(/[.!?]/);
                const relevant = sentences.find((s) => s.toLowerCase().includes(pattern));
                if (relevant) {
                    promises.push(relevant.trim().slice(0, 120));
                }
                break; // one promise per turn
            }
        }
    }
    return promises;
}
function dominantClass(turns) {
    const counts = { productive: 0, maintenance: 0, ambiguous: 0 };
    let totalConfidence = 0;
    for (const t of turns) {
        counts[t.session_class] = (counts[t.session_class] ?? 0) + 1;
        totalConfidence += t.class_confidence;
    }
    const dominant = Object.entries(counts).sort(([, a], [, b]) => b - a)[0];
    const confidence = turns.length > 0 ? totalConfidence / turns.length : 0.3;
    return { tier: dominant[0] ?? "ambiguous", confidence: Math.round(confidence * 100) / 100 };
}
function classifyComplexity(turnCount) {
    if (turnCount <= 2)
        return "low";
    if (turnCount <= 6)
        return "medium";
    return "high";
}
function isEfficiencyFlag(turnCount, complexity) {
    // Flag when turns significantly exceed expected for complexity
    if (complexity === "low" && turnCount > 3)
        return true;
    if (complexity === "medium" && turnCount > 9)
        return true;
    if (complexity === "high" && turnCount > 18)
        return true;
    return false;
}
// Simple outcome extraction from the last assistant message
// Used as fallback when LLM extraction is skipped
function inferOutcome(turns) {
    if (turns.length === 0)
        return "No turns recorded";
    const last = turns[turns.length - 1].assistantMessage;
    // Grab first 120 chars of last assistant message as outcome hint
    return last.slice(0, 120).replace(/\n/g, " ").trim() + (last.length > 120 ? "…" : "");
}
// Build a concise prompt for LLM outcome extraction
// Uses only the last 2 turns to minimize token cost
function buildOutcomePrompt(turns) {
    const last2 = turns.slice(-2);
    const ctx = last2
        .map((t) => `User: ${t.userMessage.slice(0, 300)}\nAssistant: ${t.assistantMessage.slice(0, 300)}`)
        .join("\n---\n");
    return `In one sentence (max 120 chars), describe what was accomplished in this AI assistant exchange:

${ctx}

Reply with only the one-sentence outcome, no preamble.`;
}
export class SessionSummarizer {
    config;
    logger;
    api;
    constructor(config, logger, api) {
        this.config = config;
        this.logger = logger;
        this.api = api;
    }
    async summarize(sessionId, turns, writeFn) {
        if (turns.length === 0)
            return;
        const { tier, confidence } = dominantClass(turns);
        const complexity = classifyComplexity(turns.length);
        const applications = detectApplications(turns, this.config.applicationPatterns);
        const promises = detectPromises(turns);
        const correctionCount = turns.filter((t) => t.correction_detected).length;
        const efficiencyFlag = isEfficiencyFlag(turns.length, complexity);
        const totalInput = turns.reduce((s, t) => s + t.input_tokens, 0);
        const totalOutput = turns.reduce((s, t) => s + t.output_tokens, 0);
        // LLM outcome extraction — optional, falls back to keyword inference.
        //
        // Gate on `summarizerMinTurns` (default 2): sub-minimal sessions (1 turn,
        // often "are you alive?" or a single keyword trigger) almost never justify
        // the tier3 call. The inferOutcome fallback is strictly cheaper and good
        // enough — the last assistant message IS the outcome for a 1-turn session.
        // This is the #1 waste source Budget Hawk v2 is designed to eliminate.
        let outcome = inferOutcome(turns);
        const minTurns = this.config.summarizerMinTurns ?? 2;
        if (this.config.enableLLMSummarization !== false && turns.length >= minTurns) {
            try {
                outcome = await this.extractOutcomeLLM(turns);
            }
            catch (err) {
                this.logger.warn(`Evolve: LLM outcome extraction failed, using fallback: ${err}`);
            }
        }
        else if (this.config.enableLLMSummarization !== false && turns.length < minTurns) {
            this.logger.debug(`Evolve: session ${sessionId.slice(0, 8)} below summarizerMinTurns (${turns.length} < ${minTurns}), skipping LLM outcome extraction`);
        }
        const summary = {
            schema_version: 2,
            type: "session_summary",
            session_id: sessionId,
            ts: new Date().toISOString(),
            bot_id: this.config.botId,
            turn_count: turns.length,
            // session_class is the canonical field name read by measure.py.
            // tier is kept for backward compatibility with older readers.
            session_class: tier,
            tier,
            tier_confidence: confidence,
            // True when the session resolved in a single turn — the user got what
            // they needed without any follow-up.  Used by measure.py for the
            // first_response_resolutions metric.
            first_response_resolution: turns.length === 1,
            outcome,
            complexity,
            applications_invoked: applications,
            promises_made: promises,
            correction_count: correctionCount,
            efficiency_flag: efficiencyFlag,
            total_input_tokens: totalInput,
            total_output_tokens: totalOutput,
        };
        // Conversation-only evidence (design §7.1a). Never let this fail the
        // summary: the summary is the load-bearing record, the recurrence row
        // is an optional extra. A throw here would cost the whole annotation.
        let recurring = null;
        try {
            // The ask and the asker MUST come from the same turn — see the
            // `requester` field note on TurnRecord.
            const asked = firstUserTurn(turns);
            recurring = buildRecurringRequest({
                requestText: asked?.userMessage,
                appTags: applications,
                requester: asked?.requester ?? null,
                at: new Date(summary.ts),
                timezone: getPodTimezone(this.config.sharedDir),
                sharedDir: this.config.sharedDir,
                botId: this.config.botId,
            });
        }
        catch (err) {
            this.logger.debug(`Evolve: recurring-request build skipped: ${err}`);
        }
        // Omit the key entirely when there is nothing to say — an absent field
        // reads as "not observed", which is the truth, whereas a null would
        // invite readers to treat it as a measured negative.
        if (recurring)
            summary.recurring_request = recurring;
        writeFn(summary);
        this.logger.info(`Evolve: session ${sessionId.slice(0, 8)} summarized — ${tier}, ${complexity}, ${turns.length} turns, outcome: ${outcome.slice(0, 60)}`);
    }
    async extractOutcomeLLM(turns) {
        const prompt = buildOutcomePrompt(turns);
        // runPinnedSubagent adapts to OC >=2026.7's override authorization:
        // pinned first, unpinned retry (loud, once) when the pin is denied.
        const result = await runPinnedSubagent(this.api, this.logger, {
            idempotencyKey: `evolve:session-summary:${Date.now()}`,
            message: prompt,
            model: this.config.classifierModel, // tier3 — cheap
            maxTurns: 1,
        });
        const response = await this.api.runtime.subagent.waitForRun({
            runId: result.runId,
            timeoutMs: 15000,
        });
        const text = (response?.lastMessage ?? "").trim();
        return text.slice(0, 150) || inferOutcome(turns);
    }
}
//# sourceMappingURL=SessionSummarizer.js.map