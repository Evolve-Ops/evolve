/**
 * PushbackDetector
 *
 * Pure-function per-turn signal that measures user pushback against the bot.
 * Replaces the substring-match `correction_detected` signal as the more
 * honest "user is in fact struggling with this bot" indicator.
 *
 * Spec: internal/spec-user-pushback-signal-2026-05-30.md.
 *
 * Design constraints (mirror StruggleDetector):
 *   - No I/O. All inputs are passed in. Pure-function unit tests.
 *   - Cheap. Regex + token-set Jaccard. Microsecond budget per turn.
 *   - Tri-state output. `score: null` (couldn't measure) ≠ `score: 0`
 *     (measured: no pushback). Per
 *     `feedback_distinguish_tooling_failure_from_findings`, silent
 *     coercion to 0 is the bug class this contract prevents.
 *   - Configurable weights — initial values are educated guesses; RSI can
 *     tune them after Phase 1 ships and data accumulates.
 *
 * Why not just expand the keyword list:
 *   The current `correction_detected` substring matcher has orthogonal
 *   failure modes (FN on rephrase, FP on innocuous code-review prose).
 *   Adding keywords doesn't fix either. Two cheap signals AND'd together
 *   — a clarification regex AND a structural rephrase check — fail on
 *   different shapes, so co-firing correlates with real pushback.
 */
export interface PushbackWeights {
    clarification_regex: number;
    rephrase_similarity: number;
    short_followup: number;
}
export type PushbackPayloadDrift = "no_prior_turn" | "dnt" | "empty_messages" | null;
export interface PushbackSignal {
    /**
     * Combined score in [0, 1]. **May be `null`** when the input doesn't
     * permit measurement (no prior user turn, DNT flag off, both messages
     * empty). Downstream consumers MUST distinguish `null` from `0`.
     */
    score: number | null;
    /** Per-feature normalized contributions (post-weight). */
    features: Record<keyof PushbackWeights, number>;
    /** Raw feature values before normalization. */
    raw: {
        clarification_loops: number;
        jaccard_similarity: number;
        current_chars: number;
        prior_user_chars: number;
        prior_assistant_chars: number;
    };
    /** Names why score is null, or null when score is a real number. */
    payload_drift: PushbackPayloadDrift;
}
export interface ComputePushbackInput {
    /** Current turn's last user message. */
    currentUserText: string | null | undefined;
    /** Previous turn's last user message (same session). Null on turn 1. */
    previousUserText: string | null | undefined;
    /** Previous turn's assistant reply text. Used to gate `short_followup`. */
    previousAssistantText: string | null | undefined;
    /** Operator/user DNT flag for this bot. When false, score is null. */
    dntEnabled?: boolean;
    /** Override the default weights (Phase 4 audit may push tuned values). */
    weights?: Partial<PushbackWeights>;
}
export declare const DEFAULT_PUSHBACK_WEIGHTS: PushbackWeights;
/**
 * Count regex matches in the current user message.
 */
export declare function countClarificationHits(text: string): number;
/**
 * Tokenize a string into a lowercase content-word set. Stop-words removed,
 * punctuation stripped. The result is a Set so duplicate words count once
 * — Jaccard cares about set membership, not term frequency.
 */
export declare function tokenizeForJaccard(text: string): Set<string>;
/**
 * Jaccard similarity between two token sets: |A ∩ B| / |A ∪ B|.
 * Returns 0 when either set is empty.
 */
export declare function jaccardSimilarity(a: Set<string>, b: Set<string>): number;
/**
 * Binary short-followup gate: current user message is bare ("hmm", "no",
 * "try again"), the prior assistant reply was substantive (≥ 100 chars),
 * and the current message has no question mark (questions are presumed
 * to be new asks, not pushback).
 */
export declare function isShortFollowup(currentText: string, priorAssistantText: string): boolean;
/**
 * Compute the user-pushback signal for a single turn.
 *
 * Pure function. No I/O, no logging. Catches errors in feature extraction
 * silently (defensive zero) — a misshapen text payload shouldn't blow up
 * the turn loop. The plugin always calls this from a try/catch in the
 * host anyway.
 */
export declare function computePushback(input: ComputePushbackInput): PushbackSignal;
//# sourceMappingURL=PushbackDetector.d.ts.map