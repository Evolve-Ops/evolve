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

// ── Public types ─────────────────────────────────────────────────────────────

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

interface TrivialitySaturation {
  short_user_message: number;       // word count under N = fully short
  single_decisive_tool: number;     // ms — placeholder, see below
  short_assistant_response: number; // word count under N = fully short
  no_struggle_markers: number;      // 0 markers = full credit; ramps down
  no_clarification: number;         // 0 clarif loops = full credit
  fast_completion: number;          // ms — completion ≤ this = fully fast
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
  messages: unknown;  // intentionally unknown to allow drift detection
  durationMs?: number;
  success?: boolean;
  weights?: Partial<TrivialityWeights>;
}

// ── Defaults ──────────────────────────────────────────────────────────────────

export const DEFAULT_TRIVIALITY_WEIGHTS: TrivialityWeights = {
  short_user_message: 0.25,
  single_decisive_tool: 0.25,
  short_assistant_response: 0.15,
  no_struggle_markers: 0.15,
  no_clarification: 0.10,
  fast_completion: 0.10,
};

const SATURATION: TrivialitySaturation = {
  // Word count under 50 = full credit. Token approximation: ~50 tokens ≈ 38 words,
  // but we round up to 50 words for a more intuitive threshold and to account for
  // models being more verbose than humans.
  short_user_message: 50,
  // Currently unused; placeholder for a future tool-duration-based signal.
  single_decisive_tool: 0,
  short_assistant_response: 100,
  // 0 markers = full triviality credit. Even ONE struggle marker fully cancels.
  no_struggle_markers: 1,
  no_clarification: 1,
  // 5 seconds (5000ms) = fully fast. Beyond 30 seconds = zero contribution.
  fast_completion: 5000,
};

// ── Content-block helpers (mirrors StruggleDetector's structure) ─────────────

function* assistantBlocks(messages: unknown[]): Iterable<any> {
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    const msg = m as any;
    if (msg.role !== "assistant") continue;
    const content = msg.content;
    if (typeof content === "string") {
      yield { type: "text", text: content };
      continue;
    }
    if (Array.isArray(content)) {
      for (const b of content) {
        if (b && typeof b === "object") yield b;
      }
    }
  }
}

function* userBlocks(messages: unknown[]): Iterable<any> {
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    const msg = m as any;
    if (msg.role !== "user") continue;
    const content = msg.content;
    if (typeof content === "string") {
      yield { type: "text", text: content };
      continue;
    }
    if (Array.isArray(content)) {
      for (const b of content) {
        if (b && typeof b === "object") yield b;
      }
    }
  }
}

function blockText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return (content as any[])
      .filter((b) => b && typeof b === "object" && b.type === "text")
      .map((b: any) => (typeof b.text === "string" ? b.text : ""))
      .join(" ");
  }
  return "";
}

// ── Restart-marker / clarification regexes ───────────────────────────────────
//
// These are the SAME patterns StruggleDetector uses for restart_markers and
// clarification_loops. The "no_struggle_markers" and "no_clarification"
// triviality features reward their ABSENCE. Duplicating the patterns here
// (instead of importing from StruggleDetector) keeps the two detectors
// independently versionable — the spec explicitly calls them "symmetric
// but separate."

