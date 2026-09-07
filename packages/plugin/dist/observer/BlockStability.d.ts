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
export declare class StickyBlockCache {
    private entry;
    private readonly ttlMs;
    private readonly maxStaleMs;
    constructor(ttlMs: number, maxStaleMs: number);
    getFresh(now: number): string | null;
    storeSuccess(text: string, now: number): string;
    storeFailure(now: number): string;
    /** Age of the last successful render, or null if none. For log lines. */
    staleAgeMs(now: number): number | null;
}
/**
 * Render the Home-narrative injection block. Pure — the byte contract with
 * the LLM (wrapper text quoted in the primary bot's AGENTS.md; keep in sync
 * with session_surface.py, see the call site's comment block).
 */
export declare function renderHomeNarrativeBlock(text: string, generatedAt: string): string;
/**
 * Byte-stable wrapper around ``renderHomeNarrativeBlock``: identical
 * narrative TEXT re-renders to the IDENTICAL block, even when the cache
 * file's ``generated_at`` was bumped by a regeneration that produced the
 * same prose. The embedded timestamp then reports when this text FIRST
 * appeared — which is the honest reading of "Generated <ts>".
 */
export declare class NarrativeStableCache {
    private lastText;
    private lastBlock;
    render(text: string, generatedAt: string): string;
}
//# sourceMappingURL=BlockStability.d.ts.map