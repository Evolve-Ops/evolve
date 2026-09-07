/**
 * runPinnedSubagent — shared wrapper for the plugin's cheap-LLM subagent
 * call sites (LLMTierClassifier, SessionStruggleJudge,
 * PreflightIntentRouter, SessionSummarizer), all of which pin the
 * operator's classifierModel via the `model:` param.
 *
 * OC 2026.7.1-2 added authorization on provider/model overrides in
 * plugin subagent runs (verified against the installed gateway dist,
 * `createGatewaySubagentRuntime.run` in dist/server-plugins-*.js):
 *
 *   - REQUEST-SCOPED runs (a hook firing during a live gateway request —
 *     which is every call site above) honor the pin only when the
 *     request's CLIENT carries admin scope or
 *     `internal.allowModelOverride`. A Telegram/Slack channel client
 *     never does. The `plugins.entries.evolve.subagent.allowModelOverride`
 *     grant deploy.py writes is NOT consulted on this path.
 *   - FALLBACK-SCOPED runs (no request client) consult that config grant
 *     via `authorizeFallbackModelOverride`.
 *
 * So under OC 2026.7 every request-scoped pinned run throws
 * "provider/model override is not authorized for this plugin subagent
 * run." — which silently degraded all four call sites at once
 * (2026-07-31 fleet incident: tier classifier → keyword fallback,
 * struggle judge → AMBIGUOUS, preflight router → abstain, summarizer →
 * heuristic outcome).
 *
 * Strategy: try the pinned run; on an authorization rejection, log
 * LOUDLY once per process, remember the denial, and retry WITHOUT the
 * pin — the run proceeds on the bot's session-default model. That is a
 * cost regression the operator hears about, not a silent capability
 * loss. Subsequent calls skip the doomed pinned attempt entirely.
 *
 * Any non-authorization error propagates to the caller unchanged — each
 * call site keeps its own degradation path for genuine failures.
 */
interface MinimalLogger {
    info(msg: string): void;
    warn(msg: string): void;
    error?(msg: string): void;
}
export type EvolveSubagentTriggerKind = "summarizer" | "classifier";
/**
 * Map an Evolve subagent idempotencyKey ("evolve:<tag>:…") or the session
 * key OC derives from it ("agent:main:explicit:evolve:<tag>:…") to the
 * cost_event trigger_kind for that call site. Returns null for anything
 * that isn't a recognizable Evolve subagent key — callers treat null as
 * "not ours" and fall through to normal handling.
 */
export declare function classifyEvolveSubagentKey(key: unknown): EvolveSubagentTriggerKind | null;
export interface PinnedSubagentRunParams {
    idempotencyKey: string;
    message: string;
    model?: string;
    maxTurns?: number;
}
/** @internal test-only — reset the per-process denial memory. */
export declare function _resetSubagentPinDenialForTest(): void;
/** @internal exposed for diagnostics/tests. */
export declare function subagentPinDenied(): boolean;
/**
 * True iff `err` is OC's subagent model-override authorization
 * rejection (any of the reason strings the 2026.7 contract emits),
 * as opposed to a transient/infrastructure failure.
 */
export declare function isSubagentOverrideAuthError(err: unknown): boolean;
export declare function runPinnedSubagent(api: any, logger: MinimalLogger, params: PinnedSubagentRunParams): Promise<{
    runId: string;
}>;
export {};
//# sourceMappingURL=subagentRun.d.ts.map