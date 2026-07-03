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
 * Spec: docs/spec-session-struggle-aggregator-2026-06-07.md (to be written).
 */
import type { EvolveConfig } from "../config.js";
import type { PluginLogger } from "openclaw/plugin-sdk/types";
import type { SessionStruggleSignal } from "./SessionStruggleAggregator.js";
export type JudgeVerdict = "STRUGGLING" | "OK" | "AMBIGUOUS";
export interface JudgeInput {
    /** Bot identity — passed to the prompt as context. */
    botId: string;
    /**
     * Conversation snippets — the last N turns as plain text. Built by
     * the caller from sessionTurns; we don't reach back to in-class state
     * to keep the judge pure-function-callable + easily testable.
     */
    conversationSnippet: string;
    /**
     * Which aggregate pre-threshold triggered this call. Recorded on the
     * span so the audit layer can distinguish "we asked because shell
     * errors" vs "we asked because the bot self-corrected" — feeds the
     * Phase 4 RSI calibration ("when shell-paste fires, judge agrees with
     * STRUGGLING N% of the time").
     */
    triggeredBy: "shell_paste" | "self_correction" | "velocity" | "multiple";
}
export interface JudgeDecision {
    verdict: JudgeVerdict;
    /** Short rationale from the LLM (single sentence or phrase). */
    reason: string;
    /** Wall-clock latency of the judge call (observation, for SLO tracking). */
    latency_ms: number;
    /** Which pre-threshold triggered this call (mirrors input.triggeredBy). */
    triggered_by: JudgeInput["triggeredBy"];
}
/**
 * Parse the judge's response. Robust to:
 *   - Multi-word verdict prefixes ("STRUGGLING:")
 *   - Whitespace / casing variations
 *   - Responses with NO rationale (just one word)
 *   - Garbage / multi-verdict responses (returns AMBIGUOUS)
 *
 * Pure function — exported for tests.
 */
export declare function _parseJudgeResponse(response: string | null | undefined): {
    verdict: JudgeVerdict;
    reason: string;
};
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
export declare function _buildConversationSnippet(turns: ReadonlyArray<{
    userMessage: string;
    assistantMessage: string;
}>): string;
export declare class SessionStruggleJudge {
    private readonly config;
    private readonly logger;
    private readonly api;
    constructor(config: EvolveConfig, logger: PluginLogger, api: unknown);
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
    judge(input: JudgeInput): Promise<JudgeDecision>;
}
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
export declare function shouldRunJudge(signal: SessionStruggleSignal): JudgeInput["triggeredBy"] | null;
//# sourceMappingURL=SessionStruggleJudge.d.ts.map