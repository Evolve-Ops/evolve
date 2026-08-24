/**
 * SessionStruggleJudge
 *
 * LLM-as-judge for cross-turn struggle detection. The "narrow the catch"
 * companion to SessionStruggleAggregator (the wide net).
 *
 * Architecture (per the operator's 2026-06-07 design conversation):
 *   "Cast a wider net with cheap regex/pattern features, then use LLM
 *    to look through what the net caught to see if there are actual
 *    fish."
 *
 *   1. SessionStruggleAggregator runs every turn (cheap, microseconds).
 *      Tracks shell-error pastes, bot self-corrections, turn velocity.
 *
 *   2. PRE-thresholds (looser than the aggregator's elevation thresholds)
 *      gate whether to call the judge:
 *        - shell_error_paste_count >= 1
 *        - bot_self_correction_count >= 1
 *        - (turn_velocity_per_min > 0.8 AND turn_count >= 4)
 *
 *      ANY of these tripping means "the cheap net caught SOMETHING —
 *      have the LLM look closer."
 *
 *   3. Judge runs on the last few turns of conversation, returns
 *      STRUGGLING / OK / AMBIGUOUS. Verdict applies to the NEXT turn
 *      via cascade controller (treated as equivalent to PR 1's
 *      elevation when STRUGGLING).
 *
 * Why this design beats per-turn regex:
 *   - Catches semantic struggle that no regex pattern can match
 *     reliably ("Sounds like you are guessing" — bike-fix session
 *     2026-06-07 — slips every current pattern but a haiku call
 *     reading the last 4 turns would catch it)
 *   - Cost-bounded: most sessions never trip the pre-threshold so
 *     never call the LLM. The file-copy case (PR 1's canonical
 *     positive) WOULD call the judge — but only because the cheap
 *     signal already suspected struggle.
 *   - Latency-bounded: 2s hard timeout; abstain on timeout. Doesn't
 *     block the user's turn (fired async at agent_end; verdict
 *     applies to the NEXT turn's cascade decision, not this one).
 *
 * Cost projection:
 *   ~$0.0005 per call (longer context than pre-flight haiku — sends
 *   actual conversation snippets). Pre-threshold gate means most
 *   sessions skip → ~$0.05/day pod-wide.
 *
 * Spec: internal/spec-session-struggle-aggregator-2026-06-07.md (to be written).
 */
import { runPinnedSubagent } from "./subagentRun.js";
// ── Prompt + parser ─────────────────────────────────────────────────────────
/**
 * Judge prompt. Three-class output (STRUGGLING / OK / AMBIGUOUS) with
 * a short rationale. Kept concise to bound input tokens.
 *
 * {bot_id} = bot identity
 * {conversation} = last N turns as plain text
 *
 * The prompt asks for "STRUGGLING" or "OK" or "AMBIGUOUS" as the FIRST
 * word, followed by a colon + rationale on the same line. Parser pulls
 * the first word and the rest as reason. Designed so a minimal "STRUGGLING"
 * response (no rationale) still parses.
 */
const JUDGE_PROMPT_TEMPLATE = `You are evaluating whether a user is making progress with an AI assistant or struggling.

STRUGGLING means the user is visibly frustrated, the bot is repeatedly correcting itself, the user is pasting failed command output, the user is questioning the bot's accuracy, OR the same task is taking many turns without progress.

OK means the conversation is productive — the bot is being helpful, the user is engaged, the task is progressing turn-by-turn.

AMBIGUOUS means you genuinely cannot tell from the snippet.

Bot: {bot_id}

Recent conversation (most recent at bottom):
{conversation}

Reply with exactly ONE word (STRUGGLING, OK, or AMBIGUOUS), followed by a colon and one short phrase explaining why.`;
/**
 * Parse the judge's response. Robust to:
 *   - Multi-word verdict prefixes ("STRUGGLING:")
 *   - Whitespace / casing variations
 *   - Responses with NO rationale (just one word)
 *   - Garbage / multi-verdict responses (returns AMBIGUOUS)
 *
 * Pure function — exported for tests.
 */
export function _parseJudgeResponse(response) {
    if (!response) {
        return { verdict: "AMBIGUOUS", reason: "no_response" };
    }
    const text = response.trim();
    // Extract first word (capital letters), strip a trailing colon
    const firstWordMatch = text.match(/^\s*([A-Z]+)\b\s*[:.\-]?\s*(.*)$/i);
    if (!firstWordMatch) {
        return { verdict: "AMBIGUOUS", reason: "unparseable" };
    }
    const first = firstWordMatch[1].toUpperCase();
    const rest = firstWordMatch[2].trim();
    // Guard against responses that contain MULTIPLE verdicts ("STRUGGLING
    // or OK — depends"). If the rest of the text mentions another verdict,
    // treat as AMBIGUOUS to be safe.
    const hasAlt = (first !== "STRUGGLING" && /\bSTRUGGLING\b/.test(text)) ||
        (first !== "OK" && /\bOK\b/.test(text)) ||
        (first !== "AMBIGUOUS" && /\bAMBIGUOUS\b/.test(text));
    if (hasAlt) {
        return { verdict: "AMBIGUOUS", reason: "multiple_verdicts" };
    }
    if (first === "STRUGGLING") {
        return { verdict: "STRUGGLING", reason: rest || "no_reason_given" };
    }
    if (first === "OK") {
        return { verdict: "OK", reason: rest || "no_reason_given" };
    }
    if (first === "AMBIGUOUS") {
        return { verdict: "AMBIGUOUS", reason: rest || "no_reason_given" };
    }
    // Unrecognized first word
    return { verdict: "AMBIGUOUS", reason: "unrecognized_verdict" };
}
// ── Conversation snippet builder ────────────────────────────────────────────
const MAX_SNIPPET_CHARS = 2000;
const MAX_SNIPPET_TURNS = 5;
/**
 * Build a plain-text conversation snippet for the judge. Takes the
 * last N turns of (user, assistant) text, cap at MAX_SNIPPET_TURNS
 * for token control, plus a hard char cap so a single long turn
 * doesn't blow the prompt size budget.
 *
 * Each turn's text is truncated to a reasonable per-turn cap if
 * needed; turn boundaries are preserved.
 *
 * Pure function — exported for tests.
 */
