/**
 * BlockStability — byte-stable helpers for the before_prompt_build injection
 * blocks.
 *
 * Motivation (internal/incident-post-mortem-2026-07-31-cost-containment.md §2 +
 * internal/spec-context-observability-2026-07-30.md): everything Evolve appends
 * via ``appendSystemContext`` lands in the system prompt AHEAD of the whole
 * conversation, so any byte change invalidates the entire prompt cache —
 * a 12.5–20× cost multiplier on the context. The blocks must therefore only
 * change bytes when their MEANING changes. Three churn mechanisms this module
 * removes:
 *
 * 1. **Soft-fail flapping.** The renderers soft-fail to ``""`` on any fault
 *    (socket timeout, slow subprocess, daemon restart) and cache the empty
 *    result for the TTL. One transient glitch therefore flips the block
 *    absent → present, i.e. TWO full-prefix invalidations per glitch.
 *    ``StickyBlockCache`` serves the last-good value through failures
 *    instead (bounded by ``maxStaleMs``), so a glitch costs zero
 *    invalidations.
 *
 * 2. **Timestamp-only re-renders.** The narrative block embeds its
 *    ``generated_at`` in prose, so a regeneration that produced IDENTICAL
 *    text still changed bytes. ``NarrativeStableCache`` reuses the previous
 *    rendered block whenever the narrative text is unchanged — the embedded
 *    timestamp then honestly reports when this TEXT first appeared.
 *
 * 3. (In TurnObserver, using these helpers): **speaker-block presence
 *    flapping** on daemon-triggered turns — see the hook site.
 *
 * Safety posture: serving stale is bounded (``maxStaleMs``) and logged by
 * the caller. Every consumer of these blocks is advisory — the capability
 * list, directory digest, and narrative are hints; enforcement paths
 * (roster fail-closed refusal, role gates) never read them — so a bounded-
 * stale block is strictly better than a flapping one.
 */

export interface StickyEntry {
  text: string;
  /** When the value was last STORED (fresh render or re-anchor on failure). */
  at: number;
  /** When the value was last produced by a SUCCESSFUL render; null = never. */
  goodAt: number | null;
}

/**
 * TTL cache whose failure path re-serves the last successful value instead
 * of caching emptiness.
 *
 * - ``getFresh(now)`` — the cached text while within ``ttlMs`` of the last
 *   store, else null (caller re-renders).
 * - ``storeSuccess(text, now)`` — a successful render; "" is a VALID success
 *   (e.g. a bot with no skills) and replaces any last-good.
 * - ``storeFailure(now)`` — a failed render; returns the text to serve:
 *   the last-good value while it is younger than ``maxStaleMs``, else "".
 *   Re-anchors the TTL either way so a persistent fault is retried once per
 *   TTL window, not every turn.
 */
export class StickyBlockCache {
  private entry: StickyEntry | null = null;
  private readonly ttlMs: number;
  private readonly maxStaleMs: number;

  constructor(ttlMs: number, maxStaleMs: number) {
    this.ttlMs = ttlMs;
    this.maxStaleMs = maxStaleMs;
  }

  getFresh(now: number): string | null {
    if (this.entry && now - this.entry.at < this.ttlMs) return this.entry.text;
    return null;
  }

  storeSuccess(text: string, now: number): string {
    this.entry = { text, at: now, goodAt: now };
    return text;
  }

  storeFailure(now: number): string {
    if (this.entry && this.entry.goodAt !== null && now - this.entry.goodAt < this.maxStaleMs) {
      // Serve last-good; re-anchor the TTL so the next re-render attempt is
      // one TTL away (mirrors the old cache-the-failure behavior, minus the
      // presence flap).
      this.entry = { ...this.entry, at: now };
      return this.entry.text;
    }
    this.entry = { text: "", at: now, goodAt: this.entry?.goodAt ?? null };
    return "";
  }

  /** Age of the last successful render, or null if none. For log lines. */
  staleAgeMs(now: number): number | null {
    if (!this.entry || this.entry.goodAt === null) return null;
    return now - this.entry.goodAt;
  }
}

/**
 * Render the Home-narrative injection block. Pure — the byte contract with
 * the LLM (wrapper text quoted in the primary bot's AGENTS.md; keep in sync
 * with session_surface.py, see the call site's comment block).
 */
export function renderHomeNarrativeBlock(text: string, generatedAt: string): string {
  const lines: string[] = [
    "[CURRENT POD REPORT — shown to admin above this chat on the home page]",
    "This is the friendly summary the admin sees as a banner at the top of",
    "the Evolve admin home page right now. When admin references \"the",
    "report\", \"the banner\", or asks about something in it (\"what was that",
    "about Codex?\"), this is what they mean — answer from this text rather",
    "than punting.",
    "",
    text,
  ];
  if (generatedAt) {
    lines.push("");
    lines.push(
      `(Generated ${generatedAt}. May be moments older than the ` +
      "live pod state — for current numbers, prefer the pod-state tools.)"
    );
  }
  return lines.join("\n");
}

/**
 * Byte-stable wrapper around ``renderHomeNarrativeBlock``: identical
 * narrative TEXT re-renders to the IDENTICAL block, even when the cache
 * file's ``generated_at`` was bumped by a regeneration that produced the
 * same prose. The embedded timestamp then reports when this text FIRST
 * appeared — which is the honest reading of "Generated <ts>".
 */
export class NarrativeStableCache {
  private lastText: string | null = null;
  private lastBlock = "";

  render(text: string, generatedAt: string): string {
    if (text === this.lastText) return this.lastBlock;
    this.lastText = text;
    this.lastBlock = renderHomeNarrativeBlock(text, generatedAt);
    return this.lastBlock;
  }
}

