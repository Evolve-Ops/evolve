/**
 * StruggleDetector
 *
 * Pure-function turn-outcome analysis. Given a finished turn's messages and
 * metadata, produces a struggle score in [0, 1] plus the per-feature
 * contributions that explain it.
 *
 * Spec: docs/spec-tier-cascade-2026-05-26.md § 2.1.
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
// ── Defaults ──────────────────────────────────────────────────────────────────
export const DEFAULT_STRUGGLE_WEIGHTS = {
    tool_error_count: 0.25,
    tool_retry_count: 0.20,
    restart_markers: 0.10,
    clarification_loops: 0.15,
    tokens_per_progress: 0.15,
    // tool_count_per_turn — added 2026-06-06. Weight 0.15 lifts the total
    // possible score from 0.85 → 1.00 deliberately: live-pod analysis
    // showed tier2_struggle_threshold=0.65 was unreachable for 9 days
    // because three of the original five features never fire on real OC
    // payloads. This feature is payload-shape-tolerant (Anthropic-style
    // tool_use AND OpenAI-style tool_calls / function_call) and fires on
    // raw volume, so it surfaces struggle that the value-reading features
    // can miss.
    //
    // Note on the success=false floor (line ~430 below): it's a MAX, not
    // an addition. A failed turn with only this feature saturated scores
    // 0.5 (the floor wins). The feature shows its value by COMBINING with
    // other features — e.g., 8 tool calls (0.15) + 2 errors (0.25) +
    // 1 clarification (0.15) = 0.55, clearing the floor. The realistic
    // tier2-escalation path remains "multiple signals stack."
    tool_count_per_turn: 0.15,
};
const SATURATION = {
    tool_error_count: 2,
    tool_retry_count: 3,
    restart_markers: 2,
    clarification_loops: 1,
    // tokens_per_progress is ms-per-tool-call. 30000 = 30s of wall-clock per
    // successful tool call is the saturation point ("the model is grinding").
    // Below 5s of wall-clock per call contributes ~0. Linear between.
    tokens_per_progress: 30000,
    // tool_count_per_turn saturation = 8. Calibration logic:
    //   • 2-3 tool calls is normal multi-step work — should contribute ~0
    //   • 4-5 calls is a healthy subagent doing several reads — partial signal
    //   • 8+ is the load-bearing case (team-bot-a 2026-06-03 = 8 calls / $33.65)
    // 4 calls normalizes to 0.5 × 0.15 weight = 0.075 contribution — meaningful
    // but small by itself. 8+ = full 0.15 contribution, which is what the
    // feature contributes to the multi-signal aggregate (see DEFAULT_STRUGGLE_WEIGHTS
    // comment for the floor interaction).
    tool_count_per_turn: 8,
};
/**
 * Regex hits in *assistant* text that indicate the model is restarting,
 * backtracking, or trying a different approach. Empirically chosen from
 * how models actually phrase struggle; conservative — false positives are
 * OK because they're weighted alongside other signals.
 */
