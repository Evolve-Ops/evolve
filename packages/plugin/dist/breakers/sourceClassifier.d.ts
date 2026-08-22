/**
 * Source-classification helpers for the L1 cost breaker veto.
 *
 * MIRRORS packages/analyzer/breakers/classify.py::is_auto_source() exactly.
 * The Python side is the spec; this is the TS port. If the two ever drift,
 * Python wins. Any changes here must be mirrored there and vice versa.
 *
 * The L1 cost breaker blocks auto-source turns (heartbeat, cron, scheduler-
 * spawned) but lets human user turns through unchanged. Spec:
 *   docs/spec-circuit-breakers-2026-05-21.md §5.2
 */
/**
 * Is this channel value one of the auto-activity tells?
 *
 * Exported so callers that REPAIR a channel value (see
 * util/channelIdentity.ts) can prove they never overwrite a tell — the
 * tells are load-bearing for the L1 veto and the heartbeat re-tag, and
 * silently replacing one with a messaging-platform name would fail the
 * breaker open.
 */
export declare function isAutoChannelTell(channel: string | null | undefined): boolean;
/**
 * Return true if this turn would be vetoed by an L1 cost breaker.
 *
 * Fail-open semantics: missing or unknown source/channel returns false
 * (the turn is allowed through). Better to let a possible-auto turn
 * cost a few cents than to accidentally block a possible-user turn.
 *
 * Pure function — no I/O, no side effects. Safe to call on every turn.
 */
export declare function isAutoSource(opts: {
    source?: string | null;
    channel?: string | null;
}): boolean;
/**
 * Should the per-turn `source` field be re-tagged to "heartbeat"?
 *
 * Workaround for openclaw/openclaw#84825: OC loses `isHeartbeat=true`
 * across follow-up turns in a heartbeat session. The channel field
 * stays "heartbeat" on sub-runs but the trigger/source drifts to
 * "user"/"human". Re-tagging keeps the per-turn JSONL `source` field
 * truthful regardless of what OC's runtime reports — without this,
 * the Usage page's "By Source" rollup misattributes heartbeat retry
 * storms as legitimate user demand. See the 2026-05-21 security_bot repro
 * in docs/incident-cost-alerting-blackout-2026-05-20.md.
 *
 * Returns true only when ALL of the following hold:
 *   - The turn's channel is "heartbeat" (heartbeat-channel session)
 *   - The current source is NOT already "heartbeat" (idempotent)
 *   - An earlier turn in the same session was observed with
 *     source=="heartbeat" (fail-safe: a session never triggered by
 *     heartbeat is never re-tagged, even if its channel reads
 *     "heartbeat" for some other reason)
 *
 * Pure function — no I/O, no side effects.
 */
export declare function shouldRetagHeartbeatSource(opts: {
    channel?: string | null;
    currentSource?: string | null;
    sessionTriggeredByHeartbeat: boolean;
}): boolean;
//# sourceMappingURL=sourceClassifier.d.ts.map