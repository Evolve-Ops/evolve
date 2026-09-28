/**
 * In-turn cost checkpoint — D-CC1..4 reach inside one run (D-CS12, ratified
 * 2026-09-22 on the $65.98 / 304-call turn in internal/incident-post-mortem-
 * 2026-09-20-image-turn-cache-thrash-and-poll-loop.md). Damage control; the
 * structural fixes (no-progress veto, image-turn cache thrash) still come first.
 *
 * At ``inTurnCheckpointUsd`` of recorded spend in one run, the ONE
 * before_tool_call gate (ToolCallGate) vetoes the next tool call with a fixed
 * message and every later one with a one-liner, so the run ends on the
 * model's next reply — held, never degraded, no retry (D-CC2). The owner
 * notice (CostCheckpoint.ts, deterministic) is appended to that reply once
 * per run via ``reply_payload_sending`` (the only outbound hook carrying the
 * runId) — the conversation, not the notify path, which is broken until
 * ``breaker-notify-survives-openclaw-config-validation`` lands. An owner's
 * "continue" raises the next run's checkpoint by D-CC3's +50 %, ledgered.
 *
 * Positive evidence only (D-CS7): no runId → ``unknown``, allowed. Spend is
 * catalog-priced (``estimateCost``; D-CS2 not landed), so it is "recorded".
 * Writes {sharedDir}/breakers/<bot>/in-turn-checkpoints.jsonl (trip / notice /
 * continue rows) and in-turn-checkpoint-status.json (the health control's
 * armed + liveness counters).
 */
import * as fs from "fs";
import * as path from "path";
import { getSender } from "../util/senderRegistry.js";
import { resolveSpeakerRole } from "../util/roleResolver.js";
import { classifyCheckpointAnswer, CHECKPOINT_INCREMENT_FRACTION, renderInTurnCheckpointMessage, } from "./CostCheckpoint.js";
export const IN_TURN_CHECKPOINT_DEFAULT_USD = 5;
/** Config can raise the checkpoint, never lower it below this. */
export const IN_TURN_CHECKPOINT_FLOOR_USD = 1;
export const IN_TURN_PRICE_SOURCE = "catalog";
/** Mirrors cost_checkpoint_bot_routes.OWNER_ROLES. */
const OWNER_ROLES = new Set(["admin", "primary_user"]);
const RUN_IDLE_MS = 60 * 60_000;
const ENDED_KEEP_MS = 5 * 60_000;
const STATUS_THROTTLE_MS = 60_000;
/** ``inTurnCheckpointUsd`` from plugin config; below the floor → refused. */
export function resolveInTurnCheckpointUsd(pluginConfig) {
    const raw = pluginConfig.inTurnCheckpointUsd;
    if (raw === undefined || raw === null)
        return { usd: IN_TURN_CHECKPOINT_DEFAULT_USD, warning: null };
    if (typeof raw === "number" && Number.isFinite(raw) && raw >= IN_TURN_CHECKPOINT_FLOOR_USD) {
        return { usd: raw, warning: null };
    }
    return {
        usd: IN_TURN_CHECKPOINT_DEFAULT_USD,
        warning: `Evolve in-turn checkpoint: inTurnCheckpointUsd=${JSON.stringify(raw)} refused — ` +
            `it must be a number ≥ $${IN_TURN_CHECKPOINT_FLOOR_USD.toFixed(2)}; using ` +
            `$${IN_TURN_CHECKPOINT_DEFAULT_USD.toFixed(2)}.`,
    };
}
function money(n) {
    return `$${n.toFixed(2)}`;
}
/** The first veto's reason — the model reads it in place of the tool result. */
export function renderInTurnVeto(spendUsd) {
    return (`Evolve checkpoint: this turn has spent ${money(spendUsd)} (recorded) — reply to ` +
        `the user now with what you have and say what is unfinished. The owner can continue it.`);
}
/** Every later veto in the same run. */
export const IN_TURN_VETO_REPEAT = "Evolve checkpoint: tool calls are stopped for this turn — reply to the user now.";
/** Per-run spend + the decision; singleton ``inTurnCheckpoint``, fed by
 *  TurnObserver's llm_output and read by ToolCallGate's before_tool_call. */