// ── The block-order contract ─────────────────────────────────────────────────
//
// Everything above keeps ONE block byte-stable. This section fixes the order
// the blocks are concatenated in, which is the other half of the same problem.
//
// Motivation (internal/finding-cost-forensics-power-bot-2026-09-04.md §2, and
// internal/spec-context-observability-2026-07-30.md §"Prefix stability"): the
// combined string lands in the system prompt AHEAD of the whole conversation,
// so the earliest byte that changes is the point past which nothing can be
// re-read from cache. Ordering the blocks by how often they change therefore
// costs nothing and puts the churn as late as it can go. The pre-2026-09
// order opened with ``costDowngrade`` — the single most volatile block in the
// set, present on some runs and absent on the next — i.e. the worst available
// position for it.
//
// The contract, most-stable first:
//
//   1. capabilities   — 15-minute TTL; changes on skill install / integration
//                       config change, both of which usually restart the
//                       gateway anyway.
//   2. toolDiscipline — fixed per session KIND: the same bytes on every
//                       scheduled turn, absent on user turns, and its only
//                       change source is a plugin upgrade — the same source
//                       as ``capabilities`` and rarer than ``digest``'s
//                       3-minute TTL. Most-stable-first therefore puts it
//                       here, not in the tail: a block that never changes
//                       within a session's lifetime displaces nothing.
//                       Absence on user-turn sessions is handled by the
//                       presence rule, which emits no separator for it.
//   3. digest         — 3-minute TTL; changes when the roster changes.
//   4. narrative      — the Home report; regenerates on the pod's own
//                       schedule, byte-stable across a no-op regeneration
//                       (``NarrativeStableCache``).
//   5. speaker        — changes when the speaker changes (TAIL).
//   6. costDowngrade  — present only on runs a cost breaker re-routed (TAIL).
//
// ``HEAD`` (1-4) is the region a test can hold to byte-equality across a
// simulated gap; ``TAIL`` (5-6) is the region allowed to vary between two
// compiles. That split is the testable form of "anything that changes between
// turns lives in the tail".
//
// NOT claimed: that reordering alone converts a miss into a hit. Where the
// provider's cache breakpoints sit inside the assembled prompt is OpenClaw's
// business, not this plugin's, and Evolve does not fork OC to find out. What
// is claimed is narrower and sufficient: the bytes Evolve contributes are
// ordered so the stable ones are never displaced by a volatile one, and the
// ordering is a pinned contract rather than the order someone happened to
// type the array literal in.

/** The injection blocks, ordered most-stable first. The concatenation order. */
export const EVOLVE_PREFIX_BLOCK_ORDER = [
  "capabilities",
  "toolDiscipline",
  "digest",
  "narrative",
  "speaker",
  "costDowngrade",
] as const;

export type EvolvePrefixBlockName = (typeof EVOLVE_PREFIX_BLOCK_ORDER)[number];

/**
 * The suffix of :data:`EVOLVE_PREFIX_BLOCK_ORDER` allowed to change between
 * two compiles of the same session. Everything before it is the HEAD, and the
 * pin test asserts the head is byte-identical across a simulated gap.
 */
export const EVOLVE_PREFIX_TAIL_BLOCKS: readonly EvolvePrefixBlockName[] = [
  "speaker",
  "costDowngrade",
];

/**
 * Blocks keyed by name. Absent / empty / whitespace-only all mean "absent" —
 * ``joinBlocks`` and ``composeEvolvePrefix`` both test ``s.trim().length > 0``,
 * so a renderer that intermittently returns "  " contributes no block and no
 * separators rather than churning the prefix with invisible bytes. The doc
 * and the filter say the same thing on purpose: this comment used to promise
 * whitespace-only meant absent while the filter tested ``length > 0`` only.
 */
export type EvolvePrefixBlocks = Partial<Record<EvolvePrefixBlockName, string>>;

/** The presence rule, in one place: a block is present iff it has non-blank text. */
function isPresent(s: string | undefined): s is string {
  return typeof s === "string" && s.trim().length > 0;
}

/** The separator between two present blocks. */
const BLOCK_SEPARATOR = "\n\n";

export interface ComposedEvolvePrefix {
  /** The full string handed to ``appendSystemContext`` ("" when empty). */
  text: string;
  /** The stable region — every block before the first tail block. */
  head: string;
  /** The volatile region — the tail blocks. */
  tail: string;
  /** Names of the blocks that were present, in contract order. */
  present: EvolvePrefixBlockName[];
}

function joinBlocks(blocks: EvolvePrefixBlocks, names: readonly EvolvePrefixBlockName[]): string {
  return names
    .map((n) => blocks[n])
    .filter(isPresent)
    .join(BLOCK_SEPARATOR);
}

/**
 * Concatenate the injection blocks in contract order.
 *
 * Pure — no clock, no I/O, no module state — so the pin test can compile the
 * same inputs twice across a simulated twenty-minute gap and compare bytes.
 * The only behavior here is order and the separator; every block's own
 * content is decided by its renderer.
 */
export function composeEvolvePrefix(blocks: EvolvePrefixBlocks): ComposedEvolvePrefix {
  const headNames = EVOLVE_PREFIX_BLOCK_ORDER.filter(
    (n) => !EVOLVE_PREFIX_TAIL_BLOCKS.includes(n),
  );
  const head = joinBlocks(blocks, headNames);
  const tail = joinBlocks(blocks, EVOLVE_PREFIX_TAIL_BLOCKS);
  const present = EVOLVE_PREFIX_BLOCK_ORDER.filter((n) => isPresent(blocks[n]));
  const text = [head, tail].filter((s) => s.length > 0).join(BLOCK_SEPARATOR);
  return { text, head, tail, present };
}