export function _buildConversationSnippet(turns) {
    const recent = turns.slice(-MAX_SNIPPET_TURNS);
    const PER_TURN_CHAR_CAP = Math.floor(MAX_SNIPPET_CHARS / Math.max(recent.length, 1) / 2);
    const lines = [];
    for (const t of recent) {
        const u = (t.userMessage ?? "").trim().slice(0, PER_TURN_CHAR_CAP);
        const a = (t.assistantMessage ?? "").trim().slice(0, PER_TURN_CHAR_CAP);
        if (u)
            lines.push(`USER: ${u}`);
        if (a)
            lines.push(`BOT: ${a}`);
    }
    let snippet = lines.join("\n");
    if (snippet.length > MAX_SNIPPET_CHARS) {
        snippet = snippet.slice(-MAX_SNIPPET_CHARS); // tail-trim — keep recent over earliest
    }
    return snippet;
}
// ── Judge class ─────────────────────────────────────────────────────────────
const HAIKU_TIMEOUT_MS = 3000;
export class SessionStruggleJudge {
    config;
    logger;
    api;
    constructor(config, logger, api) {
        this.config = config;
        this.logger = logger;
        this.api = api;
    }
    /**
     * Run the judge. Returns AMBIGUOUS on any failure (timeout, parse
     * error, api unavailable) — never throws into the hot path. Caller
     * treats AMBIGUOUS as no-signal (no escalation, no demotion).
     *
     * Latency budget: hard 3s timeout via subagent.waitForRun. Worst
     * case the SPAN write is delayed by 3s; user-perceived latency is
     * unaffected (judge fires async via the caller, not on the turn
     * reply path).
     */
    async judge(input) {
        const start = Date.now();
        const finishAt = (verdict, reason) => ({
            verdict,
            reason,
            latency_ms: Date.now() - start,
            triggered_by: input.triggeredBy,
        });
        // Defensive api guard — same pattern as PreflightIntentRouter Phase 3.
        // Missing api or partial stub (common in tests) → skip cleanly.
        const api = this.api;
        if (typeof api?.runtime?.subagent?.run !== "function"
            || typeof api?.runtime?.subagent?.waitForRun !== "function") {
            return finishAt("AMBIGUOUS", "no_api");
        }
        if (!input.conversationSnippet || input.conversationSnippet.length === 0) {
            return finishAt("AMBIGUOUS", "empty_snippet");
        }
        const prompt = JUDGE_PROMPT_TEMPLATE
            .replace("{bot_id}", input.botId)
            .replace("{conversation}", input.conversationSnippet);
        try {
            // runPinnedSubagent adapts to OC >=2026.7's override authorization:
            // pinned first, unpinned retry (loud, once) when the pin is denied.
            const runResult = await runPinnedSubagent(api, this.logger, {
                idempotencyKey: `evolve:session-judge:${input.botId}:${Date.now()}`,
                message: prompt,
                model: this.config.classifierModel,
                maxTurns: 1,
            });
            const response = await api.runtime.subagent.waitForRun({
                runId: runResult.runId,
                timeoutMs: HAIKU_TIMEOUT_MS,
            });
            const parsed = _parseJudgeResponse(response?.lastMessage);
            return {
                verdict: parsed.verdict,
                reason: parsed.reason,
                latency_ms: Date.now() - start,
                triggered_by: input.triggeredBy,
            };
        }
        catch (err) {
            this.logger.debug(`Evolve: session-struggle judge failed: ${err}`);
            return finishAt("AMBIGUOUS", "judge_failed");
        }
    }
}
// ── Pre-threshold gating helper ─────────────────────────────────────────────
/**
 * Looser thresholds than SessionStruggleAggregator's elevation thresholds.
 * When ANY of these trip, the judge is called — the aggregator's "cheap
 * net" suspects struggle and we ask the LLM to confirm or refute.
 *
 * Calibration:
 *   - shell_paste_count: 1 (any shell error paste is suspicious)
 *   - self_correction_count: 1 (any bot self-correction is suspicious)
 *   - velocity > 0.8 with turn_count >= 4 (same as elevation — but
 *     elevation requires this ALONE to trip; pre-threshold treats it
 *     as ALSO suspicious)
 *
 * Returns the triggering field, or null when no threshold trips.
 * Caller uses the field as the `triggeredBy` value passed to the judge.
 */
export function shouldRunJudge(signal) {
    const reasons = [];
    if (signal.shell_error_paste_count >= 1)
        reasons.push("shell_paste");
    if (signal.bot_self_correction_count >= 1)
        reasons.push("self_correction");
    if (signal.turn_velocity_per_min !== null
        && signal.turn_velocity_per_min > 0.8
        && signal.turn_count >= 4) {
        reasons.push("velocity");
    }
    if (reasons.length === 0)
        return null;
    if (reasons.length > 1)
        return "multiple";
    return reasons[0];
}
//# sourceMappingURL=SessionStruggleJudge.js.map