export class InTurnCheckpoint {
    runs = new Map();
    /** Sessions whose last run tripped — the only place "continue" means this. */
    trippedSessions = new Map();
    sharedDir = "";
    botId = "";
    thresholdUsd = IN_TURN_CHECKPOINT_DEFAULT_USD;
    registered = false;
    logger = null;
    counters = {
        spend_runs: 0, multi_call_runs: 0, evaluated_runs: 0, unknown_evaluations: 0, zero_cost_calls: 0,
    };
    lastStatusMs = 0;
    warnedUnknown = false;
    configure(opts) {
        this.sharedDir = opts.sharedDir;
        this.botId = opts.botId;
        this.thresholdUsd = Math.max(opts.thresholdUsd, IN_TURN_CHECKPOINT_FLOOR_USD);
        this.registered = opts.registered;
        this.logger = opts.logger ?? null;
        this.writeStatus(new Date(), true);
    }
    run(runId, sessionId, now) {
        let r = this.runs.get(runId);
        if (!r) {
            r = {
                sessionId, spendUsd: 0, llmCalls: 0, toolCalls: 0, thresholdUsd: this.thresholdUsd,
                tripped: false, tripSpendUsd: 0, tripLlmCalls: 0, noticePending: false, endedMs: null, lastSeenMs: now,
            };
            this.runs.set(runId, r);
            this.prune(now);
        }
        if (sessionId && !r.sessionId)
            r.sessionId = sessionId;
        r.lastSeenMs = now;
        return r;
    }
    /** One llm_output's catalog-priced cost, keyed by run. */
    recordCost(runId, sessionId, costUsd, now = new Date()) {
        if (!runId)
            return;
        const r = this.run(String(runId), sessionId, now.getTime());
        r.llmCalls += 1;
        if (r.llmCalls === 1)
            this.counters.spend_runs += 1;
        if (r.llmCalls === 2)
            this.counters.multi_call_runs += 1; // a tool call sat between them
        if (Number.isFinite(costUsd) && costUsd > 0)
            r.spendUsd += costUsd;
        else
            this.counters.zero_cost_calls += 1; // estimateCost's 0 = "could not price"
        this.writeStatus(now);
    }
    /** The before_tool_call decision. */
    evaluate(runId, sessionId, now = new Date()) {
        if (!runId) {
            this.counters.unknown_evaluations += 1;
            if (!this.warnedUnknown && this.logger) {
                this.warnedUnknown = true;
                this.logger.info("Evolve in-turn checkpoint: a tool call arrived without a runId — unknown, allowed.");
            }
            return { kind: "unknown" };
        }
        const r = this.runs.get(String(runId));
        if (!r)
            return { kind: "allow", spendUsd: 0 };
        r.toolCalls += 1;
        r.lastSeenMs = now.getTime();
        if (r.toolCalls === 1)
            this.counters.evaluated_runs += 1;
        if (r.tripped)
            return { kind: "veto", first: false, spendUsd: r.tripSpendUsd, reason: IN_TURN_VETO_REPEAT };
        if (r.spendUsd < r.thresholdUsd)
            return { kind: "allow", spendUsd: r.spendUsd };
        r.tripped = true;
        r.tripSpendUsd = r.spendUsd;
        r.tripLlmCalls = r.llmCalls;
        r.noticePending = true;
        const sid = r.sessionId ?? sessionId;
        if (sid)
            this.trippedSessions.set(sid, now.getTime());
        this.append({
            event: "checkpoint_in_turn", ts: now.toISOString(), run_id: String(runId), session: sid,
            spend_usd: round6(r.spendUsd), threshold_usd: r.thresholdUsd, llm_calls: r.llmCalls,
            tool_calls: r.toolCalls, price_source: IN_TURN_PRICE_SOURCE, spend_basis: "recorded",
        });
        this.writeStatus(now, true);
        this.logger?.warn(`Evolve in-turn checkpoint TRIPPED bot=${this.botId} run=${String(runId).slice(0, 8)} ` +
            `recorded=${money(r.spendUsd)} threshold=${money(r.thresholdUsd)} llm_calls=${r.llmCalls} tool_calls=${r.toolCalls}`);
        return { kind: "veto", first: true, spendUsd: r.spendUsd, reason: renderInTurnVeto(r.spendUsd) };
    }
    /** Once per tripped run: the owner notice's facts; records the delivery. */
    takeOwnerNotice(runId, now = new Date()) {
        if (!runId)
            return null;
        const r = this.runs.get(String(runId));
        if (!r || !r.noticePending)
            return null;
        r.noticePending = false;
        this.append({
            event: "owner_notice", ts: now.toISOString(), run_id: String(runId), session: r.sessionId,
            spend_usd: round6(r.tripSpendUsd), delivery: "conversation",
        });
        return {
            spendUsd: r.tripSpendUsd, thresholdUsd: r.thresholdUsd, llmCalls: r.tripLlmCalls,
            toolCalls: r.toolCalls, incrementUsd: this.incrementUsd(),
        };
    }
    /** D-CC3 grammar: +50 % of the BASE checkpoint. */
    incrementUsd() {
        return Math.round(this.thresholdUsd * CHECKPOINT_INCREMENT_FRACTION * 100) / 100;
    }
    /** before_agent_run: an owner's "continue" after a trip on this session
     *  raises THIS run's checkpoint; a non-owner's is an ordinary message. */
    noteTurnStart(runId, sessionId, message, now = new Date()) {
        if (!runId || !sessionId || !this.trippedSessions.has(sessionId))
            return;
        if (classifyCheckpointAnswer(message) !== "continue")
            return;
        const sender = getSender(runId);
        const role = sender?.senderId && sender.platform
            ? resolveSpeakerRole(this.botId, sender.platform, sender.senderId, { sharedDir: this.sharedDir }).role
            : null;
        const granted = role !== null && OWNER_ROLES.has(role);
        const r = this.run(String(runId), sessionId, now.getTime());
        if (granted) {
            r.thresholdUsd = this.thresholdUsd + this.incrementUsd();
            this.trippedSessions.delete(sessionId);
        }
        this.append({
            event: "continue", ts: now.toISOString(), run_id: String(runId), session: sessionId,
            who: sender?.senderId ? `${sender.platform ?? "unknown"}:${sender.senderId}` : null, role,
            granted, threshold_usd: r.thresholdUsd,
        });
    }
    /** agent_end: marked, not dropped — OC does not order agent_end against
     *  the last reply payload, and the notice must still find its run. */
    endRun(runId, now = new Date()) {
        const r = runId ? this.runs.get(String(runId)) : undefined;
        if (r && r.endedMs === null)
            r.endedMs = now.getTime();
    }
    /** A trip whose notice never went out (NO_REPLY, no text) is ledgered. */
    drop(runId, r, now) {
        if (r.noticePending) {
            this.append({
                event: "owner_notice", ts: new Date(now).toISOString(), run_id: runId, session: r.sessionId,
                spend_usd: round6(r.tripSpendUsd), delivery: "not_delivered",
            });
        }
        this.runs.delete(runId);
    }
    prune(now) {
        for (const [k, r] of this.runs) {
            if (now - r.lastSeenMs > RUN_IDLE_MS || (r.endedMs !== null && now - r.endedMs > ENDED_KEEP_MS))
                this.drop(k, r, now);
        }
        for (const [k, t] of this.trippedSessions)
            if (now - t > 24 * RUN_IDLE_MS)
                this.trippedSessions.delete(k);
    }
    dir() {
        return path.join(this.sharedDir, "breakers", this.botId);
    }
    append(row) {
        if (!this.sharedDir)
            return;
        try {
            fs.mkdirSync(this.dir(), { recursive: true });
            fs.appendFileSync(path.join(this.dir(), "in-turn-checkpoints.jsonl"), JSON.stringify({ bot_id: this.botId, ...row }) + "\n");
        }
        catch (err) {
            this.logger?.warn(`Evolve in-turn checkpoint: ledger append failed (${err})`);
        }
    }
    /** The health control's input. Throttled; atomic tmp+rename. */
    writeStatus(now, force = false) {
        if (!this.sharedDir || (!force && now.getTime() - this.lastStatusMs < STATUS_THROTTLE_MS))
            return;
        this.lastStatusMs = now.getTime();
        const fp = path.join(this.dir(), "in-turn-checkpoint-status.json");
        const tmp = `${fp}.tmp.${process.pid}`;
        try {
            fs.mkdirSync(this.dir(), { recursive: true });
            fs.writeFileSync(tmp, JSON.stringify({
                armed: this.registered, threshold_usd: this.thresholdUsd, price_source: IN_TURN_PRICE_SOURCE,
                updated_at: now.toISOString(), ...this.counters,
            }) + "\n");
            fs.renameSync(tmp, fp);
        }
        catch {
            try {
                fs.unlinkSync(tmp);
            }
            catch { /* ignore */ }
        }
    }
}
function round6(n) {
    return Math.round(n * 1_000_000) / 1_000_000;
}
/** The gateway's one ledger. */
export const inTurnCheckpoint = new InTurnCheckpoint();
/** ``reply_payload_sending`` wrapper: append the owner notice to a stopped
 *  run's first text payload, over whatever the other handlers decided. */
export function withInTurnOwnerNotice(event, result, ledger = inTurnCheckpoint) {
    try {
        const payload = result?.payload ?? event?.payload;
        if (!payload || typeof payload.text !== "string" || !payload.text.trim())
            return result;
        const facts = ledger.takeOwnerNotice(event?.runId ? String(event.runId) : null);
        if (!facts)
            return result;
        return { payload: { ...payload, text: `${payload.text}\n\n${renderInTurnCheckpointMessage(facts)}` } };
    }
    catch {
        return result;
    }
}
//# sourceMappingURL=InTurnCheckpoint.js.map