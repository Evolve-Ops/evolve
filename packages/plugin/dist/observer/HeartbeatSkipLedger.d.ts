/**
 * HeartbeatSkipLedger — the zero-cost turn record for a heartbeat or cron
 * that never reached a model.
 *
 * Decision: internal/decision-evolve-overhead-2026-09-07.md D-OH3 + D-OH5.
 * A saving that shows up only as *fewer rows in the turns file* is
 * indistinguishable from a heartbeat that silently died — the exact failure
 * this feature could cause. So every decision the due-check makes is written
 * down, skips and wakes alike, and the weekly receipt reads counts from here
 * rather than inferring them from an absence.
 *
 * ## Where it lands, and why not turns-<date>.jsonl
 *
 * ``{sharedDir}/{botId}/turns/heartbeat-decisions-<YYYY-MM-DD>.jsonl``.
 *
 * The obvious home would be the bot's ``turns-<date>.jsonl`` alongside real
 * turns. It is the wrong one. ``usage_analytics._find_turns_dirs`` probes
 * several candidate dirs per bot and takes **the first one that has data for
 * that date** — so on a bot whose real turns come from the OC turn-collector
 * in ``workspace/memory``, dropping skip rows into the shared dir would make
 * the shared file non-empty and shadow the real file, deleting every real
 * turn from every rollup. A distinct filename in the same (already
 * bot-writable, evolve-readable) directory keeps the data next to the turns
 * without ever colliding with the glob ``load_turns`` reads.
 *
 * ## Schema (schema_version 1)
 *
 * ```
 * {
 *   "schema_version": 1,
 *   "ts": "2026-09-07T14:00:03.123Z",
 *   "instance": "personal_bot",
 *   "source": "heartbeat" | "cron",
 *   "channel": "heartbeat" | "cron",
 *   "outcome": "skipped_nothing_due" | "woke_due",
 *   "model": null,                 // null, never "unknown": no model ran
 *   "provider": null,
 *   "input_tokens": 0, "output_tokens": 0,
 *   "cache_read_tokens": 0, "cache_write_tokens": 0,
 *   "cost": 0.0,                   // a KNOWN zero, hence 0 and not null
 *   "cost_source": "no_model_call",
 *   "session_id": "…" | null,
 *   "run_id": "…" | null,
 *   "conditions_evaluated": 4,
 *   "due_ids": ["inbox"],          // [] on a skip
 *   "conditions_sha256": "9f2c…",  // digest of the HEARTBEAT.json read
 *   "cron_job_id": null,           // the OC cron job id on a cron decision
 *   "reason": "nothing due (4 conditions checked)"
 * }
 * ```
 *
 * ``conditions_sha256`` is on every record because the file it digests lives
 * in the bot's own workspace: without it, a bot that rewrote its conditions
 * to quiet itself and a bot with a genuinely quiet week produce identical
 * ledgers. ``cron_job_id`` names which scope decided — a cron decision is
 * always made against that job's own conditions, never the heartbeat's.
 *
 * ``cost: 0.0`` is deliberate and is NOT the silent zero
 * docs/principle-tri-state-status.md forbids: no model call happened, so zero
 * is measured, not missing. ``cost_source`` says so out loud.
 */
export declare const DECISIONS_FILE_PREFIX = "heartbeat-decisions-";
export type HeartbeatOutcome = "skipped_nothing_due" | "woke_due";
export interface HeartbeatDecisionRecord {
    schema_version: 1;
    ts: string;
    instance: string;
    source: string;
    channel: string;
    outcome: HeartbeatOutcome;
    model: null;
    provider: null;
    input_tokens: 0;
    output_tokens: 0;
    cache_read_tokens: 0;
    cache_write_tokens: 0;
    cost: 0;
    cost_source: "no_model_call";
    session_id: string | null;
    run_id: string | null;
    conditions_evaluated: number;
    due_ids: string[];
    /** sha256 of the HEARTBEAT.json this decision was made from. */
    conditions_sha256: string | null;
    /** OC cron job id when the decision was made in a cron scope, else null. */
    cron_job_id: string | null;
    reason: string;
}
export interface BuildDecisionArgs {
    botId: string;
    trigger: string;
    outcome: HeartbeatOutcome;
    sessionId?: string | null;
    runId?: string | null;
    conditionsEvaluated: number;
    dueIds?: string[];
    conditionsSha256?: string | null;
    cronJobId?: string | null;
    reason: string;
    now?: Date;
}
/** Pure record builder — the shape the receipt and D-OH5's ledger read. */
export declare function buildDecisionRecord(args: BuildDecisionArgs): HeartbeatDecisionRecord;
export declare function decisionsFilePath(sharedDir: string, botId: string, day: string): string;
/**
 * Append one decision. Best-effort by contract: a ledger write must never be
 * the reason a heartbeat behaves differently, and on a pod where the turns
 * dir belongs to another gateway the append EACCESes forever — so that case
 * is quiet, and everything else is a single warning.
 */
export declare function appendDecision(sharedDir: string, record: HeartbeatDecisionRecord, logger?: {
    warn: (m: string) => void;
    debug?: (m: string) => void;
}): boolean;
//# sourceMappingURL=HeartbeatSkipLedger.d.ts.map