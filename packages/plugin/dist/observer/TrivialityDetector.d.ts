/**
 * TrivialityDetector
 *
 * Pure-function sibling of StruggleDetector. Given a finished turn's
 * messages and metadata, produces a triviality score in [0, 1] plus
 * the per-feature contributions that explain it. The cascade controller
 * uses this as the *demote* signal for user-facing sessions — when the
 * work is clearly trivial AND struggle is absent, drop from tier2 to
 * tier3 for the rest of the session.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § 2.5.
 *
 * Design constraints (same as StruggleDetector):
 *   - No I/O. All inputs are passed in. Tests are pure-function unit tests.
 *   - Single responsibility: extract signal. Cascade controller decides what
 *     to do with it.
 *   - Cheap. Pure regex + counting. Single-digit-microsecond budget per turn.
 *   - Configurable weights. Initial values are educated guesses; Phase 4
 *     audit can propose tuning once data accumulates.
 *
 * Symmetry with StruggleDetector. The spec explicitly says triviality is
 * the *symmetric* signal — strugglehigh = work was hard; trivialityhigh =
 * work was trivial. Both can be zero on the same turn (turn was
 * medium-difficulty). They're independent measurements; the cascade
 * controller compares them.
 *
 * Asymmetric thresholds (per spec § 2.5):
 *   "It takes strong positive evidence to demote, but only weak struggle
 *   to not demote. Skewed conservative because the cost of a wrong
 *   demote (next turn on Haiku, mediocre answer to a real question) is
 *   more visible than the cost of staying on Sonnet (small extra spend)."
 * → CascadeController demotes only when:
 *     triviality.score > 0.7 (default) AND struggle.score < 0.1
 *   The 0.7 floor is what makes triviality "earn" a demote.
 *
 * Phase 2 deliverable; data-independent so it can be built before Phase 1
 * telemetry accumulates.
 *
 * Payload-drift contract (spec § 2.7): same as StruggleDetector. Returns
 * `score: null` when input shape is unusable; consumer distinguishes null
 * (couldn't measure) from 0 (measured: no triviality).
 */
/**
 * Per-feature weights. Sum need not equal 1; detector normalizes each
 * feature into [0, 1] and combines as weighted sum, then clamps.
 */
export interface TrivialityWeights {
    short_user_message: number;
    single_decisive_tool: number;
    short_assistant_response: number;
    no_struggle_markers: number;
    no_clarification: number;
    fast_completion: number;
}
export interface TrivialitySignal {
    /** Combined triviality score in [0, 1], or null on payload drift. */
    score: number | null;
    features: Record<keyof TrivialityWeights, number>;
    raw: Record<keyof TrivialityWeights, number>;
    /** Same drift-reason taxonomy as StruggleDetector (spec § 2.7). */
    payload_drift?: "no_messages" | "messages_not_array" | "empty_on_failure" | null;
}
export interface ComputeTrivialityInput {
    messages: unknown;
    durationMs?: number;
    success?: boolean;
    weights?: Partial<TrivialityWeights>;
}
export declare const DEFAULT_TRIVIALITY_WEIGHTS: TrivialityWeights;
/**
 * Count words in the LAST user message. Triviality looks at the most
 * recent user input; if it's a short question, the request itself was
 * trivial. Earlier user messages aren't counted — they may have been
 * substantive even if the current turn is a quick follow-up.
 *
 * Word count, not token count, because we don't have a tokenizer in the
 * plugin. Empirically: words ≈ tokens × 0.75 for English. The 50-word
 * threshold maps to ~67 tokens.
 */
export declare function lastUserMessageWordCount(messages: unknown[]): number;
/**
 * Count words in the LAST assistant message (text blocks only).
 * Triviality looks at the bot's response length — short answer to a
 * short question = the work was probably trivial.
 */
export declare function lastAssistantMessageWordCount(messages: unknown[]): number;
/**
 * Return 1 if the turn made exactly one tool call AND it succeeded
 * first try, else 0. "Decisive" = no retries, no errors. This is the
 * pattern of a confident answer ("look up X, use the result, respond").
 *
 * Zero tool calls is NOT triviality — could be a complex pure-reasoning
 * question. We need POSITIVE evidence of competence, not absence of
 * tool use.
 */
export declare function singleDecisiveToolUsed(messages: unknown[]): boolean;
/**
 * Count struggle markers in assistant text. Same patterns
 * StruggleDetector uses; triviality rewards their ABSENCE.
 */
export declare function countStruggleMarkersForTriviality(messages: unknown[]): number;
/**
 * Count clarification markers in last user message. Same patterns
 * StruggleDetector uses; triviality rewards their ABSENCE.
 */
export declare function countClarificationForTriviality(messages: unknown[]): number;
export declare function computeTriviality(input: ComputeTrivialityInput): TrivialitySignal;
//# sourceMappingURL=TrivialityDetector.d.ts.map