const RESTART_MARKER_PATTERNS: RegExp[] = [
  /\blet me (try|attempt) (again|something else|a different)/i,
  /\b(a different|another|new) approach/i,
  /\bthat (didn't|did not) work/i,
  /\bhmm,?\s+(actually|wait|let me|that|i)/i,
  /\b(actually|wait),?\s+(let me|i should|i need|i'll)/i,
  /\bi'?ll (try|need to) (a different|another)/i,
  /\bgoing back to/i,
];

const CLARIFICATION_PATTERNS: RegExp[] = [
  /\b(no|that'?s not),?\s+(i|what|that)/i,
  /\bi (meant|asked for|said)\b/i,
  /\bthat'?s not what i\b/i,
  /\byou (misunderstood|misread|got that wrong)/i,
  /\bnot quite\b/i,
  /\bagain,?\s+but/i,
];

// ── Feature extractors ────────────────────────────────────────────────────────

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
export function lastUserMessageWordCount(messages: unknown[]): number {
  let lastUserText = "";
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    const msg = m as any;
    if (msg.role !== "user") continue;
    const text = blockText(msg.content);
    if (text) lastUserText = text;
  }
  if (!lastUserText) return 0;
  // Word count = non-empty whitespace-separated tokens.
  return lastUserText.trim().split(/\s+/).filter(Boolean).length;
}

/**
 * Count words in the LAST assistant message (text blocks only).
 * Triviality looks at the bot's response length — short answer to a
 * short question = the work was probably trivial.
 */
export function lastAssistantMessageWordCount(messages: unknown[]): number {
  let lastText = "";
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    const msg = m as any;
    if (msg.role !== "assistant") continue;
    const text = blockText(msg.content);
    if (text) lastText = text;
  }
  if (!lastText) return 0;
  return lastText.trim().split(/\s+/).filter(Boolean).length;
}

/**
 * Return 1 if the turn made exactly one tool call AND it succeeded
 * first try, else 0. "Decisive" = no retries, no errors. This is the
 * pattern of a confident answer ("look up X, use the result, respond").
 *
 * Zero tool calls is NOT triviality — could be a complex pure-reasoning
 * question. We need POSITIVE evidence of competence, not absence of
 * tool use.
 */
export function singleDecisiveToolUsed(messages: unknown[]): boolean {
  let toolUseCount = 0;
  let toolErrorCount = 0;
  for (const b of assistantBlocks(messages)) {
    if (b.type === "tool_use") toolUseCount += 1;
  }
  for (const b of userBlocks(messages)) {
    if (b.type === "tool_result" && b.is_error === true) toolErrorCount += 1;
  }
  return toolUseCount === 1 && toolErrorCount === 0;
}

/**
 * Count struggle markers in assistant text. Same patterns
 * StruggleDetector uses; triviality rewards their ABSENCE.
 */
export function countStruggleMarkersForTriviality(messages: unknown[]): number {
  let n = 0;
  for (const b of assistantBlocks(messages)) {
    if (b.type !== "text") continue;
    const text = typeof b.text === "string" ? b.text : "";
    for (const pat of RESTART_MARKER_PATTERNS) {
      const m = text.match(new RegExp(pat.source, pat.flags + "g"));
      if (m) n += m.length;
    }
  }
  return n;
}

/**
 * Count clarification markers in last user message. Same patterns
 * StruggleDetector uses; triviality rewards their ABSENCE.
 */
export function countClarificationForTriviality(messages: unknown[]): number {
  let lastUserText = "";
  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    const msg = m as any;
    if (msg.role !== "user") continue;
    const text = blockText(msg.content);
    if (text) lastUserText = text;
  }
  if (!lastUserText) return 0;
  let n = 0;
  for (const pat of CLARIFICATION_PATTERNS) {
    if (pat.test(lastUserText)) n += 1;
  }
  return n;
}

// ── Combiner ──────────────────────────────────────────────────────────────────

function normalizeShorter(raw: number, threshold: number): number {
  // For "short" metrics: smaller = more trivial. raw=0 → 1.0, raw>=threshold → 0.
  if (threshold <= 0) return 0;
  if (raw <= 0) return 1;
  if (raw >= threshold) return 0;
  return 1 - raw / threshold;
}

function normalizeAbsence(raw: number, saturation: number): number {
  // For "absence-of" metrics: 0 hits = full credit; any hit = zero credit.
  if (saturation <= 0) return raw === 0 ? 1 : 0;
  if (raw === 0) return 1;
  if (raw >= saturation) return 0;
  return 1 - raw / saturation;
}

function normalizeFastCompletion(durationMs: number | undefined, satMs: number): number {
  // 0ms (no duration given) → 0 contribution (we couldn't measure speed).
  // ≤ satMs → full triviality.
  // ≥ 6 × satMs → zero (turn took long enough that it's not "trivial").
  if (durationMs === undefined || durationMs <= 0) return 0;
  if (satMs <= 0) return 0;
  if (durationMs <= satMs) return 1;
  const upper = satMs * 6;
  if (durationMs >= upper) return 0;
  return 1 - (durationMs - satMs) / (upper - satMs);
}

