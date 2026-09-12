/**
 * StruggleDetector
 *
 * Pure-function turn-outcome analysis. Given a finished turn's messages and
 * metadata, produces a struggle score in [0, 1] plus the per-feature
 * contributions that explain it.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § 2.1.
 *
 * Design constraints (per the spec):
 *   - No I/O. All inputs are passed in. Tests are pure-function unit tests.
 *   - Single responsibility: extract signal. Cascade controller decides what
 *     to do with it.
 *   - Cheap. Pure regex + counting. Single-digit-microsecond budget per turn.
 *   - Configurable weights. Initial values are educated guesses; tuned later
 *     via the Audit Layer (spec § 2.3).
 *   - No reliance on tool-call hooks (OC doesn't fire them — see § 6). All
 *     signal comes from the agent_end `messages[]` payload, which carries
 *     Anthropic-style content blocks including tool_use / tool_result.
 *
 * Phase 1 scope: detect struggle, do not act on it. Cascade controller
 * (Phase 2) consumes the signal; this module just produces it.
 */
/**
 * Per-feature weights. Sum need not equal 1; the detector normalizes
 * each feature into [0, 1] and combines them as a weighted sum, then clamps.
 * Default weights live in DEFAULT_STRUGGLE_WEIGHTS below.
 */
export interface StruggleWeights {
    tool_error_count: number;
    tool_retry_count: number;
    restart_markers: number;
    clarification_loops: number;
    tokens_per_progress: number;
    /**
     * Total tool calls in the turn (Anthropic-style ``tool_use``,
     * OpenAI-style top-level ``tool_calls[]`` array, AND OpenAI legacy
     * ``function_call`` wrapper — all three shapes summed). Added
     * 2026-06-06 (PR sibling to #2296/#2300) to give the detector a
     * payload-shape-tolerant fallback signal: even if ``is_error`` doesn't
     * surface in OC's agent_end payload, the COUNT of tool blocks does.
     *
     * Saturation = 8 (see SATURATION below). A turn making 8+ tool calls
     * is genuinely expensive — would have caught the team-bot-a 2026-06-03
     * runaway turn (8.8M cache_write tokens, all other features=0).
     */
    tool_count_per_turn: number;
}
export interface StruggleSignal {
    /**
     * Combined score in [0, 1]. **May be `null`** when the input payload
     * doesn't conform to the OC `agent_end` contract enough to measure
     * struggle — e.g., messages is missing/non-array, or the turn was
     * marked failed but produced no inspectable content. Per spec § 2.7,
     * downstream consumers MUST distinguish `null` ("couldn't measure")
     * from `0` ("measured: no struggle"). Treating them the same was the
     * silent-failure pattern the spec exists to prevent.
     */
    score: number | null;
    /**
     * Per-feature normalized contributions (already saturated to [0, 1] and
     * multiplied by their weight). Useful for explaining why a score is what
     * it is in audit/diagnostic surfaces. Empty object when payload drift
     * prevented measurement.
     */
    features: Record<keyof StruggleWeights, number>;
    /** Raw feature values before normalization (for telemetry / debug). */
    raw: Record<keyof StruggleWeights, number>;
    /**
     * When `score === null`, names the reason. Used by the host to emit a
     * `cascade_payload_unexpected` Signal (spec § 2.7) and by the audit
     * layer to bucket sessions by drift type. `null` when score is a real
     * number.
     */
    payload_drift?: "no_messages" | "messages_not_array" | "empty_on_failure" | null;
}
export interface ComputeStruggleInput {
    /**
     * The `messages` array from OC's agent_end event payload. Each element is
     * an Anthropic-style message ({role, content}) where content is either a
     * string or an array of content blocks ({type, ...}).
     *
     * Tool calls appear as content blocks with `type: "tool_use"`; tool
     * results as `type: "tool_result"` (optionally with `is_error: true`).
     */
    messages: unknown[];
    /** Wall-clock duration of the turn in milliseconds (from agent_end ctx). */
    durationMs?: number;
    /**
     * Whether OC marked the turn `success: true`. Counts as a weak negative
     * signal — a failed turn is a struggle signal regardless of message
     * content.
     */
    success?: boolean;
    /** Override the default weights (Phase 4 audit may push tuned values). */
    weights?: Partial<StruggleWeights>;
}
export declare const DEFAULT_STRUGGLE_WEIGHTS: StruggleWeights;
/**
 * Count tool_result blocks where `is_error: true`. Anthropic API marks
 * failed tool calls this way; OC propagates the flag through.
 */
