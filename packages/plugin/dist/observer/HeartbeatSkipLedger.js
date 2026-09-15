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
import * as fs from "node:fs";
import * as path from "node:path";
export const DECISIONS_FILE_PREFIX = "heartbeat-decisions-";
/** Pure record builder — the shape the receipt and D-OH5's ledger read. */
export function buildDecisionRecord(args) {
    const trigger = (args.trigger || "heartbeat").toLowerCase();
    return {
        schema_version: 1,
        ts: (args.now ?? new Date()).toISOString(),
        instance: args.botId,
        source: trigger,
        channel: trigger,
        outcome: args.outcome,
        model: null,
        provider: null,
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        cost: 0,
        cost_source: "no_model_call",
        session_id: args.sessionId ? String(args.sessionId) : null,
        run_id: args.runId ? String(args.runId) : null,
        conditions_evaluated: args.conditionsEvaluated,
        due_ids: args.dueIds ?? [],
        conditions_sha256: args.conditionsSha256 ?? null,
        cron_job_id: args.cronJobId ?? null,
        reason: args.reason,
    };
}
export function decisionsFilePath(sharedDir, botId, day) {
    return path.join(sharedDir, botId, "turns", `${DECISIONS_FILE_PREFIX}${day}.jsonl`);
}
/**
 * Append one decision. Best-effort by contract: a ledger write must never be
 * the reason a heartbeat behaves differently, and on a pod where the turns
 * dir belongs to another gateway the append EACCESes forever — so that case
 * is quiet, and everything else is a single warning.
 */
export function appendDecision(sharedDir, record, logger) {
    const day = record.ts.slice(0, 10);
    const target = decisionsFilePath(sharedDir, record.instance, day);
    try {
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.appendFileSync(target, JSON.stringify(record) + "\n", { mode: 0o644 });
        return true;
    }
    catch (err) {
        const code = err?.code;
        if (code === "EACCES" || code === "EPERM") {
            logger?.debug?.(`Evolve heartbeat: decisions ledger for ${record.instance} is owned by ` +
                `another gateway, skipping`);
            return false;
        }
        logger?.warn(`Evolve heartbeat: failed to write decisions ledger: ${err}`);
        return false;
    }
}
//# sourceMappingURL=HeartbeatSkipLedger.js.map