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
