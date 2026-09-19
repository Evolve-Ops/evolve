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

// ── Public types ─────────────────────────────────────────────────────────────

export interface PushbackWeights {
  clarification_regex: number;
  rephrase_similarity: number;
  short_followup: number;
}

interface PushbackSaturation {
  /** clarification_regex saturates at 1 hit (the existing pattern set fires
   *  at most a handful of times on real text; one is signal enough). */
  clarification_regex: number;
  /** rephrase_similarity saturates at Jaccard ≥ 0.4. Empirically: shorter
   *  user messages with 40%+ token overlap are reliably rephrases. */
  rephrase_similarity: number;
  /** short_followup is already binary (0 | 1) — saturation = 1. */
  short_followup: number;
}

export type PushbackPayloadDrift =
  | "no_prior_turn"
  | "dnt"
  | "empty_messages"
  | null;

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

// ── Defaults ─────────────────────────────────────────────────────────────────

export const DEFAULT_PUSHBACK_WEIGHTS: PushbackWeights = {
  clarification_regex: 0.35,
  rephrase_similarity: 0.40,
  short_followup: 0.25,
};

const SATURATION: PushbackSaturation = {
  clarification_regex: 1,
  rephrase_similarity: 0.4,
  short_followup: 1,
};

/** Threshold below which a current message is treated as a bare follow-up
 *  ("hmm", "no", "try again", "again?"). */
const SHORT_FOLLOWUP_MAX_CHARS = 25;

/** Lower bound on the prior assistant reply for the short_followup feature
 *  to fire. Prevents "ok" → "ok" assistant-prompted yes/no exchanges from
 *  reading as pushback. */
const SHORT_FOLLOWUP_MIN_PRIOR_ASSISTANT_CHARS = 100;

/**
 * Clarification regex set. Deliberately identical to
 * `StruggleDetector.CLARIFICATION_PATTERNS` — the lexical signal is the
 * same one. Duplicated here as a private constant (rather than imported)
 * to keep the detector standalone and pure-function. If the StruggleDetector
 * set drifts later, the divergence is intentional, not accidental.
 */