export declare function countToolErrors(messages: unknown[]): number;
/**
 * Count tool_use blocks per tool name; return the sum of (n - 1) for any
 * tool used more than once in the same turn. "Retry" here = the model
 * called the same tool multiple times within a single agent_end span,
 * which strongly correlates with thrashing.
 *
 * Two same-name calls = 1 retry. Three same-name calls = 2 retries. We
 * don't try to distinguish "intentional second call with different args"
 * from "retry after failure" at this level — the audit layer can refine
 * later if false-positive rate is high.
 */
export declare function countToolRetries(messages: unknown[]): number;
/**
 * Count restart/backtrack markers in assistant text. Same phrase in
 * multiple text blocks counts multiple times (i.e. we don't dedup) —
 * repetition is itself signal.
 */
export declare function countRestartMarkers(messages: unknown[]): number;
/**
 * Count clarification/correction markers in user text. Only the *last*
 * user message counts — clarification at turn N is signal that turn N-1
 * struggled, and we don't want to attribute it to turn N as well.
 *
 * NB: this is "this turn's last user message," not the user message
 * across the whole session. agent_end's messages array typically contains
 * only the current turn's exchange.
 */
export declare function countClarificationLoops(messages: unknown[]): number;
/**
 * Compute the tokens-per-progress proxy. Spec § 2.1 lists this as
 * "tokens_per_progress" but we don't have per-call token data in v1 —
 * use ms-per-successful-tool-call as a proxy.
 *
 *   raw = durationMs / max(successful_tool_calls, 1)
 *
 * A turn with no tool calls but long duration *is* progress (the model
 * generated a response). We only mark it struggle-shaped when:
 *   - durationMs is well above baseline, AND
 *   - the turn made tool calls but few of them succeeded
 *
 * If no tool calls were attempted, the feature returns 0 — pure text
 * generation isn't measured by this signal.
 */
export declare function tokensPerProgressRatio(messages: unknown[], durationMs?: number): number;
/**
 * Total tool calls in the turn — payload-shape-tolerant.
 *
 * Counts THREE shapes per assistant message:
 *   1. Anthropic-style: ``content[]`` blocks with ``type === "tool_use"``
 *   2. OpenAI-style:    top-level ``message.tool_calls[]`` array
 *   3. OpenAI legacy:   top-level ``message.function_call`` object
 *
 * The other tool-related detectors (``countToolErrors`` /
 * ``countToolRetries``) walk only shape #1. Live-pod analysis after PR
 * #2296 (struggle-payload sampler) will tell us which shape OC actually
 * sends in agent_end, but until that data lands this extractor is
 * permissive: any of the three shapes contributes to the count.
 *
 * Healthy single-call turns (e.g., a subagent doing one tool call)
 * return 1; a runaway multi-tool turn returns the full sum. Saturation
 * lives in ``SATURATION.tool_count_per_turn`` (= 8).
 */
export declare function countToolCalls(messages: unknown[]): number;
/**
 * Compute the struggle signal for a single turn.
 *
 * Pure function. No I/O, no logging, no side effects. Catches errors in
 * feature extraction silently and treats them as 0 — a misshapen messages
 * payload shouldn't blow up the turn loop. (The plugin always calls this
 * from a `try { ... } catch` in the host, but defensive zero-out is cheap
 * insurance.)
 */
export declare function computeStruggle(input: ComputeStruggleInput): StruggleSignal;
//# sourceMappingURL=StruggleDetector.d.ts.map