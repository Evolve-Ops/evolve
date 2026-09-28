/**
 * AppAttribution — run/session-scoped registry answering "which app did this
 * turn serve?" (AL-1.1, internal/design-app-attribution-2026-08-15.md §4–§7).
 *
 * Three EXPLICIT sources record here (one line each at their call sites):
 *   - ``expand_app``            (tools/ExpandAppTool.ts, on a daemon-confirmed hit)
 *   - script-integrity match    (integrity/AppIntegrityMiddleware.ts, registry.lookup)
 *   - Layer C trigger intercept (observer/TurnObserver._interceptManifestTrigger)
 * TurnObserver calls ``resolveForTurn`` once at annotation-build time and gets
 * the four ``app_*`` fields for the turn annotation (schema_version 5). The
 * SCHEDULED path (``recordScheduled``) is fed by ``apps/scheduledAttribution``
 * at ``before_agent_run`` (AL-1.2): the OC-cron map join and the AL-0.4
 * claim-file join.
 *
 * Shape deliberately mirrors ``util/senderRegistry.ts``: module-level bounded
 * Maps keyed on runId (per-turn signals) and sessionId (stickiness), FIFO
 * eviction + TTL so a long-running gateway can never grow them forever, and a
 * cooperative seam between independently-registered tools/middleware and the
 * observer without threading references through factory chains.
 *
 * Decision order (design §4, strongest first): scheduled > explicit >
 * inferred > none. Inferred is AL-1.9 — nothing records it yet, but the
 * ordering is pinned by the decision-table tests now so the classifier can
 * never override a deterministic signal later.
 *
 * Stickiness (design §5): a session becomes sticky to app X on its first
 * explicit signal; later signal-less turns resolve ``explicit``/``"sticky"``
 * until a different app's explicit signal flips the session or
 * ``STICKY_SIGNALLESS_TURN_LIMIT`` signal-less turns pass (the
 * inferred-disagreement early flip is AL-1.9). Scheduled sessions never flip.
 *
 * Fail-open to "no signal": attribution is read-only observation — nothing in
 * this module may ever throw into a turn, block a tool call, or change a tool
 * result. Any internal error resolves ``none`` and warns once per process per
 * reason (the TurnObserver warn-once pattern).
 */
export type AppAttributionGrade = "scheduled" | "explicit" | "inferred" | "none";
export interface AppAttributionResult {
    app_id: string | null;
    app_attribution: AppAttributionGrade;
    /** 1.0 for scheduled/explicit; null for none (design §3 — only ever
     *  fractional for the AL-1.9 inferred classifier). */
    app_confidence: number | null;
    /** "expand_app" | "script_middleware" | "layer_c" | "sticky" |
     *  "oc_cron_map" | "claim_file" | "scheduled" | null. */
    app_attribution_source: string | null;
}
interface AttributionLogger {
    warn(msg: string): void;
    debug(msg: string): void;
}
/** sharedDir/botId are only needed for the conflict ledger; everything else
 *  is pure in-memory state. Unconfigured (tests, exotic hosts) ⇒ conflict
 *  logging silently off, attribution itself unaffected. */
interface AttributionConfig {
    readonly sharedDir: string;
    readonly botId: string;
}
/** Design §5: stickiness ends after N signal-less turns (start N=6). Turns
 *  1..N after the last signal still resolve sticky; turn N+1 resolves none. */
export declare const STICKY_SIGNALLESS_TURN_LIMIT = 6;
export declare const CONFLICT_LEDGER_FILENAME = "app-attribution-conflicts.jsonl";
/** One-time wiring of the conflict-ledger destination + logger. Called from
 *  the plugin's register(); safe to call again (last call wins). */
export declare function configureAppAttribution(config: AttributionConfig, logger: AttributionLogger): void;
/**
 * Record an EXPLICIT attribution signal for the current run (and make the
 * session sticky to the app when the caller knows the sessionId — Layer C's
 * before_model_resolve ctx may not carry one; ``resolveForTurn`` re-stamps
 * stickiness from the run signal at annotation time, so a null sessionId here
 * only defers stickiness, never loses it).
 *
 * Within a run, the LAST signal wins (design §4.2); a differing predecessor
 * is appended to the per-bot conflict ledger for calibration. Never throws.
 */
export declare function recordExplicit(runId: string | null | undefined, sessionId: string | null | undefined, appId: string, source: string): void;
/**
 * Stamp a session as serving one scheduled app (design §4.1). Called from
 * ``apps/scheduledAttribution.ts`` at ``before_agent_run`` (AL-1.2) with the
 * join that matched — ``source`` is ``"oc_cron_map"`` or ``"claim_file"``
 * (distinct for calibration; defaults to ``"scheduled"`` for direct callers).
 * Scheduled beats explicit and the session never flips or expires on
 * signal-less turns. Never throws.
 */
export declare function recordScheduled(sessionId: string, appId: string, source?: string): void;
/**
 * Resolve the attribution for one completed turn. Called once per turn from
 * TurnObserver's annotation build. Decision order (design §4): scheduled >
 * explicit (this run's signal, else sticky) > none — inferred slots between
 * explicit and none when AL-1.9 adds it. Never throws; any internal error
 * resolves ``none`` (warn-once).
 */
export declare function resolveForTurn(runId: string | null | undefined, sessionId: string | null | undefined): AppAttributionResult;
/** Test helper — clear all module state (registries, warn-once set, config).
 *  Tests in different files share module state; this prevents bleed-over. */
export declare function _resetForTests(): void;
export {};
//# sourceMappingURL=AppAttribution.d.ts.map