export function computeTriviality(input: ComputeTrivialityInput): TrivialitySignal {
  const weights: TrivialityWeights = {
    ...DEFAULT_TRIVIALITY_WEIGHTS,
    ...(input.weights ?? {}),
  };

  // ── Payload-drift contract (spec § 2.7) ─────────────────────────────────────
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

  // Triviality requires positive evidence. An empty messages array
  // (legitimately empty turn — e.g., an immediate-return from a hook
  // interception) doesn't have evidence either way. Absence-of-struggle
  // features would otherwise fire 1.0 on emptiness and produce a
  // misleading high score. Per spec § 2.5: "demotion requires positive
  // evidence, never absence-of-signal alone." Empty content = 0.
  if (messages.length === 0) {
    return {
      score: 0,
      features: {
        short_user_message: 0, single_decisive_tool: 0,
        short_assistant_response: 0, no_struggle_markers: 0,
        no_clarification: 0, fast_completion: 0,
      },
      raw: {
        short_user_message: 0, single_decisive_tool: 0,
        short_assistant_response: 0, no_struggle_markers: 0,
        no_clarification: 0, fast_completion: 0,
      },
      payload_drift: null,
    };
  }

  let raw_short_user_message = 0;
  let raw_single_decisive_tool = 0;
  let raw_short_assistant_response = 0;
  let raw_no_struggle_markers = 0;
  let raw_no_clarification = 0;
  let raw_fast_completion = 0;

  try { raw_short_user_message = lastUserMessageWordCount(messages); } catch { /* keep 0 */ }
  try { raw_single_decisive_tool = singleDecisiveToolUsed(messages) ? 1 : 0; } catch { /* keep 0 */ }
  try { raw_short_assistant_response = lastAssistantMessageWordCount(messages); } catch { /* keep 0 */ }
  try { raw_no_struggle_markers = countStruggleMarkersForTriviality(messages); } catch { /* keep 0 */ }
  try { raw_no_clarification = countClarificationForTriviality(messages); } catch { /* keep 0 */ }
  try { raw_fast_completion = input.durationMs ?? 0; } catch { /* keep 0 */ }

  // Normalize each feature into [0, 1] triviality contribution.
  const n_short_user_message = normalizeShorter(raw_short_user_message, SATURATION.short_user_message);
  // single_decisive_tool is binary — 1 if matched, 0 if not.
  const n_single_decisive_tool = raw_single_decisive_tool;
  const n_short_assistant_response = normalizeShorter(raw_short_assistant_response, SATURATION.short_assistant_response);
  const n_no_struggle_markers = normalizeAbsence(raw_no_struggle_markers, SATURATION.no_struggle_markers);
  const n_no_clarification = normalizeAbsence(raw_no_clarification, SATURATION.no_clarification);
  const n_fast_completion = normalizeFastCompletion(input.durationMs, SATURATION.fast_completion);

  const contrib: Record<keyof TrivialityWeights, number> = {
    short_user_message: n_short_user_message * weights.short_user_message,
    single_decisive_tool: n_single_decisive_tool * weights.single_decisive_tool,
    short_assistant_response: n_short_assistant_response * weights.short_assistant_response,
    no_struggle_markers: n_no_struggle_markers * weights.no_struggle_markers,
    no_clarification: n_no_clarification * weights.no_clarification,
    fast_completion: n_fast_completion * weights.fast_completion,
  };

  let score =
    contrib.short_user_message +
    contrib.single_decisive_tool +
    contrib.short_assistant_response +
    contrib.no_struggle_markers +
    contrib.no_clarification +
    contrib.fast_completion;

  // A FAILED turn is by definition not trivial. Floor triviality at 0
  // when OC says success=false — overrides whatever positive features
  // may have fired (defensive against pathological cases).
  if (input.success === false) {
    score = 0;
  }

  if (score < 0) score = 0;
  if (score > 1) score = 1;

  return {
    score,
    features: contrib,
    raw: {
      short_user_message: raw_short_user_message,
      single_decisive_tool: raw_single_decisive_tool,
      short_assistant_response: raw_short_assistant_response,
      no_struggle_markers: raw_no_struggle_markers,
      no_clarification: raw_no_clarification,
      fast_completion: raw_fast_completion,
    },
    payload_drift: null,
  };
}

function _emptySignal(reason: NonNullable<TrivialitySignal["payload_drift"]>): TrivialitySignal {
  return {
    score: null,
    features: {
      short_user_message: 0,
      single_decisive_tool: 0,
      short_assistant_response: 0,
      no_struggle_markers: 0,
      no_clarification: 0,
      fast_completion: 0,
    },
    raw: {
      short_user_message: 0,
      single_decisive_tool: 0,
      short_assistant_response: 0,
      no_struggle_markers: 0,
      no_clarification: 0,
      fast_completion: 0,
    },
    payload_drift: reason,
  };
}
