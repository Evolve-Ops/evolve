/**
 * AppAttribution — run/session-scoped registry answering "which app did this
 * turn serve?" (AL-1.1, docs/design-app-attribution-2026-08-15.md §4–§7).
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
import * as fs from "node:fs";
import * as path from "node:path";
/** Per-run explicit signals (last writer wins; conflicts ledgered). ~1 entry
 *  per concurrent active turn — 1024 leaves generous headroom. */
const MAX_RUN_ENTRIES = 1024;
/** A run signal only needs to survive from the tool call / intercept to the
 *  same turn's agent_end annotation build. 30 min covers even pathologically
 *  long agent turns; after that a stale runId must not resolve. */
const RUN_TTL_MS = 30 * 60_000;
const MAX_SESSION_ENTRIES = 2048;
/** Stickiness is a within-conversation convenience, not durable state (no
 *  disk write by design §5); 12h outlives any realistic session gap while
 *  guaranteeing an abandoned session can't stay attributed forever. */
const SESSION_TTL_MS = 12 * 60 * 60_000;
/** Design §5: stickiness ends after N signal-less turns (start N=6). Turns
 *  1..N after the last signal still resolve sticky; turn N+1 resolves none. */
export const STICKY_SIGNALLESS_TURN_LIMIT = 6;
/** Conflict ledger stops appending past this size — it is a calibration
 *  input, not an audit log, and must never fill a disk (bounded, best-effort). */
const MAX_CONFLICT_LEDGER_BYTES = 1_000_000;
export const CONFLICT_LEDGER_FILENAME = "app-attribution-conflicts.jsonl";
const _runSignals = new Map();
const _sessions = new Map();
const _warnedReasons = new Set();
let _config = null;
let _logger = null;
/** One-time wiring of the conflict-ledger destination + logger. Called from
 *  the plugin's register(); safe to call again (last call wins). */
export function configureAppAttribution(config, logger) {
    _config = config;
    _logger = logger;
}
/** Warn once per process per reason — an attribution bug must be visible
 *  without ever spamming per-turn logs (TurnObserver drift-log pattern). */
function warnOnce(reason, err) {
    if (_warnedReasons.has(reason))
        return;
    _warnedReasons.add(reason);
    try {
        _logger?.warn(`Evolve app-attribution: ${reason} — resolving "none" (warns once per ` +
            `process per reason): ${err}`);
    }
    catch {
        /* logging must never throw out of the hot path */
    }
}
function evictOldest(map, max) {
    if (map.size < max)
        return;
    const oldestKey = map.keys().next().value;
    if (oldestKey !== undefined)
        map.delete(oldestKey);
}
function getFreshRunSignal(runId) {
    const rec = _runSignals.get(runId);
    if (!rec)
        return null;
    if (Date.now() - rec.capturedAt > RUN_TTL_MS) {
        _runSignals.delete(runId);
        return null;
    }
    return rec;
}
function getFreshSession(sessionId) {
    const rec = _sessions.get(sessionId);
    if (!rec)
        return null;
    if (Date.now() - rec.updatedAt > SESSION_TTL_MS) {
        _sessions.delete(sessionId);
        return null;
    }
    return rec;
}
/** Best-effort, bounded append to the per-bot conflict ledger
 *  ({sharedDir}/{botId}/app-attribution-conflicts.jsonl). Ids + sources only,
 *  never message content. Any IO error is swallowed — a calibration write
 *  must never affect the turn. */
function appendConflict(record) {
    try {
        if (!_config)
            return;
        const dir = path.join(_config.sharedDir, _config.botId);
        const file = path.join(dir, CONFLICT_LEDGER_FILENAME);
        try {
            if (fs.statSync(file).size > MAX_CONFLICT_LEDGER_BYTES)
                return;
        }
        catch {
            /* missing file — first append creates it */
        }
        fs.mkdirSync(dir, { recursive: true });
        fs.appendFileSync(file, JSON.stringify(record) + "\n");
    }
    catch (err) {
        try {
            _logger?.debug(`Evolve app-attribution: conflict-ledger append failed (continuing): ${err}`);
        }
        catch {
            /* logging must never throw out of the hot path */
        }
    }
}
/** Establish or refresh explicit stickiness for a session (design §5).
 *  Scheduled sessions never flip; a different app's explicit signal flips a
 *  non-scheduled session; the same app's signal resets the signal-less count. */
