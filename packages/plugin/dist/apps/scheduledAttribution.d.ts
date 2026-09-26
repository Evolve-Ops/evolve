/**
 * scheduledAttribution — AL-1.2's before_agent_run capture: join a
 * scheduled (cron-driven) turn to the app it serves and stamp the session
 * via ``AppAttribution.recordScheduled`` (design-app-attribution-2026-08-15
 * §4.1, resolved per AL-0.4). Two lanes, most-specific first:
 *
 *   Lane B — claim file. A wrapper that shells out to ``openclaw agent``
 *   mints a UUIDv4 session id, writes
 *   ``{sharedDir}/{botId}/app-runs/<uuid>.json`` = ``{app_id, label, ts}``,
 *   and passes ``--session-id <uuid>`` (the AL-0.4 contract, probe-verified).
 *   When the claim exists for this turn's sessionId we record it
 *   (source ``"claim_file"``) and consume the file. Claims are swept after
 *   24h so an orphaned claim (wrapper died before the turn ran) can never
 *   pile up. No live producer writes claims yet — the Python writer ships
 *   unwired in ``applications/install_helpers.py``.
 *
 *   Lane A — OC-native cron join. The gateway threads the cron job id
 *   through the hook ctx session key: observed live (OC 2026.7, canary
 *   proof) as `` agent:<agentId>:cron:<job.id>:run:<runId> ``; the bare
 *   `` cron:<job.id> `` form from the store internals (AL-0.4) is tolerated
 *   too — extraction is by exact ``cron`` token, not prefix.
 *   We resolve job id → job NAME from the bot's own cron store —
 *   ``~/.openclaw/cron/jobs.json`` when present (older OC; mtime-cached),
 *   else the SQLite store current OC migrated to
 *   (``~/.openclaw/state/openclaw.sqlite`` ``cron_jobs``, discovered live on
 *   the 2026-08-15 canary proof: at gateway start jobs.json is imported and
 *   renamed ``jobs.json.migrated``, so it no longer exists at runtime). The
 *   SQLite read is same-user, read-only, via ``node:sqlite`` (node ≥22.13;
 *   unavailable → this lane records nothing). Then name → app_id from
 *   ``{sharedDir}/{botId}/app-cron-map.json`` (written at deploy time by the
 *   ``_merge_cron_entries`` call site, keyed by name because Evolve owns no
 *   stable job id at materialization). Record source ``"oc_cron_map"``.
 *
 * No match at any step → record NOTHING (the turn stays honestly "none");
 * this module never guesses. Same fail-open contract as AppAttribution:
 * observation only — never throws into the turn, warns once per process per
 * reason.
 */
/** Distinct source strings (design §3 — kept apart for calibration). */
export declare const SOURCE_OC_CRON_MAP = "oc_cron_map";
export declare const SOURCE_CLAIM_FILE = "claim_file";
export declare const CLAIM_DIR_NAME = "app-runs";
export declare const CRON_MAP_FILENAME = "app-cron-map.json";
/** Extract the cron job id from a gateway session key. Live OC 2026.7 keys a
 *  cron run `` agent:<agentId>:cron:<job.id>:run:<runId> `` (observed on the
 *  canary proof); older/internal forms use bare `` cron:<job.id> ``. Token
 *  scan (exact ``cron`` segment, take the next) covers both without ever
 *  matching a job id that merely STARTS with "cron" (e.g. the
 *  ``cron-migrated-*`` ids the SQLite migration mints). */
export declare function cronJobIdFromSessionKey(key: string): string | null;
interface AttributionLogger {
    warn(msg: string): void;
    debug(msg: string): void;
}
export interface ScheduledAttributionOpts {
    readonly sharedDir: string;
    readonly botId: string;
    readonly logger?: AttributionLogger;
    /** Test seam — defaults to ``os.homedir()`` (the bot's own home). */
    readonly homeDir?: string;
}
/**
 * The before_agent_run capture. TurnObserver calls this (call-site lines
 * only — the module boundary rule) on every turn at every active tier;
 * non-scheduled turns exit on the first prefix/existence check. Never
 * throws.
 */
export declare function captureScheduledAttribution(event: unknown, ctx: unknown, opts: ScheduledAttributionOpts): void;
/** Test helper — clear the parse/name caches, sweep timer, and warn-once set. */
export declare function _resetScheduledAttributionForTests(): void;
export {};
//# sourceMappingURL=scheduledAttribution.d.ts.map