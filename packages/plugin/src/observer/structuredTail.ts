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

export const TAIL_OPEN = "<<<evolve:judge";
export const TAIL_CLOSE = "evolve:judge>>>";

const VALID_VERDICTS: ReadonlySet<string> = new Set(["OK", "STRUGGLING", "AMBIGUOUS"]);

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
export function buildJudgeTailRequest(): string {
  return (
    `After your reply to the user, on its own final line, append exactly:\n` +
    `${TAIL_OPEN} <VERDICT> ${TAIL_CLOSE}\n` +
    `where <VERDICT> is OK if this conversation is going fine, STRUGGLING if ` +
    `the user is stuck or repeating themselves, or AMBIGUOUS if you cannot ` +
    `tell. The line is stripped before the user sees your reply — never ` +
    `mention it, and never let it change what you say above it.`
  );
}

/**
 * Parse and strip the judge tail from an assistant message.
 *
 * Uses the LAST opener in the text: a reply that discusses the marker
 * (a bot explaining its own plumbing, say) still has its real tail read
 * off the end rather than off the quoted one.
 */
export function parseStructuredTail(text: unknown): ParsedTail {
  const source = typeof text === "string" ? text : "";
  const openAt = source.lastIndexOf(TAIL_OPEN);

  if (openAt < 0) {
    // No opener. A bare closer is still a malformed marker — report it
    // so the caller can log, but leave the text alone.
    return {
      verdict: null,
      stripped: source,
      malformed: source.includes(TAIL_CLOSE),
      found: false,
    };
  }

  const bodyStart = openAt + TAIL_OPEN.length;
  const closeAt = source.indexOf(TAIL_CLOSE, bodyStart);
  if (closeAt < 0) {
    // Opener with no closer — we do not know where the tail ends, so we
    // do not guess. Left visible.
    return { verdict: null, stripped: source, malformed: true, found: false };
  }

  const body = source.slice(bodyStart, closeAt).trim().toUpperCase();
  if (!VALID_VERDICTS.has(body)) {
    // Well-delimited but unintelligible. The delimiters DO tell us where
    // the block ends, but an unparseable verdict means the model did not
    // follow the contract, and we would rather show the operator that
    // than quietly swallow whatever it did emit.
    return { verdict: null, stripped: source, malformed: true, found: false };
  }

  const before = source.slice(0, openAt);
  const after = source.slice(closeAt + TAIL_CLOSE.length);
  // Drop the whitespace that separated the reply from the tail, but keep
  // anything the model wrote AFTER the closer (there should be nothing;
  // if there is, it is the user's text and not ours to delete).
  const stripped = (before.replace(/\s+$/, "") + after.replace(/^[ \t]*\n?/, "")).trimEnd();

  return {
    verdict: body as TailVerdict,
    stripped,
    malformed: false,
    found: true,
  };
}

/**
 * Strip a well-formed tail without caring about the verdict. Used on
 * the outbound payload seam, where the only job is that the user never
 * sees the marker.
 */
export function stripStructuredTail(text: unknown): string {
  return parseStructuredTail(text).stripped;
}

/** True when the text carries either marker — cheap pre-check for the hot path. */
export function hasTailMarker(text: unknown): boolean {
  const s = typeof text === "string" ? text : "";
  return s.includes(TAIL_OPEN) || s.includes(TAIL_CLOSE);
}
