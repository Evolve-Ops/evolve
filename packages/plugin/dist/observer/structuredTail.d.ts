/**
 * structuredTail — ask the model that is already answering, instead of
 * calling a second one.
 *
 * Decision: internal/decision-evolve-overhead-2026-09-07.md D-OH4 —
 * "Where a live judgement is needed, the primary model returns it
 * inside its own turn (a short structured tail the plugin parses and
 * strips), never a second call."
 *
 * The struggle judge used to be a whole extra model call per session
 * whose pre-thresholds tripped: build a snippet, run a subagent, wait
 * 3s, cache-write the snippet, bill it to the bot. The judgement itself
 * is one word. So we append a one-line instruction to the turn the bot
 * is already taking, and read the answer off the end of its reply.
 *
 * The tail is delimited, not positional, because a model that puts a
 * newline in the wrong place must not be able to eat the user's reply:
 *
 *     …the bot's actual answer to the user.
 *     <<<evolve:judge STRUGGLING evolve:judge>>>
 *
 * Two rules govern the strip, and both exist because the failure mode
 * here is silently deleting a reply someone was waiting for:
 *
 *   1. A WELL-FORMED tail is removed, along with the whitespace that
 *      separated it from the reply.
 *   2. A MALFORMED tail — an opener with no closer, a closer with no
 *      opener, an unrecognised verdict — is LEFT VISIBLE and yields no
 *      verdict. Ugly beats lossy: a stray marker in a reply is a bug an
 *      operator can see and report, whereas a heuristic strip that
 *      guesses at the boundary deletes real text and nobody finds out.
 *
 * Pure. No I/O, no clock.
 */
/** The verdicts the judge tail may carry. Same vocabulary as SessionStruggleJudge. */
export type TailVerdict = "OK" | "STRUGGLING" | "AMBIGUOUS";
export declare const TAIL_OPEN = "<<<evolve:judge";
export declare const TAIL_CLOSE = "evolve:judge>>>";
export interface ParsedTail {
    /** The verdict, or null when there was no tail or it did not parse. */
    verdict: TailVerdict | null;
    /** The text with a well-formed tail removed; unchanged otherwise. */
    stripped: string;
    /** True when a tail marker was present but could not be parsed. */
    malformed: boolean;
    /** True when a well-formed tail was found and removed. */
    found: boolean;
}
/**
 * The instruction appended to the turn's system prompt when a
 * judgement is wanted from THIS turn.
 *
 * Deliberately short: it rides on every sampled turn's prompt, so its
 * own token cost is part of the overhead this decision is trying to
 * remove. Two sentences and an example.
 */
export declare function buildJudgeTailRequest(): string;
/**
 * Parse and strip the judge tail from an assistant message.
 *
 * Uses the LAST opener in the text: a reply that discusses the marker
 * (a bot explaining its own plumbing, say) still has its real tail read
 * off the end rather than off the quoted one.
 */
export declare function parseStructuredTail(text: unknown): ParsedTail;
/**
 * Strip a well-formed tail without caring about the verdict. Used on
 * the outbound payload seam, where the only job is that the user never
 * sees the marker.
 */
export declare function stripStructuredTail(text: unknown): string;
/** True when the text carries either marker — cheap pre-check for the hot path. */
export declare function hasTailMarker(text: unknown): boolean;
//# sourceMappingURL=structuredTail.d.ts.map