const CLARIFICATION_PATTERNS: RegExp[] = [
  /\b(no|that'?s not),?\s+(i|what|that)/i,
  /\bi (meant|asked for|said)\b/i,
  /\bthat'?s not what i\b/i,
  /\byou (misunderstood|misread|got that wrong)/i,
  /\bnot quite\b/i,
  /\bagain,?\s+but/i,
];

/**
 * English stop words removed before Jaccard token-set comparison.
 * Conservative — only function words that contribute noise to overlap
 * (articles, common auxiliaries, prepositions). Content words ("write",
 * "delete", "summary") stay so a topic rephrase still scores high.
 */
const STOP_WORDS = new Set([
  "a", "an", "the",
  "is", "are", "was", "were", "be", "been", "being",
  "do", "does", "did", "done",
  "have", "has", "had",
  "of", "to", "in", "on", "for", "with", "at", "by", "from", "about",
  "and", "or", "but", "so", "if",
  "i", "you", "we", "they", "it", "he", "she",
  "me", "my", "your", "our",
  "that", "this", "these", "those",
  "can", "could", "would", "should", "will", "shall", "may", "might",
  "not",
]);

// ── Feature extractors ───────────────────────────────────────────────────────

/**
 * Count regex matches in the current user message.
 */
export function countClarificationHits(text: string): number {
  if (!text) return 0;
  let n = 0;
  for (const pat of CLARIFICATION_PATTERNS) {
    if (pat.test(text)) n += 1;
  }
  return n;
}

/**
 * Tokenize a string into a lowercase content-word set. Stop-words removed,
 * punctuation stripped. The result is a Set so duplicate words count once
 * — Jaccard cares about set membership, not term frequency.
 */
export function tokenizeForJaccard(text: string): Set<string> {
  if (!text) return new Set();
  const cleaned = text
    .toLowerCase()
    // Replace anything that isn't a letter, digit, or apostrophe with space.
    // Keep apostrophes to preserve "don't" / "won't" as single tokens.
    .replace(/[^a-z0-9']+/g, " ")
    .trim();
  if (!cleaned) return new Set();
  const out = new Set<string>();
  for (const tok of cleaned.split(/\s+/)) {
    if (tok.length < 2) continue;       // single letters add noise
    if (STOP_WORDS.has(tok)) continue;
    out.add(tok);
  }
  return out;
}

/**
 * Jaccard similarity between two token sets: |A ∩ B| / |A ∪ B|.
 * Returns 0 when either set is empty.
 */
export function jaccardSimilarity(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  let inter = 0;
  // Iterate the smaller set for membership check on the larger.
  const [small, large] = a.size <= b.size ? [a, b] : [b, a];
  for (const tok of small) {
    if (large.has(tok)) inter += 1;
  }
  const union = a.size + b.size - inter;
  return union > 0 ? inter / union : 0;
}

/**
 * Binary short-followup gate: current user message is bare ("hmm", "no",
 * "try again"), the prior assistant reply was substantive (≥ 100 chars),
 * and the current message has no question mark (questions are presumed
 * to be new asks, not pushback).
 */
export function isShortFollowup(
  currentText: string,
  priorAssistantText: string,
): boolean {
  const t = (currentText ?? "").trim();
  if (t.length === 0) return false;
  if (t.length > SHORT_FOLLOWUP_MAX_CHARS) return false;
  if (t.includes("?")) return false;
  const prior = (priorAssistantText ?? "").trim();
  if (prior.length < SHORT_FOLLOWUP_MIN_PRIOR_ASSISTANT_CHARS) return false;
  return true;
}

// ── Combiner ─────────────────────────────────────────────────────────────────

function normalize(raw: number, saturation: number): number {
  if (saturation <= 0) return 0;
  const x = raw / saturation;
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

function _emptySignal(reason: NonNullable<PushbackPayloadDrift>): PushbackSignal {
  return {
    score: null,
    features: {
      clarification_regex: 0,
      rephrase_similarity: 0,
      short_followup: 0,
    },
    raw: {
      clarification_loops: 0,
      jaccard_similarity: 0,
      current_chars: 0,
      prior_user_chars: 0,
      prior_assistant_chars: 0,
    },
    payload_drift: reason,
  };
}

/**
 * Compute the user-pushback signal for a single turn.
 *
 * Pure function. No I/O, no logging. Catches errors in feature extraction
 * silently (defensive zero) — a misshapen text payload shouldn't blow up
 * the turn loop. The plugin always calls this from a try/catch in the
 * host anyway.
 */
export function computePushback(input: ComputePushbackInput): PushbackSignal {
  // ── DNT check (spec § 6) ──────────────────────────────────────────────
  // When the operator has turned off pushback observation, we don't run
  // the regex OR the text comparison. Score is null with the dnt reason
  // so the aggregator knows this turn was opted out vs. cleanly measured-zero.
  if (input.dntEnabled === false) {
    return _emptySignal("dnt");
  }

  const currentRaw = input.currentUserText ?? "";
  const priorUserRaw = input.previousUserText ?? "";
  const priorAssistantRaw = input.previousAssistantText ?? "";
  const current = currentRaw.trim();
  const priorUser = priorUserRaw.trim();
  const priorAssistant = priorAssistantRaw.trim();

  // ── Payload-drift contract check ──────────────────────────────────────
  // No prior user turn: turn 1 of the session. Clarification regex on the
  // current message is still meaningful, but we can't form a composite —
  // the spec says return null with the named reason and let the aggregator
  // treat this turn as "unmeasurable." Don't silently degrade to a
  // current-message-only score; that's the substring-match shape this spec
  // exists to leave behind.
  if (!priorUser) {
    const sig = _emptySignal("no_prior_turn");
    // Still surface clarification_loops in raw — useful as a debug
    // anchor when investigating false-positive trends, but not enough on
    // its own to drive the chip.
    try {
      sig.raw.clarification_loops = countClarificationHits(current);
    } catch { /* keep 0 */ }
    sig.raw.current_chars = current.length;
    return sig;
  }

  // Both messages empty after strip: nothing to measure. Distinct from
  // "no_prior_turn" so the audit layer can bucket the cases separately.
  if (!current && !priorUser) {
    return _emptySignal("empty_messages");
  }

  const weights: PushbackWeights = {
    ...DEFAULT_PUSHBACK_WEIGHTS,
    ...(input.weights ?? {}),
  };

  let raw_clarification = 0;
  let raw_jaccard = 0;
  let raw_short_followup = 0;

  try {
    raw_clarification = countClarificationHits(current);
  } catch { /* keep 0 */ }
  try {
    const aSet = tokenizeForJaccard(current);
    const bSet = tokenizeForJaccard(priorUser);
    raw_jaccard = jaccardSimilarity(aSet, bSet);
  } catch { /* keep 0 */ }
  try {
    raw_short_followup = isShortFollowup(current, priorAssistant) ? 1 : 0;
  } catch { /* keep 0 */ }

  const n_clarification = normalize(raw_clarification, SATURATION.clarification_regex);
  const n_jaccard = normalize(raw_jaccard, SATURATION.rephrase_similarity);
  const n_short_followup = normalize(raw_short_followup, SATURATION.short_followup);

  const contrib: Record<keyof PushbackWeights, number> = {
    clarification_regex: n_clarification * weights.clarification_regex,
    rephrase_similarity: n_jaccard * weights.rephrase_similarity,
    short_followup: n_short_followup * weights.short_followup,
  };

  let score =
    contrib.clarification_regex +
    contrib.rephrase_similarity +
    contrib.short_followup;

  if (score < 0) score = 0;
  if (score > 1) score = 1;

  return {
    score,
    features: contrib,
    raw: {
      clarification_loops: raw_clarification,
      jaccard_similarity: raw_jaccard,
      current_chars: current.length,
      prior_user_chars: priorUser.length,
      prior_assistant_chars: priorAssistant.length,
    },
    payload_drift: null,
  };
}