const RESTART_MARKER_PATTERNS = [
    /\blet me (try|attempt) (again|something else|a different)/i,
    // "approach" only counts when prefixed by a restart-signal modifier.
    // Bare "approach" fires on healthy turns ("Here's my approach to this")
    // — confirmed in round-3 code review (Medium #5). Required prefix
    // restricts to actual restart signal.
    /\b(a different|another|new) approach/i,
    /\bthat (didn't|did not) work/i,
    /\bhmm,?\s+(actually|wait|let me|that|i)/i,
    /\b(actually|wait),?\s+(let me|i should|i need|i'll)/i,
    /\bi'?ll (try|need to) (a different|another)/i,
    /\bgoing back to/i,
];
/**
 * Regex hits in *user* text that indicate the user is correcting the
 * model — strong signal that the previous turn didn't land.
 */
const CLARIFICATION_PATTERNS = [
    /\b(no|that'?s not),?\s+(i|what|that)/i,
    /\bi (meant|asked for|said)\b/i,
    /\bthat'?s not what i\b/i,
    /\byou (misunderstood|misread|got that wrong)/i,
    /\bnot quite\b/i,
    /\bagain,?\s+but/i,
];
// ── Content-block helpers ─────────────────────────────────────────────────────
/**
 * Extract text from a content blob — string or content-block array.
 * Mirrors TurnObserver.extractMessages's logic but exposed standalone so
 * struggle detection doesn't depend on the observer.
 */
function blockText(content) {
    if (typeof content === "string")
        return content;
    if (Array.isArray(content)) {
        return content
            .filter((b) => b && typeof b === "object" && b.type === "text")
            .map((b) => (typeof b.text === "string" ? b.text : ""))
            .join(" ");
    }
    return "";
}
/**
 * Iterate the content blocks across all assistant messages, yielding each
 * block. Tolerates string-form content (treated as a single text block).
 */
function* assistantBlocks(messages) {
    for (const m of messages) {
        if (!m || typeof m !== "object")
            continue;
        const msg = m;
        if (msg.role !== "assistant")
            continue;
        const content = msg.content;
        if (typeof content === "string") {
            yield { type: "text", text: content };
            continue;
        }
        if (Array.isArray(content)) {
            for (const b of content) {
                if (b && typeof b === "object")
                    yield b;
            }
        }
    }
}
/**
 * Iterate the content blocks across all user messages (similar to
 * assistantBlocks). User messages can also contain tool_result blocks —
 * those are the standard OC/Anthropic shape for "tool finished, here's
 * the output."
 */
function* userBlocks(messages) {
    for (const m of messages) {
        if (!m || typeof m !== "object")
            continue;
        const msg = m;
        if (msg.role !== "user")
            continue;
        const content = msg.content;
        if (typeof content === "string") {
            yield { type: "text", text: content };
            continue;
        }
        if (Array.isArray(content)) {
            for (const b of content) {
                if (b && typeof b === "object")
                    yield b;
            }
        }
    }
}
// ── Feature extractors ────────────────────────────────────────────────────────
/**
 * Count tool_result blocks where `is_error: true`. Anthropic API marks
 * failed tool calls this way; OC propagates the flag through.
 */
export function countToolErrors(messages) {
    let n = 0;
    for (const b of userBlocks(messages)) {
        if (b.type === "tool_result" && b.is_error === true)
            n += 1;
    }
    return n;
}
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
export function countToolRetries(messages) {
    const counts = new Map();
    for (const b of assistantBlocks(messages)) {
        if (b.type === "tool_use" && typeof b.name === "string") {
            counts.set(b.name, (counts.get(b.name) ?? 0) + 1);
        }
    }
    let retries = 0;
    for (const n of counts.values()) {
        if (n > 1)
            retries += n - 1;
    }
    return retries;
}
/**
 * Count restart/backtrack markers in assistant text. Same phrase in
 * multiple text blocks counts multiple times (i.e. we don't dedup) —
 * repetition is itself signal.
 */
export function countRestartMarkers(messages) {
    let n = 0;
    for (const b of assistantBlocks(messages)) {
        if (b.type !== "text")
            continue;
        const text = typeof b.text === "string" ? b.text : "";
        for (const pat of RESTART_MARKER_PATTERNS) {
            const m = text.match(new RegExp(pat.source, pat.flags + "g"));
            if (m)
                n += m.length;
        }
    }
    return n;
}
/**
 * Count clarification/correction markers in user text. Only the *last*
 * user message counts — clarification at turn N is signal that turn N-1
 * struggled, and we don't want to attribute it to turn N as well.
 *
 * NB: this is "this turn's last user message," not the user message
 * across the whole session. agent_end's messages array typically contains
 * only the current turn's exchange.
 */
export function countClarificationLoops(messages) {
    // Find the last user message and analyze just its text.
    let lastUserText = "";
    for (const m of messages) {
        if (!m || typeof m !== "object")
            continue;
        const msg = m;
        if (msg.role !== "user")
            continue;
        const text = blockText(msg.content);
        if (text)
            lastUserText = text;
    }
    if (!lastUserText)
        return 0;
    let n = 0;
    for (const pat of CLARIFICATION_PATTERNS) {
        if (pat.test(lastUserText))
            n += 1;
    }
    return n;
}
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
export function tokensPerProgressRatio(messages, durationMs) {
    if (!durationMs || durationMs <= 0)
        return 0;
    let total = 0;
    let errors = 0;
    for (const b of assistantBlocks(messages)) {
        if (b.type === "tool_use")
            total += 1;
    }
    for (const b of userBlocks(messages)) {
        if (b.type === "tool_result" && b.is_error === true)
            errors += 1;
    }
    const successful = Math.max(total - errors, 0);
    if (total === 0)
        return 0; // no tool work → not measured
    // Use total calls (not successful) as denominator floor to avoid
    // divide-by-zero blowups when every call failed — that *is* struggle.
    // The "failure" weight comes from tool_error_count, not from this ratio.
    const denom = Math.max(successful, 1);
    return durationMs / denom;
}
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
export function countToolCalls(messages) {
    let n = 0;
    for (const m of messages) {
        if (!m || typeof m !== "object")
            continue;
        const msg = m;
        if (msg.role !== "assistant")
            continue;
        // Anthropic-style content blocks
        const content = msg.content;
        if (Array.isArray(content)) {
            for (const b of content) {
                if (b && typeof b === "object" && b.type === "tool_use") {
                    n += 1;
                }
            }
        }
        // OpenAI-style top-level tool_calls array
        if (Array.isArray(msg.tool_calls)) {
            n += msg.tool_calls.length;
        }
        // OpenAI legacy single function_call wrapper
        if (msg.function_call && typeof msg.function_call === "object") {
            n += 1;
        }
    }
    return n;
}
// ── Combiner ──────────────────────────────────────────────────────────────────
function normalize(raw, saturation) {
    if (saturation <= 0)
        return 0;
    const x = raw / saturation;
    return x < 0 ? 0 : x > 1 ? 1 : x;
}
/**
 * Compute the struggle signal for a single turn.
 *
 * Pure function. No I/O, no logging, no side effects. Catches errors in
 * feature extraction silently and treats them as 0 — a misshapen messages
 * payload shouldn't blow up the turn loop. (The plugin always calls this
 * from a `try { ... } catch` in the host, but defensive zero-out is cheap
 * insurance.)
 */
export function computeStruggle(input) {
    const weights = {
        ...DEFAULT_STRUGGLE_WEIGHTS,
        ...(input.weights ?? {}),
    };
    // ── Payload-drift contract check (spec § 2.7) ─────────────────────────────
    // Return score=null (NOT 0) when we can't actually measure struggle:
    //   - messages is undefined / missing
    //   - messages is present but not an array (some future OC payload shape)
    //   - turn was marked failed (success=false) AND messages is empty
    // Each case names itself via payload_drift so the host can emit a
    // `cascade_payload_unexpected` Signal and the audit layer can bucket
    // these separately from real score=0 turns. Silent coercion to 0 was
    // the round-3 code-review finding (High #1).
    if (input.messages === undefined || input.messages === null) {
        return _emptySignal("no_messages");
    }
    if (!Array.isArray(input.messages)) {
        return _emptySignal("messages_not_array");
    }
    if (input.messages.length === 0 && input.success === false) {
        return _emptySignal("empty_on_failure");
    }
    const messages = input.messages;
    let raw_tool_error_count = 0;
    let raw_tool_retry_count = 0;
    let raw_restart_markers = 0;
    let raw_clarification_loops = 0;
    let raw_tokens_per_progress = 0;
    let raw_tool_count_per_turn = 0;
    try {
        raw_tool_error_count = countToolErrors(messages);
    }
    catch { /* keep 0 */ }
    try {
        raw_tool_retry_count = countToolRetries(messages);
    }
    catch { /* keep 0 */ }
    try {
        raw_restart_markers = countRestartMarkers(messages);
    }
    catch { /* keep 0 */ }
    try {
        raw_clarification_loops = countClarificationLoops(messages);
    }
    catch { /* keep 0 */ }
    try {
        raw_tokens_per_progress = tokensPerProgressRatio(messages, input.durationMs);
    }
    catch { /* keep 0 */ }
    try {
        raw_tool_count_per_turn = countToolCalls(messages);
    }
    catch { /* keep 0 */ }
    const n_tool_error_count = normalize(raw_tool_error_count, SATURATION.tool_error_count);
    const n_tool_retry_count = normalize(raw_tool_retry_count, SATURATION.tool_retry_count);
    const n_restart_markers = normalize(raw_restart_markers, SATURATION.restart_markers);
    const n_clarification_loops = normalize(raw_clarification_loops, SATURATION.clarification_loops);
    const n_tokens_per_progress = normalize(raw_tokens_per_progress, SATURATION.tokens_per_progress);
    const n_tool_count_per_turn = normalize(raw_tool_count_per_turn, SATURATION.tool_count_per_turn);
    const contrib = {
        tool_error_count: n_tool_error_count * weights.tool_error_count,
        tool_retry_count: n_tool_retry_count * weights.tool_retry_count,
        restart_markers: n_restart_markers * weights.restart_markers,
        clarification_loops: n_clarification_loops * weights.clarification_loops,
        tokens_per_progress: n_tokens_per_progress * weights.tokens_per_progress,
        tool_count_per_turn: n_tool_count_per_turn * weights.tool_count_per_turn,
    };
    let score = contrib.tool_error_count +
        contrib.tool_retry_count +
        contrib.restart_markers +
        contrib.clarification_loops +
        contrib.tokens_per_progress +
        contrib.tool_count_per_turn;
    // A failed turn (OC's success=false) is a strong signal on its own.
    // Floor the score at 0.5 when OC says the turn failed, so the cascade
    // controller always sees "this didn't go well" even if no individual
    // feature lit up. Conservative — doesn't replace, only raises.
    if (input.success === false) {
        score = Math.max(score, 0.5);
    }
    if (score < 0)
        score = 0;
    if (score > 1)
        score = 1;
    return {
        score,
        features: contrib,
        raw: {
            tool_error_count: raw_tool_error_count,
            tool_retry_count: raw_tool_retry_count,
            restart_markers: raw_restart_markers,
            clarification_loops: raw_clarification_loops,
            tokens_per_progress: raw_tokens_per_progress,
            tool_count_per_turn: raw_tool_count_per_turn,
        },
        payload_drift: null,
    };
}
/**
 * Constructor for the score=null sentinel returned on payload drift.
 * Exported for tests; not part of the public API.
 */
function _emptySignal(reason) {
    return {
        score: null,
        features: {
            tool_error_count: 0,
            tool_retry_count: 0,
            restart_markers: 0,
            clarification_loops: 0,
            tokens_per_progress: 0,
            tool_count_per_turn: 0,
        },
        raw: {
            tool_error_count: 0,
            tool_retry_count: 0,
            restart_markers: 0,
            clarification_loops: 0,
            tokens_per_progress: 0,
            tool_count_per_turn: 0,
        },
        payload_drift: reason,
    };
}
//# sourceMappingURL=StruggleDetector.js.map