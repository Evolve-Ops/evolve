/**
 * PreflightCallCap — a ceiling on preflight classifications that does not
 * depend on understanding why they are happening.
 *
 * The 2026-09-08 loop (internal/finding-tier-router-self-call-loop-2026-09-07.md)
 * ran because `before_model_resolve` fired for the preflight router's OWN
 * subagent session and nothing in the hook noticed. The fix for THAT is the
 * self-call guard in TurnObserver — two independent checks, the session-key
 * shape and the prompt-borne sentinel.
 *
 * This is the backstop for the loop we have not met yet. Both halves of the
 * guard are recognition: they answer "is this call mine?", and a future OC
 * change could make Evolve's own call unrecognisable again — that is exactly
 * what 2026.9.2 did to the assumption before it. A cap answers a different
 * question, "how many of these have I already done?", which stays true
 * whatever the caller turns out to be. It is deliberately dumb for that
 * reason.
 *
 * Two ceilings, because the loop had two shapes:
 *
 *   - **One classification per runId.** A preflight decision is a property of
 *     a turn. OC re-fires `before_model_resolve` for the same runId on
 *     provider retries (see TurnObserver's `_evoHandledRuns`), and the
 *     recursion re-entered while the parent turn was still open, so a second
 *     classification under one runId is already a bug when it is not a loop.
 *
 *   - **Ten per minute per session.** The runId ceiling alone would not have
 *     stopped 2026-09-08: each nested call arrived under its own fresh runId.
 *     What the log shows is ten fires in the first 500 ms on one session key
 *     — so the per-session rate is the ceiling that would actually have cut
 *     it, and 10/min is comfortably above any real conversation (the steady
 *     state on the same bot was 12 hook fires a DAY).
 *
 * Over either ceiling the caller skips the router and lets the turn proceed
 * unrouted — the same outcome as the router abstaining, which is a path the
 * turn already has. Nothing about which model answers a user turn changes:
 * an unrouted turn is answered by the bot's configured primary.
 */
/** One line for the operator, emitted only on the transition into capped state. */
export interface CapVerdict {
    /** False ⇒ the caller must skip the preflight classification. */
    admitted: boolean;
    /**
     * Non-null exactly once per session per window, on the fire that first
     * trips the cap. A loop produces thousands of refusals and one log line;
     * the refusal itself is not worth a line each, which is the mistake the
     * diag line in this same hook already made.
     */
    logLine: string | null;
}
export declare class PreflightCallCap {
    /** Ceiling per session per rolling minute. */
    static readonly PER_MINUTE_PER_SESSION = 10;
    static readonly WINDOW_MS = 60000;
    /**
     * Bound on remembered runIds/sessions. Matches the `_evoHandledRuns`
     * convention in TurnObserver — a cap whose own memory can grow without
     * limit is a second incident waiting behind the first.
     */
    static readonly MAX_TRACKED = 1024;
    private readonly _seenRunIds;
    /** sessionKey → classification timestamps inside the current window. */
    private readonly _sessionHits;
    /** sessionKey → window start already reported, so the line is logged once. */
    private readonly _reported;
    /**
     * Record an attempt and say whether it may proceed.
     *
     * Call this ONLY where a classification would actually happen — it has the
     * side effect of counting. `runId` may be absent (OC does not populate it
     * on every surface); the per-session ceiling still applies, which is the
     * one that catches a loop.
     */
    admit(runId: string | null | undefined, sessionKey: string, now?: number): CapVerdict;
    /** Refuse, and decide whether this refusal is the one that gets logged. */
    private _verdict;
    private _evict;
    private _evictMap;
    /** @internal test-only */
    _resetForTest(): void;
}
//# sourceMappingURL=preflightCallCap.d.ts.map