function stickExplicit(sessionId, appId) {
    if (!sessionId)
        return;
    const existing = getFreshSession(sessionId);
    if (existing) {
        if (existing.scheduled)
            return; // single-app by construction — never flip
        existing.appId = appId;
        existing.signallessTurns = 0;
        existing.updatedAt = Date.now();
        return;
    }
    evictOldest(_sessions, MAX_SESSION_ENTRIES);
    _sessions.set(sessionId, {
        appId,
        scheduled: false,
        signallessTurns: 0,
        updatedAt: Date.now(),
    });
}
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
export function recordExplicit(runId, sessionId, appId, source) {
    try {
        if (typeof appId !== "string" || !appId.trim())
            return;
        const id = appId.trim();
        if (runId) {
            const prior = getFreshRunSignal(runId);
            if (prior && prior.appId !== id) {
                appendConflict({
                    ts: new Date().toISOString(),
                    bot_id: _config?.botId ?? null,
                    run_id: runId,
                    session_id: sessionId ?? null,
                    prior_app_id: prior.appId,
                    prior_source: prior.source,
                    app_id: id,
                    source,
                });
            }
            if (!prior)
                evictOldest(_runSignals, MAX_RUN_ENTRIES);
            _runSignals.set(runId, { appId: id, source, capturedAt: Date.now() });
        }
        stickExplicit(sessionId, id);
    }
    catch (err) {
        warnOnce("recordExplicit failed", err);
    }
}
/**
 * Stamp a session as serving one scheduled app (design §4.1). Called from
 * ``apps/scheduledAttribution.ts`` at ``before_agent_run`` (AL-1.2) with the
 * join that matched — ``source`` is ``"oc_cron_map"`` or ``"claim_file"``
 * (distinct for calibration; defaults to ``"scheduled"`` for direct callers).
 * Scheduled beats explicit and the session never flips or expires on
 * signal-less turns. Never throws.
 */
export function recordScheduled(sessionId, appId, source = "scheduled") {
    try {
        if (!sessionId || typeof appId !== "string" || !appId.trim())
            return;
        evictOldest(_sessions, MAX_SESSION_ENTRIES);
        _sessions.set(sessionId, {
            appId: appId.trim(),
            scheduled: true,
            scheduledSource: typeof source === "string" && source ? source : "scheduled",
            signallessTurns: 0,
            updatedAt: Date.now(),
        });
    }
    catch (err) {
        warnOnce("recordScheduled failed", err);
    }
}
const NONE = Object.freeze({
    app_id: null,
    app_attribution: "none",
    app_confidence: null,
    app_attribution_source: null,
});
/**
 * Resolve the attribution for one completed turn. Called once per turn from
 * TurnObserver's annotation build. Decision order (design §4): scheduled >
 * explicit (this run's signal, else sticky) > none — inferred slots between
 * explicit and none when AL-1.9 adds it. Never throws; any internal error
 * resolves ``none`` (warn-once).
 */
export function resolveForTurn(runId, sessionId) {
    try {
        const session = sessionId ? getFreshSession(sessionId) : null;
        if (session?.scheduled) {
            session.updatedAt = Date.now();
            return {
                app_id: session.appId,
                app_attribution: "scheduled",
                app_confidence: 1.0,
                app_attribution_source: session.scheduledSource ?? "scheduled",
            };
        }
        const run = runId ? getFreshRunSignal(runId) : null;
        if (run) {
            // Re-stamp stickiness with the resolve-time sessionId — the record
            // site may not have known it (Layer C's before_model_resolve ctx).
            stickExplicit(sessionId, run.appId);
            return {
                app_id: run.appId,
                app_attribution: "explicit",
                app_confidence: 1.0,
                app_attribution_source: run.source,
            };
        }
        if (session) {
            session.signallessTurns += 1;
            if (session.signallessTurns > STICKY_SIGNALLESS_TURN_LIMIT) {
                _sessions.delete(String(sessionId));
                return NONE;
            }
            session.updatedAt = Date.now();
            return {
                app_id: session.appId,
                app_attribution: "explicit",
                app_confidence: 1.0,
                app_attribution_source: "sticky",
            };
        }
        return NONE;
    }
    catch (err) {
        warnOnce("resolveForTurn failed", err);
        return NONE;
    }
}
/** Test helper — clear all module state (registries, warn-once set, config).
 *  Tests in different files share module state; this prevents bleed-over. */
export function _resetForTests() {
    _runSignals.clear();
    _sessions.clear();
    _warnedReasons.clear();
    _config = null;
    _logger = null;
}
//# sourceMappingURL=AppAttribution.js.map